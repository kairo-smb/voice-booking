"""Live DB tests for voice_agent.number_requests + shop_telephony insert-only guarantee.

Migration 12 (booking_engine/db/sql/12_number_requests.sql) is not yet part of
the seeded local schema, so these tests run only against the real (QA) Neon
branch, where the migration must be applied first — see the task instructions
for the psql command. There is no seed data for this table: every test
discovers a real shop at runtime (never the fictional 02_seed_data.sql IDs)
and cleans up its own rows so the shared QA branch is left exactly as found.
"""
from __future__ import annotations

import json

import pytest

from booking_engine.db.connection import execute, execute_one, execute_void
from booking_engine.db.number_request_queries import (
    get_request,
    list_pending_review,
    set_health,
    set_sids,
    set_status,
    upsert_request,
)
from booking_engine.db.voice_telephony_queries import get_telephony, insert_telephony


# ── shop discovery (no seed data on QA; never use 02_seed_data.sql IDs) ──

@pytest.fixture
async def real_shop_id():
    """First real shop on the QA branch, read-only."""
    row = await execute_one(
        "SELECT id FROM business_app_core.shops ORDER BY id LIMIT 1"
    )
    assert row is not None, "QA branch has no shops — cannot run live number_requests tests"
    return row["id"]


@pytest.fixture
async def second_shop_id(real_shop_id):
    """A second real shop, distinct from real_shop_id."""
    row = await execute_one(
        "SELECT id FROM business_app_core.shops WHERE id != $1 ORDER BY id LIMIT 1",
        real_shop_id,
    )
    assert row is not None, "QA branch needs at least 2 shops for this test"
    return row["id"]


@pytest.fixture
async def shop_without_telephony_id():
    """A real shop that currently has NO voice_agent.shop_telephony row.

    Never touches an existing telephony row — the caller inserts its own
    test-owned row and must delete it in a finally block.
    """
    row = await execute_one(
        """
        SELECT s.id FROM business_app_core.shops s
        LEFT JOIN voice_agent.shop_telephony t ON t.shop_id = s.id
        WHERE t.shop_id IS NULL
        ORDER BY s.id LIMIT 1
        """
    )
    assert row is not None, "QA branch has no shop without a shop_telephony row"
    return row["id"]


# ── cleanup fixtures ──

@pytest.fixture
async def cleanup_number_request_shop_ids():
    """Shop IDs whose voice_agent.number_requests row must be deleted after the test."""
    ids: list = []
    yield ids
    for sid in ids:
        await execute_void(
            "DELETE FROM voice_agent.number_requests WHERE shop_id = $1", sid,
        )


class TestUpsertRequest:
    async def test_upsert_twice_leaves_one_row_second_wins(
        self, db_connection, real_shop_id, cleanup_number_request_shop_ids
    ):
        cleanup_number_request_shop_ids.append(real_shop_id)

        await upsert_request(
            shop_id=real_shop_id,
            business_name="First Salon Name",
            contact_email="first@example.com",
            status="draft",
        )
        await upsert_request(
            shop_id=real_shop_id,
            business_name="Second Salon Name",
            contact_email="second@example.com",
            status="evaluating",
        )

        rows = await execute(
            "SELECT * FROM voice_agent.number_requests WHERE shop_id = $1",
            real_shop_id,
        )
        assert len(rows) == 1
        assert rows[0]["business_name"] == "Second Salon Name"
        assert rows[0]["contact_email"] == "second@example.com"
        assert rows[0]["status"] == "evaluating"


class TestSetSids:
    async def test_set_sids_cannot_blank_already_stored_sid(
        self, db_connection, real_shop_id, cleanup_number_request_shop_ids
    ):
        cleanup_number_request_shop_ids.append(real_shop_id)

        await upsert_request(
            shop_id=real_shop_id,
            business_name="Sid Test Salon",
            contact_email="sids@example.com",
        )

        await set_sids(shop_id=real_shop_id, end_user_sid="EU_stored_first")
        row = await get_request(real_shop_id)
        assert row["end_user_sid"] == "EU_stored_first"
        assert row["document_sid"] is None

        # Second call only supplies document_sid — end_user_sid must survive
        # via COALESCE, not get blanked to NULL.
        await set_sids(shop_id=real_shop_id, document_sid="RD_stored_second")
        row = await get_request(real_shop_id)
        assert row["end_user_sid"] == "EU_stored_first"
        assert row["document_sid"] == "RD_stored_second"


