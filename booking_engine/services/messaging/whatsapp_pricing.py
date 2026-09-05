"""What one WhatsApp message costs the salon, per Meta conversation category.

**One party bills now, not two.** Under the Twilio model this table added
Twilio's flat per-message platform fee to Meta's rate. Going direct as a Meta
Tech Provider removes that fee *and* removes Kairo from the transaction
entirely: the salon attaches its own card to its own WABA and Meta charges it
directly (a Tech Provider, unlike a Solution Partner, has no credit line to
share). So nothing here is a cost Kairo recovers — it is what the owner will
see on their own Meta invoice.

That is why `send_credits` is gone from this module and `try_debit_for_message`
is gone from the send path: charging AI credits on top would bill the salon
twice for one message. The SMS path still does both, correctly — there Kairo
really does pay Twilio.

These remain **estimates**, shown to the owner before they click send and
written to `outbound_messages.price_usd` at send time. Meta reports no amount
on send and none on the status webhook, so unlike the SMS path there is no
later correction to a real price. The invoice is Meta's, not ours.

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
