from uuid import uuid4

import pytest

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import whatsapp_receipt as wr

SHOP = uuid4()
CUSTOMER = uuid4()
PHONE = "+393331112222"


class Settings:
    meta_kairo_waba_id = "KAIRO_WABA"
    meta_kairo_token = "kairo-token"
    meta_receipt_sample_url = "https://example.test/sample.pdf"


def _sender(**over):
    row = {
        "status": "online", "phone_number_id": "PN1", "access_token": "tok",
        "phone_number": "+393331112222",
    }
    row.update(over)
    return row


def _template(**over):
    row = {"status": "approved", "name": "purchase_receipt_1", "language": "it"}
    row.update(over)
    return row


def _patch(monkeypatch, *, sender=None, template=None):
    async def _get_sender(shop_id):
        return sender if sender is not None else _sender()
    async def _get_template(shop_id, key):
        return template if template is not None else _template()
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "get_template", _get_template)


@pytest.mark.asyncio
async def test_send_receipt_refuses_an_offline_sender(monkeypatch):
    _patch(monkeypatch, sender={"status": "offline"})

    result = await wr.send_receipt(
        shop_id=SHOP, customer_id=CUSTOMER, phone=PHONE, reference="REF1",
        filename="ricevuta.pdf", pdf_base64="aGVsbG8=", initiated_by=None,
        settings=Settings(),
    )
    assert result == {"ok": False, "error": "sender_not_online"}


@pytest.mark.asyncio
async def test_send_receipt_refuses_an_unapproved_template(monkeypatch):
    _patch(monkeypatch, template=_template(status="pending"))

    result = await wr.send_receipt(
        shop_id=SHOP, customer_id=CUSTOMER, phone=PHONE, reference="REF1",
        filename="ricevuta.pdf", pdf_base64="aGVsbG8=", initiated_by=None,
        settings=Settings(),
    )
    assert result["error"] == "template_pending"


@pytest.mark.asyncio
async def test_send_receipt_uploads_and_sends_the_document(monkeypatch):
    _patch(monkeypatch)
    calls = {}

    async def _upload(**kw):
        calls["upload"] = kw
        return "MEDIA1"
    async def _send_doc(**kw):
        calls["send"] = kw
        return "WAMID1"
    async def _enqueue(**kw):
        calls["enqueue"] = kw
        return uuid4()
    async def _mark_sent(**kw):
        calls["mark_sent"] = kw

    monkeypatch.setattr(meta, "upload_media", _upload)
    monkeypatch.setattr(meta, "send_document_template", _send_doc)
    monkeypatch.setattr(wq, "enqueue", _enqueue)
    monkeypatch.setattr(wq, "mark_sent", _mark_sent)

    result = await wr.send_receipt(
        shop_id=SHOP, customer_id=CUSTOMER, phone=PHONE, reference="REF1",
        filename="ricevuta.pdf", pdf_base64="aGVsbG8=", initiated_by=None,
        settings=Settings(),
    )

    assert result["ok"] is True
    assert calls["upload"]["content"] == b"hello"
    assert calls["send"]["media_id"] == "MEDIA1"
    assert calls["send"]["to"] == PHONE
    assert calls["send"]["name"] == "purchase_receipt_1"
    # A receipt is not a campaign: no campaign_key, so re-sends stay legal.
    assert calls["enqueue"]["campaign_key"] is None
    assert calls["mark_sent"]["provider_sid"] == "WAMID1"


@pytest.mark.asyncio
async def test_ensure_receipt_template_fails_closed_without_sample_url(monkeypatch):
    async def _get_sender(shop_id):
        return {"waba_id": "WABA1", "access_token": "tok"}
    async def _get_template(shop_id, key):
        return None
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "get_template", _get_template)

    no_sample = Settings()
    no_sample.meta_receipt_sample_url = ""

    result = await wr.ensure_receipt_template(shop_id=SHOP, settings=no_sample)
    assert result == {"ok": False, "error": "receipt_sample_not_configured"}


def _patch_ensure(monkeypatch, *, kairo_body):
    async def _get_sender(shop_id):
        return {"waba_id": "WABA1", "access_token": "tok"}
    async def _get_template(shop_id, key):
        return None
    async def _fetch(**kw):
        return meta.TemplateStatus(status="approved", rejection_reason=None,
                                   body=kairo_body)
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "get_template", _get_template)
    monkeypatch.setattr(meta, "fetch_template", _fetch)


@pytest.mark.asyncio
async def test_ensure_receipt_template_creates_it_and_records_the_body(monkeypatch):
    from booking_engine.services.messaging import whatsapp_templates as wt

    _patch_ensure(monkeypatch, kairo_body=wt.RECEIPT_TEMPLATE_BODY)
    calls = {}

    async def _create(**kw):
        calls["create"] = kw
        return "TPLDOC", "pending"
    async def _upsert(**kw):
        calls["upsert"] = kw
        return kw
    monkeypatch.setattr(meta, "create_document_template", _create)
    monkeypatch.setattr(wq, "upsert_template", _upsert)

    result = await wr.ensure_receipt_template(shop_id=SHOP, settings=Settings())

    assert result == {"ok": True, "created": 1}
    assert calls["create"]["example_url"] == "https://example.test/sample.pdf"
    assert calls["upsert"]["body_hash"] == wt.body_hash(wt.RECEIPT_TEMPLATE_BODY)


@pytest.mark.asyncio
async def test_ensure_receipt_template_refuses_a_body_kairo_never_approved(monkeypatch):
    """Same rule as the catalogue gate: an approved *name* is not an approved
    body. Edit the receipt copy here and the customer WABAs wait for Kairo's
    re-approval rather than receiving unreviewed text."""
    _patch_ensure(monkeypatch, kairo_body="una ricevuta diversa")

    async def _create(**kw):
        raise AssertionError("Kairo's WABA has not approved this copy")
    monkeypatch.setattr(meta, "create_document_template", _create)

    result = await wr.ensure_receipt_template(shop_id=SHOP, settings=Settings())
    assert result == {"ok": False, "error": "not_ready"}
