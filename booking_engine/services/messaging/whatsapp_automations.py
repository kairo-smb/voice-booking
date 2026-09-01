"""The automation tick stage: fire each shop's enabled rules against what is due.

This is the one place in the product where a message goes out with nobody
looking at it, so the rails live in the same place as the send:

1. Sender online?  Not online -> the shop is skipped entirely.
2. Quality gate.   YELLOW/RED pauses automated MARKETING; the two rules that
   ship here are UTILITY and continue regardless. The gate is written now so
   Plan 3's automated win-back (MARKETING) lands beside it without having to
   remember to add the pause.
3. Enqueue, then record. A crash between the two re-sends once (recoverable);
   recording first would silently drop a message forever.

Both templates are UTILITY: nothing is generated, every variable is a database
fact, so no marketing consent is needed and the 7-day recipient cooldown does
not apply. The send path agrees — see whatsapp_send's category branch.

Per-rule params the owner configures on the webapp tile:
  feedback:  hours_after (default 24), platform (google/facebook/instagram/
             general), link (optional review URL).
  reminder:  min_no_shows (default 0 = everyone; the 24h lead is fixed).
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from booking_engine.db import whatsapp_automation_queries as aq
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.phone_normalize import normalize_e164
from booking_engine.services.messaging.whatsapp_templates import (
    clean_variable, render,
)

logger = logging.getLogger(__name__)

SALON_TZ = ZoneInfo("Europe/Rome")

# The audit-trail marker for an automated send. There is no `source` column on
# outbound_messages, so the marker lives in campaign_key — the webapp's
# per-customer message history already surfaces it. The appointment id makes it
# unique per send, which keeps the campaign-level unique index from ever
# blocking a retry or a second rule reaching the same customer.
AUTOMATION_KEY_PREFIX = "automation"

_IT_MONTHS = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]
_IT_DAYS = [
    "lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica",
]

# Both rules are UTILITY — see whatsapp_templates.CATALOGUE. feedback_v2 is
# the review request (platform + link); feedback_v1 stays in the catalogue only
# because Meta cannot un-approve an already-approved body, so the old template
# is retired rather than edited.
_RULE_TEMPLATE = {"feedback": "feedback_v2", "reminder": "reminder_v1"}

_PLATFORM_LABELS = {
    "google": "Google",
    "facebook": "Facebook",
    "instagram": "Instagram",
}
_GENERAL_PLATFORM = "un canale a tua scelta"
_NO_LINK_CTA = "rispondendo a questo messaggio"


def _feedback_date(when: datetime) -> str:
    """The visit date, Italian: "12 marzo". A fact, not a generation."""
    local = when.astimezone(SALON_TZ)
    return f"{local.day} {_IT_MONTHS[local.month - 1]}"


def _reminder_when(when: datetime) -> str:
    """The appointment moment, Italian: "giovedì 14 alle 10:30"."""
    local = when.astimezone(SALON_TZ)
    return f"{_IT_DAYS[local.weekday()]} {local.day} alle {local:%H:%M}"


def render_variables(
    template_key: str, row: dict,
    *, platform: str = "general", link: str = "",
) -> dict[str, str]:
    """The variables the UTILITY templates need, from the due-work row.

    {{1}} name, {{2}} salon, {{3}} appointment time, {{4}} services. Nothing
    is generated: each value is a database fact, formatted for the approved
    frame. That is what keeps these templates UTILITY.

    The review request (`feedback_v2`) also fills {{5}} (the platform the
    owner picked, or a neutral phrase when "general") and {{6}} (the review
    link, or a plain CTA when the owner left it empty).
    """
    when = row["appointment_at"]
    if template_key.startswith("feedback"):
        date_text = _feedback_date(when)
    else:
        date_text = _reminder_when(when)
    out = {
        "1": clean_variable(row["first_name"] or ""),
        "2": clean_variable(row["shop_name"] or ""),
        "3": clean_variable(date_text),
        "4": clean_variable(row["service_names"] or ""),
    }
    if template_key == "feedback_v2":
        out["5"] = _PLATFORM_LABELS.get(platform, _GENERAL_PLATFORM)
        out["6"] = clean_variable(link) if link else _NO_LINK_CTA
    return out


def _quality_blocks_marketing(sender: dict) -> bool:
    """YELLOW/RED quality rating pauses automated MARKETING.

    Both rules this plan ships are UTILITY, so this never fires today — it
    exists so Plan 3's automated win-back (MARKETING) can land beside this
    code without having to remember to add the pause.
    """
    return (sender.get("quality_rating") or "GREEN") in ("YELLOW", "RED")


async def run_automations(*, settings) -> dict:
    """Fire every enabled rule across all shops. One bad shop cannot abort the rest."""
    counts = {"feedback": 0, "reminder": 0, "skipped_shops": 0, "errors": 0}

    rules = await aq.list_enabled_rules()
    for rule in rules:
        shop_id: UUID = rule["shop_id"]
        rule_key: str = rule["rule_key"]
        try:
            sender = await wq.get_sender(shop_id)
            if not sender or sender.get("status") != "online":
                counts["skipped_shops"] += 1
                continue

            template_key = _RULE_TEMPLATE[rule_key]
            template = await wq.get_template(shop_id, template_key)
            if not template or template.get("status") != "approved":
                # Sending on an unapproved template fails at Meta and still
                # burns a queue slot; refuse here where the owner can see why
                # (the tile disables the toggle on the same signal).
                continue

            if _quality_blocks_marketing(sender) and template.get("category") == "MARKETING":
                # YELLOW/RED pauses automated MARKETING. Both rules here are
                # UTILITY, so both continue regardless; the gate stays so Plan
                # 3's win-back lands beside it without remembering to add it.
                continue

            if rule_key == "feedback":
                due = await aq.due_feedback(
                    shop_id, rule["params"].get("hours_after", 24)
                )
            else:
                due = await aq.due_reminders(
                    shop_id, rule["params"].get("min_no_shows", 0)
                )

            for row in due:
                variables = render_variables(
                    template_key, row,
                    platform=str(rule["params"].get("platform", "general")),
                    link=str(rule["params"].get("link", "")),
                )
                try:
                    message_id = await wq.enqueue(
                        shop_id=shop_id,
                        customer_id=row["customer_id"],
                        campaign_key=(
                            f"{AUTOMATION_KEY_PREFIX}:{rule_key}:{row['appointment_id']}"
                        ),
                        to_phone=normalize_e164(row["phone"]),
                        from_number=sender["phone_number"],
                        template_name=template["name"],
                        template_language=template["language"],
                        variables=variables,
                        preview=render(template_key, variables),
                        scheduled_at=datetime.now(tz=SALON_TZ),
                    )
                except Exception:  # noqa: BLE001 — one bad row retries, not a dead shop
                    logger.exception(
                        "whatsapp_automations.enqueue_failed shop=%s rule=%s appt=%s",
                        shop_id, rule_key, row["appointment_id"],
                    )
                    counts["errors"] += 1
                    continue

                # Enqueue first, record second: a crash between them re-sends
                # once (recoverable), where recording first would silently drop
                # a message forever. A `None` from enqueue means the unique
                # index already had this appointment — a previous crashed tick
                # enqueued it and send_due will deliver it — so record anyway.
                await aq.record_automation_send(
                    shop_id=shop_id, rule_key=rule_key,
                    appointment_id=row["appointment_id"],
                )
                if message_id is not None:
                    counts[rule_key] += 1
        except Exception:  # noqa: BLE001 — one shop must not abort the others
            logger.exception(
                "whatsapp_automations.shop_failed shop=%s rule=%s", shop_id, rule_key,
            )
            counts["errors"] += 1

    return counts
