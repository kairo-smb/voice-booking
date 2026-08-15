"""Live DB tests — the `sms` schema against real Neon.

Mocked tests never catch a schema mismatch. The 2026-07-21 incident in
CLAUDE.md was exactly that: code assuming a column no migration had created,
invisible because the only tests covering it were live-DB ones that had been
silently skipping. These run the real SQL against the real schema.

Deliberately self-contained: the QA branch has never had `02_seed_data.sql`
applied (confirmed 2026-08-12, still true — see CLAUDE.md 2026-07-17), so
`conftest.SHOP_ID` and friends do not exist there and every FK would blow up.
These tests discover a real shop at runtime instead, write only into the
(new, otherwise-empty) `sms.*` tables, and delete what they wrote.

Nothing here mutates a real customer. `withdraw_marketing_consent` is only
exercised with a phone that matches nobody — asserting it reports zero rows —
because the alternative is revoking a real person's marketing consent to make
a test pass.

Run with:  TEST_DATABASE_URL=postgresql://... pytest tests/live_db/test_sms_live.py -v
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from booking_engine.db import sms_queries
from booking_engine.db.connection import execute, execute_one, execute_void

# Phones in a range no real customer can hold, so a stray row can never collide
# with — or be mistaken for — production data.
_TEST_PREFIX = "+3900000"


def _unique_phone() -> str:
    return f"{_TEST_PREFIX}{uuid4().int % 1_000_000:06d}"


@pytest.fixture
async def any_shop_id(db_connection) -> UUID:
    """A real shop on the branch. Read-only — we never modify the shop itself."""
    row = await execute_one("SELECT id FROM business_app_core.shops ORDER BY id LIMIT 1")
    if not row:
        pytest.skip("no shops on this branch")
    return row["id"]


@pytest.fixture
async def cleanup_opt_outs():
    """Delete opt_out rows created by a test, even if it failed mid-way."""
    keys: list[tuple[UUID, str]] = []
    yield keys
    for shop_id, phone in keys:
        await execute_void(
            "DELETE FROM sms.opt_outs WHERE shop_id = $1 AND phone_normalized = $2",
            shop_id, phone,
        )


@pytest.fixture
async def cleanup_outbound():
    ids: list[UUID] = []
    yield ids
    for message_id in ids:
        await execute_void("DELETE FROM sms.outbound_messages WHERE id = $1", message_id)


class TestOptOutLive:
    async def test_record_opt_out_is_idempotent(self, any_shop_id, cleanup_opt_outs):
        """A customer who texts STOP twice must not cause an error.

        Also the ON CONFLICT proof against the real schema: the 2026-07-18
        incident was this exact clause failing because the unique constraint it
        named did not exist on the live table.
        """
        phone = _unique_phone()
        cleanup_opt_outs.append((any_shop_id, phone))

        await sms_queries.record_opt_out(
            shop_id=any_shop_id, phone_normalized=phone, keyword="STOP", raw_body="STOP"
        )
        await sms_queries.record_opt_out(
            shop_id=any_shop_id, phone_normalized=phone, keyword="STOP", raw_body="stop"
        )

        rows = await execute(
            "SELECT id FROM sms.opt_outs WHERE shop_id = $1 AND phone_normalized = $2",
            any_shop_id, phone,
        )
        assert len(rows) == 1
        assert await sms_queries.is_opted_out(any_shop_id, phone) is True

    async def test_unknown_phone_is_not_opted_out(self, any_shop_id):
        assert await sms_queries.is_opted_out(any_shop_id, _unique_phone()) is False

    async def test_withdraw_consent_for_unknown_phone_reports_zero(self, any_shop_id):
        """STOP from a phone matching no customer must work, not raise.

        This is the whole reason sms.opt_outs exists alongside the consent
        columns: imported lists, wrong numbers and deleted customers still have
        to be suppressed.
        """
        updated = await sms_queries.withdraw_marketing_consent(
            shop_id=any_shop_id, phone_normalized=_unique_phone()
        )
        assert updated == 0


class TestCustomerLookupLive:
    async def test_lookup_is_shop_scoped(self, db_connection):
        """Cross-shop isolation: the shop id is part of the WHERE, not a filter
        applied afterwards by the caller."""
        row = await execute_one(
            "SELECT id, shop_id FROM business_app_core.customers ORDER BY id LIMIT 1"
        )
        if not row:
            pytest.skip("no customers on this branch")

        found = await sms_queries.get_customer_for_send(row["shop_id"], row["id"])
        assert found is not None
        assert found["id"] == row["id"]
        # Every column the send path reads must actually come back.
        for column in (
            "full_name", "phone", "phone_normalized",
            "marketing_consent", "marketing_consent_granted_at",
            "marketing_consent_withdrawn_at",
        ):
            assert column in found

        assert await sms_queries.get_customer_for_send(uuid4(), row["id"]) is None


class TestOutboundLive:
    async def test_insert_outbound_round_trips(self, any_shop_id, cleanup_outbound):
        message_id = await sms_queries.insert_outbound(
            shop_id=any_shop_id,
            customer_id=None,
            to_phone=_unique_phone(),
            from_number="+37251234567",
            body="Ciao Giulia, ti aspettiamo! Rispondi STOP per non ricevere piu'.",
            segments=1,
            encoding="gsm7",
            status="suppressed",
            suppressed_reason="no_consent",
        )
        cleanup_outbound.append(message_id)

        row = await execute_one(
            "SELECT segments, encoding, status, suppressed_reason, credits_charged "
            "FROM sms.outbound_messages WHERE id = $1",
            message_id,
        )
        assert row["segments"] == 1
        assert row["encoding"] == "gsm7"
        assert row["status"] == "suppressed"
        assert row["suppressed_reason"] == "no_consent"
        assert row["credits_charged"] is None

    async def test_mark_sent_then_status_webhook_updates_price(
        self, any_shop_id, cleanup_outbound
    ):
        """The send writes an estimated price; Twilio's callback overwrites it
        with the real one, matched on provider_sid."""
        sid = f"SM{uuid4().hex[:30]}"
        message_id = await sms_queries.insert_outbound(
            shop_id=any_shop_id, customer_id=None, to_phone=_unique_phone(),
            from_number="+37251234567", body="test", segments=1,
            encoding="gsm7", status="queued",
        )
        cleanup_outbound.append(message_id)

        await sms_queries.mark_sent(
            message_id=message_id, provider_sid=sid, price_usd=0.093, credits=186
        )
        await sms_queries.update_status_by_sid(
            provider_sid=sid, status="delivered", price_usd=0.0857, error_code=None
        )

        row = await execute_one(
            "SELECT status, price_usd, credits_charged, sent_at "
            "FROM sms.outbound_messages WHERE id = $1",
            message_id,
        )
        assert row["status"] == "delivered"
        assert float(row["price_usd"]) == pytest.approx(0.0857)
        # The credit charge is NOT rewritten by the callback — the shop was
        # billed at send time and a cheaper real price does not retro-refund.
        assert row["credits_charged"] == 186
        assert row["sent_at"] is not None

    async def test_mark_failed_records_the_error(self, any_shop_id, cleanup_outbound):
        message_id = await sms_queries.insert_outbound(
            shop_id=any_shop_id, customer_id=None, to_phone=_unique_phone(),
            from_number="+37251234567", body="test", segments=1,
            encoding="gsm7", status="queued",
        )
        cleanup_outbound.append(message_id)

        await sms_queries.mark_failed(message_id=message_id, error_code="21610")

        row = await execute_one(
            "SELECT status, error_code FROM sms.outbound_messages WHERE id = $1",
            message_id,
        )
        assert row["status"] == "failed"
        assert row["error_code"] == "21610"


class TestSenderNumberLive:
    async def test_sender_number_query_is_valid_sql(self, any_shop_id):
        """Most QA shops have no telephony row, so None is a legitimate result.
        The point is that the query runs against the real schema at all."""
        result = await sms_queries.get_shop_sender_number(any_shop_id)
        assert result is None or isinstance(result, str)

    async def test_unrouteable_number_resolves_to_no_shop(self, db_connection):
        assert await sms_queries.get_shop_by_sender_number("+37259999999") is None
