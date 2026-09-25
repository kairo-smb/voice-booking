"""One turn of the WhatsApp booking agent, via the marketing-engine gateway.

The turn itself — the prompt, the model, the tool-calling loop — lives in the
marketing-engine repo. This is the thin HTTP client that asks for one.

**The engine gates and charges the turn, not this repo.** `/whatsapp/agent`
reads the shop's basket before it calls a provider and answers **402** when it
is empty; on a turn that ran, it settles the *actual* LLM cost against that
same basket itself. So there is exactly one debit path for a turn, which is the
standing rule in AGENTS.md (2026-08-12: "Two debit paths for the same charge
would eventually double-charge or drift", and 2026-09-03, which deleted this
repo's own basket arithmetic for precisely that reason). A `charge_actual` call
from this side would be a second, invented, flat charge stacked on top of a
real one — see `wa_agent.py` for the whole argument.

`Turn.escalate` is the only thing a caller needs to branch on, and **`text` is
empty whenever it is true** — the engine's own contract. An agent that
apologises in a way that still reads like an answer leaves the customer waiting
for a reply that is not coming.

Every failure returns `REFUSED` — unconfigured, 402, engine down, timeout,
malformed JSON — which is an escalation with a reason, never a guess at what
the agent would have said. `turn()` never raises: it is awaited from a
fire-and-forget background task, where an uncaught exception is a customer
message silently dropped. Same posture as `clients/marketing_triage.py`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

import httpx

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_AGENT_PATH = "/whatsapp/agent"

# Longer than the classifier's 15s: a turn may run a tool-calling loop against
# this repo's own availability queries before it has anything to say. Still
# bounded — a customer waiting on WhatsApp has no spinner to watch, and a turn
# that takes a minute has already failed at being a conversation.
_TIMEOUT_SECONDS = 45.0


class Turn(NamedTuple):
    """What the agent decided. `text` is empty whenever `escalate` is true."""
    text: str
    escalate: bool
    reason: str | None
    tool_calls: int
    cost_usd: float


def _refused(reason: str) -> Turn:
    """Every failure is the same shape: say nothing, hand it to a person.

    The reason travels with it so the owner can be told *why* the agent went
    quiet — "the agent is silent and nobody can say why" is the state that makes
    an owner switch it off.
    """
    return Turn(text="", escalate=True, reason=reason, tool_calls=0, cost_usd=0.0)


async def turn(
    *,
    shop_id: UUID,
    call_id: UUID,
    shop_name: str,
    services: list[dict[str, Any]],
    intake: dict[str, str],
    messages: list[dict[str, str]],
    first_turn: bool,
    customer_name: str | None,
    customer_phone: str | None,
    now: datetime,
    settings: Settings,
) -> Turn:
    """Run one turn. Never raises; every failure is an escalation with a reason."""
    try:
        return await _turn(
            shop_id=shop_id, call_id=call_id, shop_name=shop_name,
            services=services, intake=intake, messages=messages,
            first_turn=first_turn, customer_name=customer_name,
            customer_phone=customer_phone, now=now, settings=settings,
        )
    except Exception:  # noqa: BLE001 — every failure here is the same refusal
        logger.exception("whatsapp.agent_unexpected_error shop=%s call=%s",
                         shop_id, call_id)
        return _refused("agent_error")


async def _turn(
    *,
    shop_id: UUID,
    call_id: UUID,
    shop_name: str,
    services: list[dict[str, Any]],
    intake: dict[str, str],
    messages: list[dict[str, str]],
    first_turn: bool,
    customer_name: str | None,
    customer_phone: str | None,
    now: datetime,
    settings: Settings,
) -> Turn:
    base = (settings.market_intel_api_url or "").rstrip("/")
    secret = settings.market_intel_secret or ""
    if not base or not secret:
        logger.warning(
            "whatsapp.agent_unconfigured shop=%s: MARKET_INTEL_API_URL/"
            "MARKET_INTEL_SECRET not configured", shop_id,
        )
        return _refused("unconfigured")

    payload = {
        "shop_id": str(shop_id),
        # Not bookkeeping: the engine passes this back to *this* repo's voice
        # tools, which read the shop off the session row and never off a
        # header. It is the authorization basis for every booking the turn
        # touches.
        "call_id": str(call_id),
        "shop_name": shop_name,
        "services": services,
        "intake": intake,
        "messages": messages,
        "first_turn": first_turn,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "now": now.isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}{_AGENT_PATH}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.error("whatsapp.agent_unreachable shop=%s call=%s err=%s",
                     shop_id, call_id, exc)
        return _refused("agent_unreachable")

    if resp.status_code == 402:
        # Expected traffic, not an incident: the salon has no AI credit and the
        # engine refused to run a turn it cannot be paid for. Logged quietly,
        # like webapp_credits' and marketing_triage's 402 handling — an
        # out-of-credit salon must not fill the logs with something that reads
        # like an outage.
        logger.info("whatsapp.agent_refused_no_credit shop=%s call=%s",
                    shop_id, call_id)
        return _refused("no_credit")

    if resp.status_code != 200:
        logger.error("whatsapp.agent_failed shop=%s call=%s status=%s body=%s",
                     shop_id, call_id, resp.status_code, resp.text[:300])
        return _refused("agent_error")

    try:
        body = resp.json()
    except ValueError:
        logger.error("whatsapp.agent_malformed_json shop=%s call=%s",
                     shop_id, call_id)
        return _refused("agent_error")

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        logger.error("whatsapp.agent_malformed_turn shop=%s call=%s data=%r",
                     shop_id, call_id, data)
        return _refused("agent_error")

    escalate = data.get("escalate") is True
    text = data.get("text")
    text = text.strip() if isinstance(text, str) else ""

    return Turn(
        # Belt and braces on the engine's own contract. A future change there
        # that let an apology ride along with an escalation would otherwise
        # send the customer a reply *and* hand the thread to the owner, who
        # then answers a question that already looks answered.
        text="" if escalate else text,
        escalate=escalate,
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        tool_calls=_as_int(data.get("tool_calls")),
        cost_usd=_as_float(body.get("llm_cost_usd")),
    )


def _as_int(value: Any) -> int:
    """Telemetry out of a JSON body must never crash a live conversation."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
