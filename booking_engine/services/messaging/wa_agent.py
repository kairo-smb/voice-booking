"""Who is allowed to speak on a thread.

Three writers exist at once — the customer, the owner (webapp AND the WhatsApp
Business App on their phone), and the agent. Only one of them is ours to
control, so every rule here is about the agent standing down.

Every salon is `source='coexistence'`: the number is still live on the owner's
own phone and they answer from it out of habit. Meta reports those replies back
as echoes (`outbound_messages.origin = 'phone'`). So an agent talking over the
owner is not a distant edge case to harden against later — it is what happens
on day one unless something stops it, and this module is that something.

**The charge is not here, and that is deliberate.** `/whatsapp/agent` in the
marketing-engine gates on the shop's basket before it calls a provider (402 when
empty) and settles the *actual* LLM cost against that same basket after a turn
that ran. Adding a `charge_actual` call on this side would bill the salon twice
for one turn — once for real, once for a flat number invented here — which is
exactly the failure CLAUDE.md's 2026-08-12 entry forbids ("Two debit paths for
the same charge would eventually double-charge or drift") and which the
2026-09-03 entry deleted this repo's own basket arithmetic to prevent. So the
cost rule this module owns is the one the engine cannot see: a **ceiling on how
many turns one conversation may buy**, and a refused charge (the 402) standing
the agent down rather than running the turn unpaid.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from booking_engine.clients import marketing_agent
from booking_engine.clients import meta_whatsapp as meta
from booking_engine.config import get_settings
from booking_engine.db import queries
from booking_engine.db import service_intake_queries as intake_q
from booking_engine.db import voice_config_queries as config_q
from booking_engine.db import wa_session_queries as wsq
from booking_engine.db import whatsapp_thread_queries as tq
from booking_engine.services.messaging import wa_routing

logger = logging.getLogger(__name__)

# People send "ciao" / "volevo prenotare" / "per sabato" as three messages.
# Answering each is three replies to one thought, and three billed turns.
DEBOUNCE_SECONDS = 2.0

# A booking is four or five exchanges. Twelve means the conversation is not
# going where the agent thinks it is, and the honest move is a human — not
# another turn on the salon's basket.
#
# Counted **per session**, off `origin='agent'` rows since the session's own
# `started_at`, so yesterday's conversation cannot exhaust today's and no
# counter column has to be kept true.
MAX_SESSION_TURNS = 12


def may_speak(thread: dict) -> tuple[bool, str]:
    """Every reason the agent stays quiet. An allowlist of conditions, so a new
    thread state defaults to silence rather than to speech.

    Pure: a dict in, a verdict out. No clock, no database, no Meta — the same
    shape as `wa_routing.decide` and `number_health.decide_health`. The rules
    about who may speak are the last thing that should be tangled with IO.

    Every refusal carries a **distinct** reason string. Task 24 renders one
    sentence per reason to the owner, and "the agent is quiet and nobody can say
    why" is the state that makes an owner switch it off.

    Read with `.get`, not `[]`, on purpose: a thread dict missing a key must
    produce a refusal, not a KeyError. A refusal is silence, which is the safe
    direction; an exception is a crash in a background task with nobody to
    raise to. Note that a blank dict falls out at the first rule — the two
    positive requirements (opted in, intent we handle) are what a caller must
    *establish*, never what it gets by default.
    """
    if not thread.get("agent_enabled"):
        return False, "not_opted_in"
    if thread.get("intent") not in wa_routing.WHITELIST:
        return False, "intent_not_whitelisted"
    if thread.get("escalated"):
        return False, "escalated"
    if thread.get("human_replied_at") is not None:
        return False, "human_took_over"     # echo or webapp, same rule
    if int(thread.get("agent_turns") or 0) >= MAX_SESSION_TURNS:
        return False, "turn_limit"
    return True, "ok"


# The owner pressing "rispondo io" in the Inbox. Written into the session row's
# `outcome_reason` so the read side can tell it apart from the agent giving up:
# both are `outcome = 'escalated'`, and "hai preso tu questa conversazione" and
# "l'assistente ti ha passato la conversazione" are not the same sentence.
TAKEOVER_REASON = "human_took_over"


def agent_status(thread: dict) -> tuple[bool, str | None]:
    """What to TELL THE OWNER about a thread — the same rule, read aloud.

    `may_speak` decides; this only renames one of its verdicts. There is no
    second policy here on purpose: a status line that disagreed with the rule
    would be worse than no status line, because the owner would trust it.

    Returns `(active, reason)`, with `reason` None while the agent is speaking.
    Four reasons reach the Inbox — `not_opted_in`, `intent_not_whitelisted`,
    `escalated`, `human_took_over` — and `turn_limit` is not one of them: it is
    the refusal that escalates the session, so by the time anyone reads the
    thread back it presents as `escalated`, which is the true thing to say.

    The rename: an explicit takeover is a *person taking the thread*, which is
    what `human_took_over` means, and it arrives as an escalation only because
    that is the durable row we already had to write it on.
    """
    ok, reason = may_speak(thread)
    if ok:
        return True, None
    if reason == "escalated" and thread.get("outcome_reason") == TAKEOVER_REASON:
        return False, TAKEOVER_REASON
    return False, reason


async def handle(sender: dict, row: dict, *, intent: str | None) -> None:
    """Answer one inbound message, or stand down and say why.

    Called from `wa_inbound` on a routed thread. Never raises on its own
    account — it runs under that module's fire-and-forget wrapper, and the
    collaborators here are individually tolerant for the same reason.
    """
    shop_id = sender.get("shop_id")
    phone = str(row.get("from_phone") or "")
    if not shop_id or not phone:
        return
    settings = get_settings()

    # 1. The cheapest two rules first, before a sleep or a session row. A shop
    #    that never asked for an agent must cost nothing to not-answer, and an
    #    intent outside the allowlist is not ours however the thread looks.
    #    The session-dependent fields are genuinely absent here rather than
    #    assumed away: there may be no session yet, and if there is one, the
    #    authoritative check at step 4 reads its real state.
    config = await config_q.get_config(shop_id)
    agent_enabled = bool(config and config.get("whatsapp_agent_enabled"))
    ok, reason = may_speak({"agent_enabled": agent_enabled, "intent": intent})
    if not ok:
        logger.info("whatsapp.agent_silent shop=%s phone=%s reason=%s",
                    shop_id, phone, reason)
        return

    # 2. Debounce. Three messages about one thought get one answer.
    if await _superseded(shop_id, phone, row):
        logger.info("whatsapp.agent_superseded shop=%s phone=%s", shop_id, phone)
        return

    # 3. The session row: the conversation's identity, and the authorization
    #    basis for every booking tool this turn may call.
    call_id = await wsq.open_session(
        shop_id=shop_id, phone=phone, customer_id=row.get("customer_id"),
    )
    state = await wsq.session_state(call_id=call_id, phone=phone)

    # 4. The authoritative check, on real state. Re-run after the debounce
    #    rather than before it: the owner picking up their phone during those
    #    two seconds is precisely the race this exists to lose gracefully.
    thread = {
        "agent_enabled": agent_enabled,
        "intent": intent,
        "escalated": state.get("escalated"),
        "human_replied_at": state.get("human_replied_at"),
        "agent_turns": state.get("agent_turns"),
    }
    ok, reason = may_speak(thread)
    if not ok:
        # `turn_limit` is the one refusal here that is something *happening*
        # rather than a thread that was never the agent's: the conversation ran
        # long and a person now has to finish it, so it goes in the owner's
        # queue. The others are already where they belong.
        await _stand_down(call_id, shop_id, phone, reason,
                          escalate=reason in _ESCALATING_REASONS)
        return

    # 5. One turn. The catalogue is read once and used twice — the services
    #    themselves and the owner's per-service intake questions are keyed off
    #    the same rows, and fetching them separately bought nothing.
    catalogue = await queries.list_services(shop_id)
    turn = await marketing_agent.turn(
        shop_id=shop_id,
        call_id=call_id,
        shop_name=await _shop_name(shop_id),
        services=_services(catalogue),
        intake=await intake_q.for_services(shop_id, [r["id"] for r in catalogue]),
        messages=await _messages(shop_id, phone, state.get("started_at")),
        first_turn=int(state.get("agent_turns") or 0) == 0,
        customer_name=await _customer_name(shop_id, phone),
        customer_phone=phone,
        now=datetime.now(timezone.utc),
        settings=settings,
    )
    logger.info(
        "whatsapp.agent_turn shop=%s call=%s escalate=%s reason=%s tools=%s cost=%s",
        shop_id, call_id, turn.escalate, turn.reason, turn.tool_calls,
        turn.cost_usd,
    )

    # 6. An escalation sends NOTHING. `text` is empty whenever `escalate` is
    #    true — an apology that still reads like an answer leaves the customer
    #    waiting for a reply that is not coming. The empty basket (402) arrives
    #    here as reason='no_credit', and takes the same path: silence, and the
    #    thread lands in the owner's queue.
    if turn.escalate:
        await _stand_down(call_id, shop_id, phone, turn.reason or "escalated",
                          escalate=True)
        return

    # A turn that produced no text is not an answer either. Sending an empty
    # body is a Graph error; sending a blank bubble is worse. Stripped here as
    # well as in the client: this is the boundary the send actually crosses,
    # and whitespace is not speech whichever layer let it through.
    text = turn.text.strip()
    if not text:
        logger.info("whatsapp.agent_empty_text shop=%s call=%s", shop_id, call_id)
        return

    await _say(sender, phone, text)


# Refusals that mean "a person is needed on this thread", as opposed to "this
# was never the agent's to answer". Only these are written to the session row:
# marking a not-opted-in shop's every thread escalated would fill the owner's
# queue with threads nothing went wrong on.
_ESCALATING_REASONS = ("turn_limit",)


async def _stand_down(
    call_id: UUID, shop_id, phone: str, reason: str, *, escalate: bool = False,
) -> None:
    """Go quiet, and leave a reason where the owner will see it.

    `escalate` is False for the refusals that are not something going wrong —
    a shop that never asked for an agent, an intent that was never ours, a
    thread the owner is already answering. Marking those escalated would fill
    the owner's queue with threads nothing happened on.
    """
    logger.info("whatsapp.agent_silent shop=%s phone=%s call=%s reason=%s",
                shop_id, phone, call_id, reason)
    if escalate:
        await wsq.mark_escalated(call_id=call_id, reason=reason)


async def _superseded(shop_id, phone: str, row: dict) -> bool:
    """Wait out the debounce, then answer: did a newer message arrive?

    **The debounce is a sleep plus a re-read, not a per-thread timer**, and the
    choice matters under the fire-and-forget model `wa_inbound` already uses.
    A timer needs a mutable registry of pending tasks keyed by thread, plus
    cancellation — shared state in a module whose every other collaborator is
    stateless, and state that two Fly machines would each keep their own copy
    of, so the timer would not actually debounce across them. This rule has no
    state at all: every task sleeps, then asks the database one question whose
    answer is the same for whoever asks it. The last message wins because it is
    the last, not because anyone coordinated.

    It **batches rather than drops**: the surviving task reads the whole session
    back out of the database at step 5, so all three of "ciao" / "volevo
    prenotare" / "per sabato" reach the agent — only the two earlier *turns* are
    dropped, never the two earlier messages.

    Two threads debouncing at once cannot interfere, by construction: this
    question is scoped to one shop and one phone, and neither task touches
    anything the other reads.
    """
    await asyncio.sleep(DEBOUNCE_SECONDS)
    history = await tq.inbound_history(shop_id, phone)
    if not history:
        return False
    # `inbound_history` is ascending, so the newest is last. Our row was
    # committed before the worker was scheduled, so it is certainly in here;
    # finding something after it means a later task will answer for both.
    return history[-1].get("id") != row.get("id")


async def _messages(shop_id, phone: str, since) -> list[dict[str, str]]:
    """The conversation so far, in the engine's `{role, content}` shape."""
    if since is None:
        return []
    rows = await wsq.session_transcript(shop_id=shop_id, phone=phone, since=since)
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def _services(rows: list[dict]) -> list[dict]:
    """The catalogue, in the engine's shape. `price_cents` from `price_eur`.

    The agent needs prices to answer "quanto costa", which is an ordinary part
    of booking on this channel — unlike the voice agent, where cost is gated
    behind an explicit ask (CLAUDE.md 2026-07-21) because a phone agent reciting
    a price list is a worse experience than a written one.
    """
    return [
        {
            "id": str(r["id"]),
            "name": r["service_name"],
            "duration_minutes": r.get("duration_minutes"),
            "price_cents": _cents(r.get("price_eur")),
        }
        for r in rows
    ]


