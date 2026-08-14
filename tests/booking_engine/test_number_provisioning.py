import pytest
from types import SimpleNamespace
from uuid import uuid4

from booking_engine.clients.twilio_numbers import AvailableNumber, PurchasedNumber
from booking_engine.clients.twilio_regulatory import Violation
from booking_engine.db import voice_telephony_queries as q
from booking_engine.services import number_provisioning


@pytest.mark.asyncio
async def test_insert_telephony_does_not_overwrite_an_existing_number(monkeypatch):
    """The PK guarantees one row; nothing guaranteed one PURCHASE.

    A second insert must lose, not overwrite — an overwritten row leaves the
    previously bought number billed by Twilio forever with nothing referencing
    it. The caller uses the None return as its signal to hand the number back.
    """
    captured = {}

    async def fake_execute_one(sql, *args):
        captured["sql"] = sql
        return None            # simulate: a row already existed

    monkeypatch.setattr(q, "execute_one", fake_execute_one)

    row = await q.insert_telephony(
        shop_id=uuid4(), provider="twilio", kairo_number="+37251234567",
        kairo_number_sid="PN1", salon_existing_number=None, setup_path="new",
    )

    assert row is None
    assert "DO NOTHING" in captured["sql"], "must not overwrite an existing row"
    assert "DO UPDATE" not in captured["sql"]


@pytest.mark.asyncio
async def test_insert_telephony_returns_the_row_when_it_wins(monkeypatch):
    async def fake_execute_one(sql, *args):
        return {"shop_id": args[0], "kairo_number": args[2]}
    monkeypatch.setattr(q, "execute_one", fake_execute_one)

    shop = uuid4()
    row = await q.insert_telephony(
        shop_id=shop, provider="twilio", kairo_number="+37251234567",
        kairo_number_sid="PN1", salon_existing_number=None, setup_path="new",
    )
    assert row["shop_id"] == shop


def test_upsert_telephony_still_exists_for_status_updates():
    """upsert_telephony is NOT removed — legitimate callers update an existing
    row's activation status. Only the provisioning path becomes insert-only."""
    assert hasattr(q, "upsert_telephony")


# ---------------------------------------------------------------------------
# number_provisioning.submit_request / provision_approved
# ---------------------------------------------------------------------------


def _settings():
    return SimpleNamespace(
        twilio_account_sid="AC1",
        twilio_auth_token="tok",
        twilio_default_country="EE",
        twilio_address_sid="",
        public_base_url="https://api.example.com",
    )


def _patch_happy_chain_up_to_evaluate(monkeypatch, *, shop_id):
    """Wire a full, successful End-User -> document -> Bundle -> assignments
    chain, stopping short of `evaluate` — the caller patches that (and
    whatever comes after) to control compliant vs noncompliant."""

    async def fake_get_request(sid):
        assert sid == shop_id
        return None

    monkeypatch.setattr(number_provisioning.number_request_queries, "get_request", fake_get_request)

    async def fake_get_regulation_sid(**kwargs):
        return "RN26dca8d0e541a6c8fce4abd46e518506"

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "get_regulation_sid", fake_get_regulation_sid)

    async def fake_upsert_request(**kwargs):
        return {}

    monkeypatch.setattr(number_provisioning.number_request_queries, "upsert_request", fake_upsert_request)

    sids_set = []

    async def fake_set_sids(**kwargs):
        sids_set.append(kwargs)

    monkeypatch.setattr(number_provisioning.number_request_queries, "set_sids", fake_set_sids)

    async def fake_create_end_user(**kwargs):
        return "EU1"

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "create_end_user", fake_create_end_user)

    async def fake_upload_document(**kwargs):
        return "DOC1"

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "upload_document", fake_upload_document)

    async def fake_create_bundle(**kwargs):
        return "BU1"

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "create_bundle", fake_create_bundle)

    async def fake_assign_item(**kwargs):
        return None

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "assign_item", fake_assign_item)

    return sids_set


