"""What happens to an inbound WhatsApp message after the webhook has answered
200 and gone.

The webhook must answer fast and must never fail, so everything slow —
downloading media, transcription, the classifier — lives here, behind a
fire-and-forget task.

**Everything degrades to "a human looks at it".** No credit, engine down,
media gone, malformed anything: the message stays in the thread as raw text
and the session stays unrouted, which puts it in the owner's 'Da gestire'
queue. That is the direction a mistake should fall — it costs the owner a
glance, where a wrong verdict routes a real customer to the wrong handler and
nobody ever finds out.

`process` therefore **never raises**. It runs in a background task with nobody
to raise to, so an exception here is a customer message silently lost with no
error anywhere — same posture as `clients/marketing_triage.py` and
`wa_transcribe.py`, which is why several steps below are individually
tolerant as well as collectively wrapped.
"""
from __future__ import annotations

import asyncio
import logging

from booking_engine.clients import marketing_triage as triage
from booking_engine.clients import meta_whatsapp as meta
from booking_engine.config import get_settings
from booking_engine.db import whatsapp_thread_queries as tq
from booking_engine.services.messaging import wa_routing, wa_transcribe

logger = logging.getLogger(__name__)

MENU_BODY = "Non ho capito bene, cosa ti serve?"

# The disambiguation step, offered once when the model was unsure. Three is
# Meta's own ceiling on reply buttons, so this is the whole menu, not a
# selection from a longer one.
#
# `other` is deliberately NOT in `wa_routing.WHITELIST`: tapping it routes to
# a human, which is the honest answer for a request no handler covers. The
# titles are under Meta's 20-character limit.
MENU_BUTTONS = [
    ("booking", "Prenotare"),
    ("reschedule", "Spostare o disdire"),
    ("other", "Altro"),
]

# asyncio holds only a WEAK reference to a task, so a bare `create_task` whose
# return value is dropped can be garbage-collected mid-flight. This repo
# shipped exactly that bug in the call supervisor (CLAUDE.md 2026-07-21) and
# it presented as intermittent silence — the hardest possible symptom to
# diagnose. The set is the strong reference; the done callback is what stops
# it growing forever.
_TASKS: set[asyncio.Task] = set()


def schedule(sender: dict, row: dict) -> None:
    """Hand one freshly recorded inbound message to the worker, and return."""
    task = asyncio.create_task(process(sender, row))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def process(sender: dict, row: dict) -> None:
    """Transcribe, name, and act. Never raises — see the module docstring."""
    try:
        await _process(sender, row)
    except Exception:  # noqa: BLE001 — a background task has nobody to raise to
        logger.exception(
            "whatsapp.inbound_worker_failed shop=%s wamid=%s",
            sender.get("shop_id"), row.get("wa_message_id"),
        )


async def _process(sender: dict, row: dict) -> None:
    shop_id = sender.get("shop_id")
    phone = str(row.get("from_phone") or "")
    settings = get_settings()

    # 1. A voice note is unreadable in the Inbox and invisible to the router
    #    until it has words. The transcript is stored in its own column and
    #    NEVER overwrites `body`, which is the record of what Meta delivered.
    transcript = await _transcribed(sender, row, settings)
    if transcript:
        await tq.set_transcript(row["id"], transcript)

    text = (transcript or str(row.get("body") or "")).strip()

    history = await tq.inbound_history(shop_id, phone)

    # 2. Naming a request is a phase of the conversation, not a property of
    #    each message. Once this session has a verdict the handler owns it and
    #    the classifier must never run on it again — that is the rule the
    #    per-message cost lives or dies by.
    if wa_routing.routed_intent(history):
        return

    # 3. A tap on a menu we sent is already named: the webhook stored the id we
    #    defined as the intent, with confidence 1.0. Normally step 2 has
    #    already returned by here, since that verdict is in `history` — this is
    #    the rule stated in its own right, so a future change to
    #    `routed_intent` cannot silently start paying a model for an answer we
    #    wrote ourselves.
    if row.get("intent"):
        return

    # Nothing to classify: an unsupported type (sticker, location) or a voice
    # note we could not read. The classifier costs real money per call, so an
    # empty string is never sent to it.
    if not text:
        return

    # 4. Name it. `classify` returns None on every failure — 402, engine down,
    #    malformed — and None is not a verdict, so nothing is written and the
    #    session stays unrouted.
    verdict = await triage.classify(shop_id=shop_id, text=text, settings=settings)
    if verdict is None:
        logger.info("whatsapp.inbound_unclassified shop=%s phone=%s", shop_id, phone)
        return

    decision = wa_routing.decide(history=history, verdict=verdict)
    # Stored before acting: a send that fails should cost the menu, not the
    # verdict that explains why the menu went out.
    await tq.set_verdict(row["id"], verdict, decision)
    logger.info(
        "whatsapp.inbound_decided shop=%s phone=%s action=%s intent=%s",
        shop_id, phone, decision.action, decision.intent,
    )

    # 5. Act.
    if decision.action == "menu":
        await _send_menu(sender, phone)
    # 'route' → the booking agent lands here in increment B. Until then a
    # routed thread simply stops needing the owner's attention, which is what
    # `whatsapp_thread_queries.needs_attention` already reads off the intent.
    # 'human' → nothing is sent. We do not tell the customer "a human will
    # reply"; the thread is already in the owner's queue, and a promise we
    # make on the owner's behalf is one they may not keep.


async def _transcribed(sender: dict, row: dict, settings) -> str | None:
    """The voice note's words, or None — never a guess, never a partial.

    Tolerant on its own rather than only under `process`'s wrapper: media that
    has expired at Meta must degrade to "no transcript" and let the rest of
    the pipeline run on whatever `body` held, not abort the whole message.
    """
    if row.get("message_type") != "audio":
        return None
    media_id = row.get("media_id")
    token = sender.get("access_token")
    if not media_id or not token:
        return None
    try:
        # The token is already opened: `whatsapp_queries._opened` unseals
        # `senders.access_token` at the one boundary every reader passes
        # through, so nothing here touches secret_box.
        audio = await meta.get_media(media_id=str(media_id), token=str(token))
        return await wa_transcribe.transcribe(
            shop_id=sender.get("shop_id"), audio=audio,
            # Meta's wamid, so a ledger row traces back to the exact message
            # that caused the charge.
            run_ref=str(row.get("wa_message_id") or row.get("id") or ""),
            settings=settings,
        )
    except Exception:  # noqa: BLE001 — a lost voice note is still a message
        logger.exception(
            "whatsapp.inbound_transcribe_failed shop=%s media=%s",
            sender.get("shop_id"), media_id,
        )
        return None


async def _send_menu(sender: dict, phone: str) -> None:
    """One menu, then a human — `wa_routing` never offers a third guess."""
    if not sender.get("phone_number_id") or not sender.get("access_token"):
        logger.error(
            "whatsapp.menu_unsendable shop=%s: sender has no phone_number_id/token",
            sender.get("shop_id"),
        )
        return
    await meta.send_interactive(
        phone_number_id=str(sender["phone_number_id"]),
        to=phone,
        body=MENU_BODY,
        buttons=MENU_BUTTONS,
        token=str(sender["access_token"]),
    )