def _cents(price_eur) -> int | None:
    """`price_eur` is numeric; the engine's contract is integer cents."""
    if price_eur is None:
        return None
    try:
        return int(round(float(price_eur) * 100))
    except (TypeError, ValueError):
        return None


async def _shop_name(shop_id) -> str:
    shop = await queries.get_shop(shop_id)
    return str((shop or {}).get("shop_name") or (shop or {}).get("name") or "")


async def _customer_name(shop_id, phone: str) -> str | None:
    """The customer's name, when we know it. None is a legal answer.

    Most inbound numbers match a row — this is a salon's own clientele — but a
    new customer writing for the first time does not, and the agent asking for
    a name is better than the agent inventing one.
    """
    matches = await queries.find_customers_by_phone(shop_id, phone)
    return matches[0].get("full_name") if matches else None


async def _say(sender: dict, phone: str, text: str) -> None:
    """Send the agent's reply, and record it as the agent's.

    No window check: Meta's 24h service window is reset by the customer's own
    message, which arrived seconds ago — a check here could only ever pass.

    `origin='agent'` is what keeps the next turn honest. Recorded as 'kairo'
    (the default, meaning the owner in the webapp) the agent would read its own
    last reply as a human taking the thread over and silence itself after one
    turn — the self-suspension that migration 25's third origin value exists to
    prevent.
    """
    if not sender.get("phone_number_id") or not sender.get("access_token"):
        logger.error(
            "whatsapp.agent_unsendable shop=%s: sender has no "
            "phone_number_id/token", sender.get("shop_id"),
        )
        return
    sid = await meta.send_text(
        phone_number_id=str(sender["phone_number_id"]), to=phone,
        body=text, token=str(sender["access_token"]),
    )
    try:
        await tq.record_reply(
            shop_id=sender["shop_id"], to_phone=phone, body=text,
            provider_sid=sid, origin="agent",
        )
    except Exception:  # noqa: BLE001
        # Meta already delivered it. Losing the row costs the thread view a
        # bubble and — worse — leaves this turn uncounted against the ceiling,
        # so it is logged loudly rather than swallowed. Re-sending would be the
        # worse of the two wrongs: the customer would read it twice.
        logger.exception(
            "whatsapp.agent_reply_not_recorded shop=%s to=%s sid=%s",
            sender.get("shop_id"), phone, sid,
        )
