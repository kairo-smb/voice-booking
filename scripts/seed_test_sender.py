"""Attach Meta's free test number to a shop, for QA — no Embedded Signup.

The onboarding path (start/complete) is deliberately NOT used: it needs a real
salon popup and is frozen. This writes the row `complete()` would have left,
through the same query layer (so the token is sealed with this machine's
WHATSAPP_TOKEN_KEY), using Kairo's system-user token, which has full access to
the test WABA Meta creates with the app.

Runs ON the QA machine (the token and the seal key only exist there), piped
over stdin since scripts/ is not in the image:

    fly ssh console -a kairo-booking-engine-qa -C 'python -' < scripts/seed_test_sender.py

Edit the three constants to target something else. Refuses any shop but the
demo one: every other QA shop holds thousands of real-looking numbers.
"""
import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.config import get_settings
from booking_engine.db import whatsapp_queries as wq
from booking_engine.db.connection import close_connection, init_connection

SHOP_ID = UUID("5e0b3ecf-c85f-478f-9369-859c419e7df0")  # Kairo Demo Parrucchiere
WABA_ID = "1740733127074469"                             # Test WhatsApp Business Account
PHONE_NUMBER_ID = "1334466979746060"                     # +1 555-670-5857


async def main() -> None:
    s = get_settings()
    assert SHOP_ID == UUID("5e0b3ecf-c85f-478f-9369-859c419e7df0"), "demo shop only"
    assert os.environ.get("SENTRY_ENVIRONMENT") == "qa", "QA only"
    token = s.meta_kairo_token
    await init_connection(s)
    try:
        await meta.subscribe_app(waba_id=WABA_ID, token=token)
        number = await meta.get_phone_number(phone_number_id=PHONE_NUMBER_ID, token=token)
        await wq.upsert_sender(shop_id=SHOP_ID, display_name=number.verified_name,
                               source="coexistence")  # only legal value (migration 18)
        now = datetime.now(timezone.utc)
        await wq.set_sender_fields(
            SHOP_ID, access_token=token, waba_id=WABA_ID, phone_number_id=PHONE_NUMBER_ID,
            status="online", phone_number=number.display_phone_number,
            quality_rating=number.quality_rating, messaging_limit=number.messaging_limit,
            throughput_level=number.throughput_level, platform_type=number.platform_type,
            # A test number is not on the Business App: mark the coexistence
            # sync done or the sweep retries it against Meta for 24h.
            contacts_sync_at=now, history_sync_at=now, offline_reason=None,
        )
        row = await wq.get_sender(SHOP_ID)
        print({k: row[k] for k in ("status", "waba_id", "phone_number_id", "phone_number",
                                   "messaging_limit", "platform_type")})
    finally:
        await close_connection()


asyncio.run(main())
