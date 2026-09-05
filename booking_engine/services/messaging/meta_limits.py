"""Meta's ceilings. The one place they live, and the floor nothing may cross.

**The layering this module exists to enforce:**

    Meta's limits      — platform facts. Never exceeded, by construction.
        ↓  min()
    Kairo's limits     — commercial knobs: daily_cap, the plan allowance.
        ↓
    the queue

A commercial knob can only ever make us send *less*. `effective_daily_cap`
takes the minimum, so raising `senders.daily_cap` to 5000 on a Tier-250 sender
buys nothing instead of getting the WABA rate-limited or downgraded. Before
this module, `daily_cap` was an unrelated hand-set number and nothing read
`messaging_limit` at all.

**Everything here fails closed.** An unrecognised tier is treated as the
unverified floor, an unknown throughput as the slowest possible. A Meta value
we don't understand must never widen a limit — if Meta adds `TIER_5K` we
under-send until someone adds the row, which is the harmless direction.

Sources: Meta's messaging limits (business-initiated conversations per rolling
24h, per business phone number) and throughput levels. Coexistence numbers are
pinned at 20 mps for WhatsApp Business App compatibility.
"""
from __future__ import annotations

# Business-initiated conversations per **rolling 24 hours**, per number.
# Note the window: Meta's is rolling, ours (`sent_today`) is the calendar day.
# They are not interchangeable — see `whatsapp_queries.sent_last_24h`.
TIER_DAILY_CONVERSATIONS = {
    "TIER_50": 50,
    "TIER_250": 250,
    "TIER_1K": 1_000,
    "TIER_10K": 10_000,
    "TIER_100K": 100_000,
    "TIER_UNLIMITED": 10**9,
}

# What an unverified WABA gets, and what we assume when Meta tells us something
# we don't recognise.
UNVERIFIED_TIER_CONVERSATIONS = 250

# Messages per second, per number.
THROUGHPUT_MPS = {"STANDARD": 80, "HIGH": 1_000}

# A number live on both the WhatsApp Business App and Cloud API is pinned here
# by Meta for app compatibility. It is the slowest case that exists, so it is
# also the safe assumption when we don't know.
COEXISTENCE_MPS = 20

# The process-wide send rate can never be allowed past the slowest per-number
# throughput: below this, no single number can be over-driven however the
# claimed batch happens to be distributed across shops. This is why there is no
# per-number pacer — the invariant is enforced once, here, instead of with a
# second mechanism that would be dead machinery at any sane configuration.
MAX_SENDS_PER_MINUTE = COEXISTENCE_MPS * 60

# Meta pauses marketing template delivery to +1 recipients (since 2025-04-01).
# Refused at enqueue rather than discovered as a guaranteed provider failure.
MARKETING_BLOCKED_PREFIXES = ("+1",)

# New customers a Tech Provider may onboard per rolling 7 days: 10 until
# Access Verification is complete, 200 after. Configured rather than assumed,
# because getting it wrong means the 11th salon's onboarding fails at Meta
# with nothing in our logs explaining why.
ONBOARDING_PER_7_DAYS_BEFORE_VERIFICATION = 10
ONBOARDING_PER_7_DAYS_AFTER_VERIFICATION = 200


def tier_daily_conversations(messaging_limit: str | None) -> int:
    """Meta's per-24h conversation ceiling for this sender. Fails closed."""
    if not messaging_limit:
        return UNVERIFIED_TIER_CONVERSATIONS
    return TIER_DAILY_CONVERSATIONS.get(
        messaging_limit.upper(), UNVERIFIED_TIER_CONVERSATIONS
    )


def throughput_mps(*, platform_type: str | None, throughput_level: str | None) -> int:
    """Messages per second this number may be driven at. Fails closed."""
    if (platform_type or "").upper() == "COEXISTENCE":
        return COEXISTENCE_MPS
    return THROUGHPUT_MPS.get((throughput_level or "").upper(), COEXISTENCE_MPS)


def effective_daily_cap(sender: dict) -> int:
    """How many messages this sender may send in 24h — the binding constraint.

    `min(Meta's tier, our drip rate)`. The whole point is that it is a minimum:
    our number is a product decision about pacing a salon's marketing, Meta's
    is a platform ceiling with a quality-rating penalty behind it, and ours
    must never be able to authorise more than theirs.
    """
    ours = int(sender.get("daily_cap") or 0)
    theirs = tier_daily_conversations(sender.get("messaging_limit"))
    return max(0, min(ours, theirs))


def safe_sends_per_minute(configured: int) -> int:
    """Clamp the configured global send rate to what no number can be hurt by.

    Returns 0 (no pacing) only if explicitly configured as such — used by
    tests, where real sleeps buy no coverage.
    """
    if configured <= 0:
        return 0
    return min(configured, MAX_SENDS_PER_MINUTE)


def onboarding_limit(access_verified: bool) -> int:
    return (
        ONBOARDING_PER_7_DAYS_AFTER_VERIFICATION if access_verified
        else ONBOARDING_PER_7_DAYS_BEFORE_VERIFICATION
    )


def marketing_allowed(phone_e164: str) -> bool:
    """False for destinations where Meta will not deliver marketing at all."""
    return not phone_e164.startswith(MARKETING_BLOCKED_PREFIXES)