@pytest.mark.asyncio
async def test_submit_request_provisioned_short_circuits_with_zero_twilio_calls(monkeypatch):
    """A request already provisioned must not touch Twilio at all."""
    shop_id = uuid4()
    twilio_calls = []

    async def fake_get_request(sid):
        return {"shop_id": sid, "status": "provisioned"}

    monkeypatch.setattr(number_provisioning.number_request_queries, "get_request", fake_get_request)

    async def fake_get_regulation_sid(**kwargs):
        twilio_calls.append("get_regulation_sid")
        return "RN1"

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "get_regulation_sid", fake_get_regulation_sid)

    result = await number_provisioning.submit_request(
        shop_id=shop_id, business_name="Salon Bella", contact_email="owner@salon.it",
        filename="visura.pdf", content=b"pdf-bytes", content_type="application/pdf",
        settings=_settings(),
    )

    assert result == {"ok": True, "status": "provisioned"}
    assert twilio_calls == []


@pytest.mark.asyncio
async def test_submit_request_no_regulation_creates_nothing(monkeypatch):
    """No regulation on record -> clean error, no End-User/document/Bundle."""
    shop_id = uuid4()
    created = []

    async def fake_get_request(sid):
        return None

    monkeypatch.setattr(number_provisioning.number_request_queries, "get_request", fake_get_request)

    async def fake_get_regulation_sid(**kwargs):
        return None

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "get_regulation_sid", fake_get_regulation_sid)

    async def fake_upsert_request(**kwargs):
        created.append("upsert_request")
        return {}

    monkeypatch.setattr(number_provisioning.number_request_queries, "upsert_request", fake_upsert_request)

    async def fake_create_end_user(**kwargs):
        created.append("create_end_user")
        return "EU1"

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "create_end_user", fake_create_end_user)

    result = await number_provisioning.submit_request(
        shop_id=shop_id, business_name="Salon Bella", contact_email="owner@salon.it",
        filename="visura.pdf", content=b"pdf-bytes", content_type="application/pdf",
        settings=_settings(),
    )

    assert result == {"ok": False, "error": "no_regulation"}
    assert created == []


@pytest.mark.asyncio
async def test_submit_request_noncompliant_stores_violations_and_does_not_submit(monkeypatch):
    shop_id = uuid4()
    _patch_happy_chain_up_to_evaluate(monkeypatch, shop_id=shop_id)

    violations = [Violation(friendly_name="Business name", description="Required field missing")]

    async def fake_evaluate(**kwargs):
        return False, violations

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "evaluate", fake_evaluate)

    submit_calls = []

    async def fake_submit_for_review(**kwargs):
        submit_calls.append(kwargs)

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "submit_for_review", fake_submit_for_review)

    statuses = []

    async def fake_set_status(**kwargs):
        statuses.append(kwargs)

    monkeypatch.setattr(number_provisioning.number_request_queries, "set_status", fake_set_status)

    result = await number_provisioning.submit_request(
        shop_id=shop_id, business_name="Salon Bella", contact_email="owner@salon.it",
        filename="visura.pdf", content=b"pdf-bytes", content_type="application/pdf",
        settings=_settings(),
    )

    expected_errors = [{"friendly_name": "Business name", "description": "Required field missing"}]
    assert submit_calls == [], "must not submit a noncompliant bundle for review"
    assert statuses == [{"shop_id": shop_id, "status": "draft", "evaluation_errors": expected_errors}]
    assert result == {"ok": True, "status": "draft", "evaluation_errors": expected_errors}


@pytest.mark.asyncio
async def test_submit_request_compliant_submits_and_sets_pending_review(monkeypatch):
    shop_id = uuid4()
    _patch_happy_chain_up_to_evaluate(monkeypatch, shop_id=shop_id)

    async def fake_evaluate(**kwargs):
        return True, []

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "evaluate", fake_evaluate)

    submit_calls = []

    async def fake_submit_for_review(**kwargs):
        submit_calls.append(kwargs)

    monkeypatch.setattr(number_provisioning.twilio_regulatory, "submit_for_review", fake_submit_for_review)

    statuses = []

    async def fake_set_status(**kwargs):
        statuses.append(kwargs)

    monkeypatch.setattr(number_provisioning.number_request_queries, "set_status", fake_set_status)

    result = await number_provisioning.submit_request(
        shop_id=shop_id, business_name="Salon Bella", contact_email="owner@salon.it",
        filename="visura.pdf", content=b"pdf-bytes", content_type="application/pdf",
        settings=_settings(),
    )

    assert len(submit_calls) == 1
    assert statuses == [{"shop_id": shop_id, "status": "pending_review", "submitted_at_now": True}]
    assert result == {"ok": True, "status": "pending_review"}


