"""Voice notes, which on this vertical are not an edge case: customers send
"vorrei fare il colore come l'altra volta" as audio far more often than typed.
Untranscribed, such a message lands with an empty body and `message_type =
'audio'` — unreadable in the Inbox and invisible to the router.

Transcription lives here and not in marketing-engine because the audio bytes
need the salon's business token to come off Meta (`meta_whatsapp.get_media`,
against a token this repo holds encrypted at rest); shipping the bytes across
to be transcribed elsewhere would move a secret in order to move a payload.

Every failure — no credit, no key, media gone, provider error, empty result —
returns `None`, never a partial or invented transcript. A later task stores
the return value in a `transcript` column that is NULL by default, and NULL
means "we do not know", which is the truth. `""` would mean "we transcribed it
and it said nothing", a different and wrong claim. It must never overwrite
`body`.

`transcribe()` never raises: it is awaited from a fire-and-forget background
task, where an uncaught exception is a silently dropped customer message —
same posture as `clients/marketing_triage.py`.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from booking_engine.clients import webapp_credits
from booking_engine.config import Settings, get_settings

logger = logging.getLogger(__name__)

_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_TIMEOUT_SECONDS = 30.0

# The cheapest transcription model OpenAI sells ($0.003/min vs $0.006 for
# whisper-1 and gpt-4o-transcribe). A 10-second "vorrei il colore" needs no
# more; word-level timestamps, which this model does not do, are the only
# thing the dearer ones add that we would ever have wanted.
MODEL = "gpt-4o-mini-transcribe"

# Meta delivers voice notes as audio/ogg (opus). The *extension* is how the
# endpoint decides how to decode the upload, so the filename is not cosmetic.
_FILENAME = "voice.ogg"
_CONTENT_TYPE = "audio/ogg"

# A flat charge, priced at the ceiling of a typical voice note rather than at
# its average — 30 seconds, the top of the 5–30s range these actually arrive
# in. Derived, not rounded to something that looked nice:
#
#     30s = 0.5 min × $0.003/min (gpt-4o-mini-transcribe)   = $0.0015 raw
#     × 10   the house LLM margin (the webapp's rawToUserCredits) — this is
#            LLM spend, NOT the 2× carrier pass-through `send_credits` applies
#            to Twilio cost
#     × 1000 credits per USD (the same CREDITS_PER_USD `send_credits` uses)
#     = 15 credits
#
# Flat rather than per-second because a duration meter would mean decoding the
# container here to learn something worth a fraction of a credit. The honest
# consequence: a note longer than 30s is transcribed under cost. That is
# bounded by Meta's own 16MB media cap, not by us, and a salon whose customers
# routinely send minutes of audio is the signal to build the meter.
TRANSCRIBE_CREDITS = 15


async def transcribe(
    *,
    shop_id: UUID,
    audio: bytes,
    run_ref: str,
    settings: Settings | None = None,
) -> str | None:
    """Transcribe one voice note. `None` on every failure — never a guess.

    `run_ref` is the Meta message id (`wamid...`), so a ledger row can be
    traced back to the exact message that caused the charge.
    """
    try:
        return await _transcribe(
            shop_id=shop_id, audio=audio, run_ref=run_ref,
            settings=settings or get_settings(),
        )
    except Exception:  # noqa: BLE001 — a background task has nobody to raise to
        logger.exception("whatsapp.transcribe_unexpected_error shop=%s", shop_id)
        return None


async def _transcribe(
    *, shop_id: UUID, audio: bytes, run_ref: str, settings: Settings,
) -> str | None:
    if not settings.openai_api_key:
        # Fail closed *before* spending the salon's credit on work that cannot
        # happen. An unconfigured provider is our problem, not theirs.
        logger.error(
            "whatsapp.transcribe_unconfigured shop=%s: OPENAI_API_KEY not set",
            shop_id,
        )
        return None

    # Charged before the work, refused on 402. The gate is what stops us paying
    # a provider for a shop that cannot pay us: an empty basket means the raw
    # message simply stays in the owner's queue as a vocale, rather than a
    # transcript nobody paid for.
    #
    # This is deliberately the OPPOSITE order from the SMS path, which debits
    # only after Twilio accepts (AGENTS.md 2026-08-12), and the asymmetry is a
    # decision rather than an oversight. There, a rejected send is routine and
    # the charge is ~186 credits. Here the only way to keep the gate *and*
    # refund a failed run would be to POST a negative amount to charge-actual —
    # basket arithmetic this repo deliberately gave up in the 2026-09-03 entry,
    # and a contract the webapp does not offer. So a provider failure leaves
    # the salon charged 15 credits (~$0.0015 of real cost) for nothing; it is
    # logged loudly below so the exposure is countable rather than invisible.
    if not await webapp_credits.charge_actual(
        shop_id=shop_id,
        run_type=webapp_credits.WHATSAPP_TRANSCRIBE,
        run_ref=run_ref,
        credits=TRANSCRIBE_CREDITS,
        settings=settings,
    ):
        logger.info("whatsapp.transcribe_refused shop=%s run_ref=%s", shop_id, run_ref)
        return None

    try:
        text = await _openai_transcribe(audio, settings.openai_api_key)
    except Exception:  # noqa: BLE001 — every provider failure is the same refusal
        logger.exception(
            "whatsapp.transcribe_failed shop=%s run_ref=%s credits=%s charged "
            "for nothing", shop_id, run_ref, TRANSCRIBE_CREDITS,
        )
        return None

    # `or None`: an empty or whitespace-only result is "we do not know", not
    # "it said nothing".
    return text.strip() or None


async def _openai_transcribe(audio: bytes, api_key: str) -> str:
    """POST the bytes to OpenAI's transcription endpoint, return the text.

    `response_format=text` returns the transcript as the raw body — one fewer
    parsing step, and one fewer shape to be wrong about, than the JSON form.
    No `language` hint is sent: the salon's customers are overwhelmingly
    Italian but not exclusively, and forcing `it` would mangle the ones who
    are not rather than merely detect them a little less reliably.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(
            _TRANSCRIPTIONS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (_FILENAME, audio, _CONTENT_TYPE)},
            data={"model": MODEL, "response_format": "text"},
        )
        response.raise_for_status()
        return response.text
