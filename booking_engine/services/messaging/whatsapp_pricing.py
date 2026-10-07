"""What one WhatsApp template send costs the salon: Meta's fee plus Kairo's.

**Two parties bill, for two different things.** As a Meta Tech Provider we have
no credit line to share: the salon attaches its own card to its own WABA and
Meta charges it directly, per conversation category. `META_USD_IT` is that fee
— an **estimate** shown to the owner before they click send and written to
`outbound_messages.price_usd`. Meta reports no amount on send and none on the
status webhook, so there is no later correction; the invoice is Meta's.

On top of it Kairo charges a flat `SEND_CREDITS` per template actually
delivered to Meta (owner decision, 2026-10-07): a platform fee for the send,
not a recovery of Meta's cost, the same for reminders, review requests and
marketing. It is not a margin on anything, so it never scales with Meta's
category rate. Generating a message's text is LLM work and is metered
separately by the engine; free-form replies inside the 24h window are the AI
responder's, which bills only its generation — neither path pays this fee.

ponytail: a flat IT-only table, not a country matrix. Every salon is Italian and
every recipient is an Italian consumer; add the country dimension when the first
non-IT recipient exists.
"""
from __future__ import annotations

# Meta's per-message fee, Italy, as of 2026-08. Keyed by the *product* name for
# the category, because that is what the owner sees in the UI — "promemoria",
# not "utility".
META_USD_IT = {
    "marketing": 0.0691,   # campaigns: promo_v1 and anything else MARKETING
    "utility": 0.0341,     # reminders / confirmations: UTILITY templates
    "authentication": 0.0512,
    "service": 0.0,        # free-form reply inside the 24h session window
}

# Kairo credits per template send (1000 credits = $1 list), charged after Meta
# accepts the message. Flat across categories by design — see the docstring.
SEND_CREDITS = 185


def estimate_usd(kind: str) -> float:
    """USD Meta will bill the salon for one message of this kind."""
    return META_USD_IT[kind]


def price_list() -> list[dict]:
    """The cost table the webapp renders, so "what will this cost me?" has an
    answer before the owner commits to a campaign."""
    return [
        {"kind": kind, "usd": round(estimate_usd(kind), 4)}
        for kind in ("marketing", "utility", "service")
    ]