class TestSetStatus:
    async def test_evaluation_errors_roundtrip_through_jsonb(
        self, db_connection, real_shop_id, cleanup_number_request_shop_ids
    ):
        cleanup_number_request_shop_ids.append(real_shop_id)

        await upsert_request(
            shop_id=real_shop_id,
            business_name="Errors Test Salon",
            contact_email="errors@example.com",
        )

        errors = [
            {"friendly_name": "id_document", "description": "Image is blurry"},
            {"friendly_name": "address_document", "description": "Expired"},
        ]
        await set_status(
            shop_id=real_shop_id,
            status="rejected",
            evaluation_errors=errors,
            rejection_reason="Two documents failed evaluation",
        )

        row = await get_request(real_shop_id)
        stored = row["evaluation_errors"]
        parsed = json.loads(stored) if isinstance(stored, str) else stored
        assert parsed == errors
        assert row["status"] == "rejected"
        assert row["rejection_reason"] == "Two documents failed evaluation"

        # Clearing must write NULL, not the JSON string "null".
        await set_status(shop_id=real_shop_id, status="draft", evaluation_errors=None)
        row = await get_request(real_shop_id)
        assert row["evaluation_errors"] is None

    async def test_timestamps_only_stamped_when_milestone_flag_set(
        self, db_connection, real_shop_id, cleanup_number_request_shop_ids
    ):
        cleanup_number_request_shop_ids.append(real_shop_id)

        await upsert_request(
            shop_id=real_shop_id,
            business_name="Timestamp Test Salon",
            contact_email="timestamps@example.com",
        )

        await set_status(shop_id=real_shop_id, status="evaluating")
        row = await get_request(real_shop_id)
        assert row["submitted_at"] is None
        assert row["reviewed_at"] is None

        await set_status(shop_id=real_shop_id, status="pending_review", submitted_at_now=True)
        row = await get_request(real_shop_id)
        assert row["submitted_at"] is not None
        assert row["reviewed_at"] is None


class TestSetHealth:
    async def test_none_status_leaves_health_status_but_stamps_checked_at(
        self, db_connection, shop_without_telephony_id
    ):
        shop_id = shop_without_telephony_id
        row = await insert_telephony(
            shop_id=shop_id,
            provider="twilio",
            kairo_number="+37255500099",
            kairo_number_sid="PN_health_test_sid",
            salon_existing_number=None,
            setup_path="new",
        )
        assert row is not None, "expected shop_without_telephony_id to actually have no row"

        try:
            await set_health(shop_id=shop_id, status="green", detail="ok")
            first = await get_telephony(shop_id)
            assert first["health_status"] == "green"
            assert first["health_checked_at"] is not None

            # A None status means the probe was inconclusive (provider outage) —
            # health_status must NOT flip, but health_checked_at must still move.
            await set_health(shop_id=shop_id, status=None, detail="probe unreachable")
            second = await get_telephony(shop_id)
            assert second["health_status"] == "green"
            assert second["health_checked_at"] > first["health_checked_at"]
            assert second["health_detail"] == "probe unreachable"
        finally:
            await execute_void(
                "DELETE FROM voice_agent.shop_telephony WHERE shop_id = $1", shop_id,
            )


class TestInsertTelephony:
    async def test_insert_twice_returns_row_then_none(
        self, db_connection, shop_without_telephony_id
    ):
        shop_id = shop_without_telephony_id

        first = await insert_telephony(
            shop_id=shop_id,
            provider="twilio",
            kairo_number="+37255500098",
            kairo_number_sid="PN_insert_test_first",
            salon_existing_number=None,
            setup_path="new",
        )
        assert first is not None
        assert first["kairo_number_sid"] == "PN_insert_test_first"

        try:
            # Money guarantee: a second insert for the same shop MUST NOT
            # overwrite the already-purchased number.
            second = await insert_telephony(
                shop_id=shop_id,
                provider="twilio",
                kairo_number="+37255500097",
                kairo_number_sid="PN_insert_test_second",
                salon_existing_number=None,
                setup_path="new",
            )
            assert second is None

            stored = await get_telephony(shop_id)
            assert stored["kairo_number_sid"] == "PN_insert_test_first"
            assert stored["kairo_number"] == "+37255500098"
        finally:
            await execute_void(
                "DELETE FROM voice_agent.shop_telephony WHERE shop_id = $1", shop_id,
            )


class TestListPendingReview:
    async def test_returns_only_pending_review_rows(
        self, db_connection, real_shop_id, second_shop_id, cleanup_number_request_shop_ids
    ):
        cleanup_number_request_shop_ids.append(real_shop_id)
        cleanup_number_request_shop_ids.append(second_shop_id)

        await upsert_request(
            shop_id=real_shop_id,
            business_name="Pending Salon",
            contact_email="pending@example.com",
            status="pending_review",
        )
        await upsert_request(
            shop_id=second_shop_id,
            business_name="Approved Salon",
            contact_email="approved@example.com",
            status="approved",
        )

        rows = await list_pending_review()
        returned_shop_ids = {r["shop_id"] for r in rows}

        assert real_shop_id in returned_shop_ids
        assert second_shop_id not in returned_shop_ids

        # Generic guarantee, not just about our two rows: every row this
        # function returns must actually be pending_review in the DB.
        for r in rows:
            db_row = await get_request(r["shop_id"])
            assert db_row["status"] == "pending_review"