@pytest.mark.asyncio
async def test_provision_approved_already_provisioned_skips_twilio(monkeypatch):
    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return {"shop_id": sid, "kairo_number": "+37255500000"}

    monkeypatch.setattr(number_provisioning.voice_telephony_queries, "get_telephony", fake_get_telephony)

    def fake_purchase_number(**kwargs):
        raise AssertionError("must not purchase when already provisioned")

    monkeypatch.setattr(number_provisioning.twilio_numbers, "purchase_number", fake_purchase_number)

    statuses = []

    async def fake_set_status(**kwargs):
        statuses.append(kwargs)

    monkeypatch.setattr(number_provisioning.number_request_queries, "set_status", fake_set_status)

    outcome = await number_provisioning.provision_approved(shop_id, settings=_settings())

    assert outcome == "already_provisioned"
    assert statuses == [{"shop_id": shop_id, "status": "provisioned"}]


@pytest.mark.asyncio
async def test_provision_approved_releases_number_on_lost_race(monkeypatch):
    """The money test: a lost insert race must hand the purchased number back."""
    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return None

    monkeypatch.setattr(number_provisioning.voice_telephony_queries, "get_telephony", fake_get_telephony)

    async def fake_get_request(sid):
        return {"shop_id": sid, "bundle_sid": "BU_SALON_1"}

    monkeypatch.setattr(number_provisioning.number_request_queries, "get_request", fake_get_request)

    def fake_search(**kwargs):
        return [AvailableNumber(phone_number="+37255500001", friendly_name="", locality="", region="")]

    monkeypatch.setattr(number_provisioning.twilio_numbers, "search_available_numbers", fake_search)

    def fake_purchase(**kwargs):
        return PurchasedNumber(sid="PN_RACE", phone_number="+37255500001")

    monkeypatch.setattr(number_provisioning.twilio_numbers, "purchase_number", fake_purchase)

    async def fake_insert_telephony(**kwargs):
        return None  # lost the race

    monkeypatch.setattr(number_provisioning.voice_telephony_queries, "insert_telephony", fake_insert_telephony)

    released = []

    def fake_release(**kwargs):
        released.append(kwargs)

    monkeypatch.setattr(number_provisioning.twilio_numbers, "release_number", fake_release)

    outcome = await number_provisioning.provision_approved(shop_id, settings=_settings())

    assert outcome == "raced_released"
    assert released == [{"sid": "PN_RACE", "account_sid": "AC1", "auth_token": "tok"}]


@pytest.mark.asyncio
async def test_provision_approved_happy_path_uses_the_salons_own_bundle(monkeypatch):
    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return None

    monkeypatch.setattr(number_provisioning.voice_telephony_queries, "get_telephony", fake_get_telephony)

    async def fake_get_request(sid):
        return {"shop_id": sid, "bundle_sid": "BU_SALON_2"}

    monkeypatch.setattr(number_provisioning.number_request_queries, "get_request", fake_get_request)

    def fake_search(**kwargs):
        return [AvailableNumber(phone_number="+37255500002", friendly_name="", locality="", region="")]

    monkeypatch.setattr(number_provisioning.twilio_numbers, "search_available_numbers", fake_search)

    purchase_kwargs = {}

    def fake_purchase(**kwargs):
        purchase_kwargs.update(kwargs)
        return PurchasedNumber(sid="PN_HAPPY", phone_number="+37255500002")

    monkeypatch.setattr(number_provisioning.twilio_numbers, "purchase_number", fake_purchase)

    async def fake_insert_telephony(**kwargs):
        return {"shop_id": kwargs["shop_id"], "kairo_number": kwargs["kairo_number"]}

    monkeypatch.setattr(number_provisioning.voice_telephony_queries, "insert_telephony", fake_insert_telephony)

    statuses = []

    async def fake_set_status(**kwargs):
        statuses.append(kwargs)

    monkeypatch.setattr(number_provisioning.number_request_queries, "set_status", fake_set_status)

    pushed = []

    async def fake_send_push(**kwargs):
        pushed.append(kwargs)

    monkeypatch.setattr(number_provisioning.push_notifications, "send_push", fake_send_push)

    outcome = await number_provisioning.provision_approved(shop_id, settings=_settings())

    assert outcome == "provisioned"
    assert purchase_kwargs["bundle_sid"] == "BU_SALON_2", "must use the salon's own bundle, not a shared one"
    assert statuses == [{"shop_id": shop_id, "status": "provisioned"}]
    assert len(pushed) == 1
    assert pushed[0]["event"] == "number_request_approved"
