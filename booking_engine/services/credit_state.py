"""Is this shop low on credit? One answer, read by every surface.

Owner decision, 2026-09-29: a **single** low-credit threshold per shop —
`voice_agent.shop_config.auto_topup_threshold_tokens`, **10 000 when unset** —
and the cockpit banner, the owner email, the WhatsApp responder and the Inbox
all read that same number. Below it the automatic responder stands down and
the conversation becomes the owner's; the WABA stays connected.

`balance` is the basket's *effective* balance (granted, if not expired, plus
purchased) — `token_basket_queries.get_balance`, the same arithmetic as the
webapp's `effectiveBalance`. This module only reads it: the basket and its
ledger are the webapp's (AGENTS.md, 2026-09-03), and a pause is not a charge.

The empty basket keeps its own path. marketing-engine answers 402 at balance 0
and `wa_agent` stamps `no_credit`; `low_credit` is the earlier, deliberate
pause that sits ahead of it, so a salon hears about it while the owner can
still top up rather than after the first unanswered customer.
"""
from __future__ import annotations

import logging
from uuid import UUID

from booking_engine.clients import webapp_notify
from booking_engine.config import Settings
from booking_engine.db import voice_config_queries as config_q
from booking_engine.db.token_basket_queries import get_balance

logger = logging.getLogger(__name__)

# The webapp's `CreditReminderBanner` and `VoiceAgentSettingsTab` read the same
# default. A NULL column means "the owner never chose", not "never warn me".
DEFAULT_THRESHOLD_TOKENS = 10_000


def is_low(*, balance: int, threshold: int) -> bool:
    """The one comparison. At exactly the threshold the responder pauses."""
    return balance <= threshold


async def credit_state(shop_id: UUID) -> dict:
    """`{"balance", "threshold", "low"}` for one shop.

    `is None`, never `or`: an explicit threshold of 0 is an owner choice
    ("only when empty") and must not fall back to the default.
    """
    config = await config_q.get_config(shop_id)
    raw = (config or {}).get("auto_topup_threshold_tokens")
    threshold = DEFAULT_THRESHOLD_TOKENS if raw is None else int(raw)
    balance = await get_balance(shop_id)
    return {"balance": balance, "threshold": threshold,
            "low": is_low(balance=balance, threshold=threshold)}


async def notify_sweep(*, settings: Settings) -> dict:
    """Hourly: email the owner once per low-credit episode, and close episodes.

    An episode opens on the first tick that finds an opted-in, online shop at
    or below its threshold with no stamp: the webapp is asked to mail the owner
    (`POST /api/v1/hair-salon/whatsapp/credit-low`) and the shop is stamped. It
    closes on the first tick that finds the balance back above the threshold,
    so the next dip mails again. A webapp that never answered (`None`) leaves
    the shop unstamped and the next tick retries; one that answered "not sent"
    (no owner mailbox) is stamped like a send, since that is a fact about the
    shop and the banner still covers it.

    One shop's failure must not abort the sweep: each is wrapped and counted
    under 'errors', the same shape as `wa_nudge.sweep`.
    """
    counts = {"notified": 0, "cleared": 0, "errors": 0}
    for row in await config_q.list_credit_notice_candidates():
        shop_id = row["shop_id"]
        try:
            state = await credit_state(shop_id)
            stamped = row.get("credit_low_notified_at") is not None
            if state["low"] and not stamped and row.get("eligible"):
                sent = await webapp_notify.whatsapp_credit_low(
                    shop_id=shop_id, balance=state["balance"],
                    threshold=state["threshold"], settings=settings,
                )
                if sent is None:
                    continue
                await config_q.set_credit_low_notified(shop_id, notified=True)
                counts["notified"] += 1
                logger.info("credit.low_notified shop=%s balance=%s threshold=%s sent=%s",
                            shop_id, state["balance"], state["threshold"], sent)
            elif not state["low"] and stamped:
                await config_q.set_credit_low_notified(shop_id, notified=False)
                counts["cleared"] += 1
        except Exception:  # noqa: BLE001 - one shop must not abort the sweep
            logger.exception("credit.notify_failed shop=%s", shop_id)
            counts["errors"] += 1
    return counts
