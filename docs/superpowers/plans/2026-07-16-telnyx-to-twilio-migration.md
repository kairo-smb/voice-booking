# Telnyx → Twilio Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the voice platform's telephony provider from Telnyx to Twilio, provisioning Estonia (EE) Mobile numbers instead of Telnyx Italy local numbers, with no live shops to migrate (clean pre-launch swap).

**Architecture:** Restore and adapt the original Twilio client/webhook code that existed before the 2026-06-06 Telnyx swap (recoverable from git history at `df9c53a^`), rather than writing from scratch. Add Twilio request-signature verification (a gap that existed under Telnyx too, never implemented). Drop the Telnyx-specific `pending_review` webhook — Twilio's one-time regulatory Bundle approval happens out-of-band, so per-shop provisioning becomes synchronous.

**Tech Stack:** FastAPI, `twilio` Python SDK (already a dependency, `>=9.0`, importable in this environment as 9.10.9), asyncpg/Neon, pytest + pytest-asyncio + httpx `ASGITransport`.

**Reference:** `docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md`

---

### Task 1: Swap Settings from Telnyx to Twilio

**Files:**
- Modify: `booking_engine/config.py`

- [ ] **Step 1: Edit `Settings`**

Replace:

```python
    # Public base URL used for constructing Telnyx webhook URLs
    public_base_url: str = ""
    # Telnyx
    telnyx_api_key: str = ""
    telnyx_public_key: str = ""
    telnyx_default_country: str = "IT"
```

With:

```python
    # Public base URL used for constructing Twilio webhook URLs
    public_base_url: str = ""
    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_default_country: str = "EE"
    # One-time regulatory Bundle (KYC) for the shared Kairo entity, reused
    # across every provisioned DID — see the migration spec linked above.
    twilio_bundle_sid: str = ""
    twilio_address_sid: str = ""
```

- [ ] **Step 2: Commit**

```bash
git add booking_engine/config.py
git commit -m "feat(voice): swap Settings from Telnyx to Twilio fields"
```

---

### Task 2: Restore and adapt the Twilio numbers client

**Files:**
- Create: `booking_engine/clients/twilio_numbers.py`
- Delete: `booking_engine/clients/telnyx_numbers.py`
- Test: `tests/voice_gateway/test_twilio_numbers_client.py` (create)
- Delete: `tests/voice_gateway/test_telnyx_numbers_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/voice_gateway/test_twilio_numbers_client.py`:

```python
"""Tests for the Twilio Numbers API client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from booking_engine.clients.twilio_numbers import (
    search_available_numbers,
    purchase_number,
    AvailableNumber,
)


@pytest.fixture
def fake_twilio():
    with patch("booking_engine.clients.twilio_numbers.Client") as cls:
        client = MagicMock()
        cls.return_value = client
        yield client


def test_search_available_numbers_returns_typed(fake_twilio):
    fake_twilio.available_phone_numbers.return_value.mobile.list.return_value = [
        MagicMock(phone_number="+37251234567",
                  friendly_name="Tallinn",
                  locality="Tallinn",
                  region="Harju"),
        MagicMock(phone_number="+37251234568",
                  friendly_name="Tallinn",
                  locality="Tallinn",
                  region="Harju"),
    ]
    results = search_available_numbers(area_code=None, country="EE", limit=5,
                                       account_sid="AC", auth_token="tok")
    assert len(results) == 2
    assert results[0].phone_number == "+37251234567"
    assert isinstance(results[0], AvailableNumber)
    fake_twilio.available_phone_numbers.assert_called_once_with("EE")


def test_purchase_number_returns_sid(fake_twilio):
    fake_twilio.incoming_phone_numbers.create.return_value = MagicMock(
        sid="PN1234", phone_number="+37251234567"
    )
    result = purchase_number(
        phone_number="+37251234567",
        voice_url="https://api.example.com/voice/twiml/incoming",
        account_sid="AC", auth_token="tok",
    )
    assert result.sid == "PN1234"
    assert result.phone_number == "+37251234567"
    kwargs = fake_twilio.incoming_phone_numbers.create.call_args.kwargs
    assert kwargs["phone_number"] == "+37251234567"
    assert kwargs["voice_url"] == "https://api.example.com/voice/twiml/incoming"
    assert kwargs["voice_method"] == "POST"
    assert "bundle_sid" not in kwargs


def test_purchase_number_attaches_bundle_and_address(fake_twilio):
    fake_twilio.incoming_phone_numbers.create.return_value = MagicMock(
        sid="PN5678", phone_number="+37251234567"
    )
    purchase_number(
        phone_number="+37251234567",
        voice_url="https://api.example.com/voice/twiml/incoming",
        account_sid="AC", auth_token="tok",
        bundle_sid="BU123", address_sid="AD456",
    )
    kwargs = fake_twilio.incoming_phone_numbers.create.call_args.kwargs
    assert kwargs["bundle_sid"] == "BU123"
    assert kwargs["address_sid"] == "AD456"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_twilio_numbers_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'booking_engine.clients.twilio_numbers'`

- [ ] **Step 3: Create the client**

Create `booking_engine/clients/twilio_numbers.py`:

```python
"""Twilio Numbers API client — search and purchase EU mobile numbers.

Used only at onboarding time. After purchase, the number's Voice URL
points to our /voice/twiml/incoming webhook for dynamic per-call routing.
"""
from __future__ import annotations

from dataclasses import dataclass

from twilio.rest import Client


@dataclass
class AvailableNumber:
    phone_number: str
    friendly_name: str
    locality: str
    region: str


@dataclass
class PurchasedNumber:
    sid: str
    phone_number: str


def search_available_numbers(
    *,
    area_code: str | None,
    country: str,
    limit: int,
    account_sid: str,
    auth_token: str,
) -> list[AvailableNumber]:
    """Return up to `limit` available mobile numbers for the country."""
    client = Client(account_sid, auth_token)
    kwargs: dict = {"limit": limit}
    if area_code:
        kwargs["area_code"] = area_code
    found = client.available_phone_numbers(country).mobile.list(**kwargs)
    return [
        AvailableNumber(
            phone_number=n.phone_number,
            friendly_name=n.friendly_name or "",
            locality=n.locality or "",
            region=n.region or "",
        )
        for n in found
    ]


def purchase_number(
    *,
    phone_number: str,
    voice_url: str,
    account_sid: str,
    auth_token: str,
    bundle_sid: str | None = None,
    address_sid: str | None = None,
) -> PurchasedNumber:
    """Purchase a number and bind its Voice URL to the dynamic TwiML webhook.

    `bundle_sid` ties the purchase to the one Kairo-entity regulatory Bundle
    (created once, out-of-band) reused across every DID — required for
    regulated number types like Estonia mobile.
    """
    client = Client(account_sid, auth_token)
    kwargs: dict = {
        "phone_number": phone_number,
        "voice_url": voice_url,
        "voice_method": "POST",
    }
    if bundle_sid:
        kwargs["bundle_sid"] = bundle_sid
    if address_sid:
        kwargs["address_sid"] = address_sid
    result = client.incoming_phone_numbers.create(**kwargs)
    return PurchasedNumber(sid=result.sid, phone_number=result.phone_number)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/voice_gateway/test_twilio_numbers_client.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Delete the Telnyx client and its test**

```bash
git rm booking_engine/clients/telnyx_numbers.py tests/voice_gateway/test_telnyx_numbers_client.py
```

- [ ] **Step 6: Commit**

```bash
git add booking_engine/clients/twilio_numbers.py tests/voice_gateway/test_twilio_numbers_client.py
git commit -m "feat(voice): restore Twilio numbers client for EE mobile, drop Telnyx client"
```

---

### Task 3: Restore and adapt the dynamic TwiML webhook route

**Files:**
- Create: `booking_engine/api/routes/voice_twiml.py`
- Delete: `booking_engine/api/routes/voice_texml.py`
- Modify: `booking_engine/api/app.py:35-36`
- Test: `tests/voice_gateway/test_voice_twiml_webhook.py` (create)
- Delete: `tests/voice_gateway/test_voice_texml_webhook.py`

- [ ] **Step 1: Write the failing test**

Create `tests/voice_gateway/test_voice_twiml_webhook.py`:

```python
"""Tests for the dynamic TwiML webhook (per-call routing decision)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app


def _form_data(called: str = "+37251234567", from_: str = "+393201234567"):
    return {"Called": called, "From": from_, "CallSid": "CA123"}


def _config(enabled: bool = True, fallback: str | None = None):
    return {"shop_id": uuid4(), "enabled": enabled,
            "manual_fallback_number": fallback,
            "manual_fallback_normalized": (fallback or "").replace("+", "")}


def _telephony(salon_existing: str | None = None):
    return {"shop_id": uuid4(),
            "kairo_number": "+37251234567",
            "salon_existing_number": salon_existing,
            "salon_existing_normalized": (salon_existing or "").replace("+", "")}


@pytest.mark.asyncio
async def test_twiml_attaches_when_basket_ok():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": True, "balance": 5000, "detach_reason": None
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert r.status_code == 200
            assert "<Sip>" in r.text
            assert "openai.com" in r.text


@pytest.mark.asyncio
async def test_twiml_routes_to_fallback_when_detached_with_fallback():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True, fallback="+393900000000"))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": False, "balance": 200, "detach_reason": "basket_low"
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert "<Dial" in r.text
            assert "+393900000000" in r.text
            assert "<Sip>" not in r.text


@pytest.mark.asyncio
async def test_twiml_plays_say_when_detached_without_fallback():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True, fallback=None))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": False, "balance": 0, "detach_reason": "basket_low"
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert "<Say" in r.text
            assert "<Dial" not in r.text


@pytest.mark.asyncio
async def test_twiml_loop_safety_falls_back_to_say():
    # fallback equals the salon's forwarded number -> loop risk -> play Say instead
    salon_existing = "+393900000000"
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony(salon_existing=salon_existing))), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True, fallback=salon_existing))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": False, "balance": 0, "detach_reason": "basket_low"
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert "<Say" in r.text
            assert "<Dial" not in r.text


@pytest.mark.asyncio
async def test_twiml_returns_say_when_unknown_number():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=None)):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert r.status_code == 200
            assert "<Say" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_voice_twiml_webhook.py -v`
Expected: FAIL — `404 Not Found` (no `/voice/twiml/incoming` route registered yet)

- [ ] **Step 3: Create the route**

Create `booking_engine/api/routes/voice_twiml.py`:

```python
"""Dynamic TwiML webhook — per-call routing decision.

Twilio calls this on every inbound call. We respond with either:
- <Dial><Sip>OpenAI SIP endpoint</Sip></Dial> when AI is attached
- <Dial>fallback_number</Dial> when AI is detached and fallback is set
- <Say>recorded message</Say> otherwise

The decision is based on shop_config.enabled and basket balance vs. min_session_reserve.
"""
from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Response

from booking_engine.config import Settings, get_settings
from booking_engine.db.voice_config_queries import get_config
from booking_engine.db.voice_telephony_queries import get_telephony_by_kairo_number
from booking_engine.services.phone_normalize import digits_only
from booking_engine.services.token_meter import decide_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice/twiml", tags=["voice-twiml"])


_SAY_UNAVAILABLE = (
    "Salve, in questo momento il salone non è raggiungibile. "
    "La preghiamo di richiamare. Grazie."
)


def _wrap(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


def _say_unavailable() -> Response:
    return _wrap(f'<Say voice="alice" language="it-IT">{_SAY_UNAVAILABLE}</Say>')


def _dial_sip(shop_id: UUID, settings: Settings) -> Response:
    sip_uri = (
        f"sip:{settings.openai_sip_project_id};X-Shop-Id={shop_id}"
        f"@sip.api.openai.com"
    )
    return _wrap(f"<Dial><Sip>{sip_uri}</Sip></Dial>")


def _dial_fallback(number: str) -> Response:
    return _wrap(f'<Dial timeout="25">{number}</Dial>')


@router.post("/incoming")
async def incoming(
    settings: Annotated[Settings, Depends(get_settings)],
    Called: str = Form(...),
    From: str = Form(default=""),
    CallSid: str = Form(default=""),
) -> Response:
    """Twilio fires this on every inbound call."""
    telephony = await get_telephony_by_kairo_number(Called)
    if not telephony:
        logger.warning("twiml.incoming: unknown number %s sid=%s", Called, CallSid)
        return _say_unavailable()

    shop_id: UUID = telephony["shop_id"]
    config = await get_config(shop_id)
    enabled = bool(config and config.get("enabled"))
    fallback = (config or {}).get("manual_fallback_number")
    fallback_normalized = digits_only(fallback)
    salon_existing_normalized = telephony.get("salon_existing_normalized") or ""

    decision = await decide_session(
        shop_id=shop_id, enabled=enabled,
        min_reserve=settings.voice_min_session_reserve_tokens,
    )

    if decision.attach:
        return _dial_sip(shop_id, settings)

    # Detached — pick the safest available fallback
    if fallback and fallback_normalized and fallback_normalized != salon_existing_normalized:
        return _dial_fallback(fallback)

    if fallback and fallback_normalized == salon_existing_normalized:
        logger.warning(
            "twiml.incoming: fallback equals forwarded number — loop risk, "
            "playing Say. shop_id=%s sid=%s", shop_id, CallSid,
        )

    return _say_unavailable()
```

- [ ] **Step 4: Register the route in `app.py`**

In `booking_engine/api/app.py`, replace:

```python
    from booking_engine.api.routes import voice_texml
    app.include_router(voice_texml.router, prefix="/api/v1")
```

With:

```python
    from booking_engine.api.routes import voice_twiml
    app.include_router(voice_twiml.router, prefix="/api/v1")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/voice_gateway/test_voice_twiml_webhook.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Delete the TeXML route and its test**

```bash
git rm booking_engine/api/routes/voice_texml.py tests/voice_gateway/test_voice_texml_webhook.py
```

- [ ] **Step 7: Commit**

```bash
git add booking_engine/api/routes/voice_twiml.py booking_engine/api/app.py tests/voice_gateway/test_voice_twiml_webhook.py
git commit -m "feat(voice): restore dynamic TwiML webhook route, drop TeXML"
```

---

### Task 4: Add Twilio request-signature verification

This closes a gap that existed under Telnyx too (never implemented — see the deploy readiness brief's gap #7). Twilio's SDK provides `RequestValidator`; the endpoint needs the raw form body (not individually-declared `Form(...)` fields) because the signature is computed over the complete POST body.

**Files:**
- Modify: `booking_engine/api/routes/voice_twiml.py`
- Test: `tests/voice_gateway/test_voice_twiml_webhook.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/voice_gateway/test_voice_twiml_webhook.py`:

```python
from twilio.request_validator import RequestValidator


@pytest.mark.asyncio
async def test_twiml_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/twiml/incoming",
                data=_form_data(),
                headers={"X-Twilio-Signature": "bogus"},
            )
            assert r.status_code == 403


@pytest.mark.asyncio
async def test_twiml_accepts_valid_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    form = _form_data()
    url = "https://api.example.com/api/v1/voice/twiml/incoming"
    signature = RequestValidator("test-token").compute_signature(url, form)
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": True, "balance": 5000, "detach_reason": None
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/twiml/incoming",
                data=form,
                headers={"X-Twilio-Signature": signature},
            )
            assert r.status_code == 200
            assert "<Sip>" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/voice_gateway/test_voice_twiml_webhook.py -v -k signature`
Expected: FAIL — both return 200 regardless of signature (no verification exists yet)

- [ ] **Step 3: Add signature verification to the route**

In `booking_engine/api/routes/voice_twiml.py`, replace the imports:

```python
from fastapi import APIRouter, Depends, Form, Response
```

With:

```python
from fastapi import APIRouter, Depends, Request, Response
from twilio.request_validator import RequestValidator
```

Add this helper after `_dial_fallback`:

```python
def _twilio_signature_valid(request: Request, form: dict, settings: Settings) -> bool:
    """Verify X-Twilio-Signature; no-op until TWILIO_AUTH_TOKEN is provisioned."""
    if not settings.twilio_auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.public_base_url}/api/v1/voice/twiml/incoming"
    return RequestValidator(settings.twilio_auth_token).validate(url, form, signature)
```

Replace the `incoming` endpoint with:

```python
@router.post("/incoming")
async def incoming(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Twilio fires this on every inbound call."""
    form = dict(await request.form())
    if not _twilio_signature_valid(request, form, settings):
        logger.warning("twiml.incoming: invalid Twilio signature")
        return Response(status_code=403)

    called = form.get("Called", "")
    call_sid = form.get("CallSid", "")

    telephony = await get_telephony_by_kairo_number(called)
    if not telephony:
        logger.warning("twiml.incoming: unknown number %s sid=%s", called, call_sid)
        return _say_unavailable()

    shop_id: UUID = telephony["shop_id"]
    config = await get_config(shop_id)
    enabled = bool(config and config.get("enabled"))
    fallback = (config or {}).get("manual_fallback_number")
    fallback_normalized = digits_only(fallback)
    salon_existing_normalized = telephony.get("salon_existing_normalized") or ""

    decision = await decide_session(
        shop_id=shop_id, enabled=enabled,
        min_reserve=settings.voice_min_session_reserve_tokens,
    )

    if decision.attach:
        return _dial_sip(shop_id, settings)

    # Detached — pick the safest available fallback
    if fallback and fallback_normalized and fallback_normalized != salon_existing_normalized:
        return _dial_fallback(fallback)

    if fallback and fallback_normalized == salon_existing_normalized:
        logger.warning(
            "twiml.incoming: fallback equals forwarded number — loop risk, "
            "playing Say. shop_id=%s sid=%s", shop_id, call_sid,
        )

    return _say_unavailable()
```

- [ ] **Step 4: Run all webhook tests to verify they pass**

Run: `pytest tests/voice_gateway/test_voice_twiml_webhook.py -v`
Expected: PASS (7 tests) — the 5 tests from Task 3 still pass because `TWILIO_AUTH_TOKEN` isn't set in their environment, so `_twilio_signature_valid` short-circuits to `True`.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice_twiml.py tests/voice_gateway/test_voice_twiml_webhook.py
git commit -m "feat(voice): verify X-Twilio-Signature on the TwiML webhook"
```

---

### Task 5: Update the provisioning route to use Twilio

**Files:**
- Modify: `booking_engine/api/routes/voice_telephony.py`
- Modify: `tests/voice_gateway/test_voice_telephony_routes.py`

- [ ] **Step 1: Update the failing/changing tests first**

Edit `tests/voice_gateway/test_voice_telephony_routes.py`:

Replace the `stub_secret` fixture:

```python
@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")
    monkeypatch.setenv("TELNYX_API_KEY", "key")
```

With:

```python
@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token123")
```

Replace `test_search_numbers_returns_list`'s import line:

```python
    from booking_engine.clients.telnyx_numbers import AvailableNumber
```

With:

```python
    from booking_engine.clients.twilio_numbers import AvailableNumber
```

Replace `test_provision_writes_telephony_row` entirely:

```python
@pytest.mark.asyncio
async def test_provision_writes_telephony_row():
    from booking_engine.clients.twilio_numbers import PurchasedNumber
    with patch("booking_engine.api.routes.voice_telephony.purchase_number",
               return_value=PurchasedNumber(sid="PN1", phone_number="+37251234567")), \
         patch("booking_engine.db.voice_telephony_queries.upsert_telephony",
               return_value={
                   "shop_id": "00000000-0000-0000-0000-000000000001",
                   "kairo_number": "+37251234567",
                   "kairo_number_sid": "PN1",
                   "setup_path": "new",
                   "salon_existing_number": None,
               }):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/numbers/provision",
                headers=AUTH,
                json={
                    "shop_id": "00000000-0000-0000-0000-000000000001",
                    "phone_number": "+37251234567",
                    "setup_path": "new",
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["data"]["kairo_number"] == "+37251234567"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_voice_telephony_routes.py -v`
Expected: FAIL — `ensure_texml_application` no longer exists on the route module import path, `search_available_numbers`/`purchase_number` still bound to the Telnyx client's signature (`api_key=` kwarg, not `account_sid`/`auth_token`)

- [ ] **Step 3: Update the route**

In `booking_engine/api/routes/voice_telephony.py`, replace the import:

```python
from booking_engine.clients.telnyx_numbers import (
    ensure_texml_application,
    purchase_number,
    search_available_numbers,
)
```

With:

```python
from booking_engine.clients.twilio_numbers import (
    purchase_number,
    search_available_numbers,
)
```

Replace the `search` handler body:

```python
    results = search_available_numbers(
        area_code=area_code,
        country=settings.telnyx_default_country,
        limit=limit,
        api_key=settings.telnyx_api_key,
    )
```

With:

```python
    results = search_available_numbers(
        area_code=area_code,
        country=settings.twilio_default_country,
        limit=limit,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )
```

Replace the entire `provision` handler body (keep the function signature and the leading `salon_existing_number` validation as-is):

```python
    voice_url = f"{settings.public_base_url}/voice/texml/incoming"
    app_id = ensure_texml_application(
        voice_url=voice_url, api_key=settings.telnyx_api_key,
    )
    purchased = purchase_number(
        phone_number=body.phone_number,
        voice_url=voice_url,
        api_key=settings.telnyx_api_key,
        connection_id=app_id,
    )
    # Path 2 (new IT number) starts as pending_review; transitions to active/rejected
    # via the /voice/telnyx/number-status webhook once Telnyx completes the review.
    activation_status = "pending_review" if body.setup_path == "new" else "active"
    row = await q.upsert_telephony(
        shop_id=body.shop_id,
        provider="telnyx",
        kairo_number=purchased.phone_number,
        kairo_number_sid=purchased.sid,
        salon_existing_number=body.salon_existing_number,
        setup_path=body.setup_path,
        activation_status=activation_status,
    )
```

With:

```python
    voice_url = f"{settings.public_base_url}/voice/twiml/incoming"
    purchased = purchase_number(
        phone_number=body.phone_number,
        voice_url=voice_url,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        bundle_sid=settings.twilio_bundle_sid or None,
        address_sid=settings.twilio_address_sid or None,
    )
    # The one Kairo-entity regulatory Bundle is approved once, out-of-band,
    # before any shop onboards (see the migration spec) — every purchase
    # after that is synchronous: it succeeds now (active) or raises outright.
    row = await q.upsert_telephony(
        shop_id=body.shop_id,
        provider="twilio",
        kairo_number=purchased.phone_number,
        kairo_number_sid=purchased.sid,
        salon_existing_number=body.salon_existing_number,
        setup_path=body.setup_path,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/voice_gateway/test_voice_telephony_routes.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice_telephony.py tests/voice_gateway/test_voice_telephony_routes.py
git commit -m "feat(voice): provision Twilio EE mobile numbers, drop pending_review path"
```

---

### Task 5b: Fix the provisioned Voice URL missing its `/api/v1` prefix

**Found during Task 4's code quality review.** `voice_telephony.py`'s `provision()` builds the Voice URL bound to each purchased number as `f"{settings.public_base_url}/voice/twiml/incoming"` — but every router in `app.py` (including `voice_twiml.router`) is mounted at `prefix="/api/v1"`, and `docs/DEPLOY_VOICE_AGENT.md` documents the real endpoint as `/api/v1/voice/twiml/incoming`. Without `/api/v1`, every number Twilio ever provisions points at a path that 404s in this app — inbound calls would never reach `voice_twiml.py` at all, let alone the signature verification Task 4 just added. This bug predates this migration (the same missing prefix existed in the Telnyx-era code this was adapted from) and had no test pinning the value, which is why it went unnoticed until an independent review caught it. Fixing it now rather than filing it away, since it makes the rest of this migration's telephony code unreachable in production otherwise.

**Files:**
- Modify: `booking_engine/api/routes/voice_telephony.py`
- Modify: `tests/voice_gateway/test_voice_telephony_routes.py`

- [ ] **Step 1: Write the failing test**

In `tests/voice_gateway/test_voice_telephony_routes.py`, extend `test_provision_writes_telephony_row` to assert the `voice_url` Twilio actually receives:

```python
@pytest.mark.asyncio
async def test_provision_writes_telephony_row():
    from booking_engine.clients.twilio_numbers import PurchasedNumber
    with patch("booking_engine.api.routes.voice_telephony.purchase_number",
               return_value=PurchasedNumber(sid="PN1", phone_number="+37251234567")) as mock_purchase, \
         patch("booking_engine.db.voice_telephony_queries.upsert_telephony",
               return_value={
                   "shop_id": "00000000-0000-0000-0000-000000000001",
                   "kairo_number": "+37251234567",
                   "kairo_number_sid": "PN1",
                   "setup_path": "new",
                   "salon_existing_number": None,
               }):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/numbers/provision",
                headers=AUTH,
                json={
                    "shop_id": "00000000-0000-0000-0000-000000000001",
                    "phone_number": "+37251234567",
                    "setup_path": "new",
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["data"]["kairo_number"] == "+37251234567"
            assert mock_purchase.call_args.kwargs["voice_url"].endswith(
                "/api/v1/voice/twiml/incoming"
            )
```

(This replaces the existing `test_provision_writes_telephony_row` — same test, with the added `mock_purchase` capture and the final `assert` line.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_voice_telephony_routes.py -v -k test_provision_writes_telephony_row`
Expected: FAIL — `voice_url` currently ends with `/voice/twiml/incoming`, missing `/api/v1`

- [ ] **Step 3: Fix the route**

In `booking_engine/api/routes/voice_telephony.py`, replace:

```python
    voice_url = f"{settings.public_base_url}/voice/twiml/incoming"
```

With:

```python
    voice_url = f"{settings.public_base_url}/api/v1/voice/twiml/incoming"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/voice_gateway/test_voice_telephony_routes.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice_telephony.py tests/voice_gateway/test_voice_telephony_routes.py
git commit -m "fix(voice): include /api/v1 prefix in the provisioned Voice URL"
```

---

### Task 6: Retire the Telnyx number-status webhook (no replacement)

Per the design spec: Twilio's one-time Bundle approval happens out-of-band before any shop onboards, so there's no steady-state async status to react to per number. This route and its test are deleted, not adapted.

**Files:**
- Delete: `booking_engine/api/routes/voice_telnyx_webhooks.py`
- Delete: `tests/voice_gateway/test_voice_telnyx_webhooks.py`
- Modify: `booking_engine/api/app.py:45-46`
- Modify: `booking_engine/db/voice_telephony_queries.py`

**Note found during Task 5's code quality review:** `update_telephony_activation()` in `voice_telephony_queries.py` has exactly one caller — `voice_telnyx_webhooks.py`, deleted in this task. Once that's gone, it's dead code (nothing else sets `activation_status` away from its DB default of `"active"`). Remove it here rather than leave unreachable code behind.

- [ ] **Step 1: Remove the route registration**

In `booking_engine/api/app.py`, delete these two lines:

```python
    from booking_engine.api.routes import voice_telnyx_webhooks
    app.include_router(voice_telnyx_webhooks.router, prefix="/api/v1")
```

- [ ] **Step 2: Delete the route and its test**

```bash
git rm booking_engine/api/routes/voice_telnyx_webhooks.py tests/voice_gateway/test_voice_telnyx_webhooks.py
```

- [ ] **Step 3: Remove the now-dead `update_telephony_activation` function**

In `booking_engine/db/voice_telephony_queries.py`, delete this function (its only caller no longer exists after Step 2):

```python
async def update_telephony_activation(
    *,
    kairo_number: str,
    activation_status: str,
    regulatory_rejection_reason: str | None = None,
    activated_at=None,  # datetime | None
) -> dict | None:
    from datetime import datetime, timezone
    ts = activated_at or (datetime.now(timezone.utc) if activation_status == "active" else None)
    return await execute_one(
        """
        UPDATE voice_agent.shop_telephony
        SET activation_status = $2,
            regulatory_rejection_reason = $3,
            activated_at = $4
        WHERE kairo_number = $1
        RETURNING *
        """,
        kairo_number, activation_status, regulatory_rejection_reason, ts,
    )
```

Leave `upsert_telephony`, `get_telephony`, and `get_telephony_by_kairo_number` untouched — they're still used elsewhere.

- [ ] **Step 4: Run the full voice_gateway suite to confirm nothing else references either deleted symbol**

Run: `pytest tests/voice_gateway/ -v`
Expected: PASS, no import errors

Run: `grep -rn "update_telephony_activation" --include="*.py" booking_engine/ tests/`
Expected: no output (only the definition you just deleted referenced it)

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/app.py booking_engine/db/voice_telephony_queries.py
git commit -m "chore(voice): retire Telnyx number-status webhook, no Twilio equivalent needed"
```

---

### Task 7: Doc/comment cleanup and DB default

Cosmetic references to Telnyx left in docstrings/comments, plus keeping the DB column default honest (not functionally load-bearing — `voice_telephony.py` always passes `provider=` explicitly — but misleading if left as `'telnyx'`).

**Files:**
- Create: `booking_engine/db/sql/09_shop_telephony_twilio_provider.sql`
- Modify: `booking_engine/api/routes/voice_openai.py:4`
- Modify: `booking_engine/services/forwarding_heartbeat.py:5`
- Modify: `booking_engine/services/realtime_session.py:3`
- Modify: `docs/DEPLOY_READINESS_BRIEF.md:115,132`

**Note found during Task 3's code quality review:** `docs/DEPLOY_READINESS_BRIEF.md` twice names the now-deleted `voice_texml.py` as the file that dials OpenAI's SIP endpoint (lines 115 and 132) — a dead pointer once Task 3 lands. This is a dated, point-in-time status brief (live Telnyx-testing notes, funding decisions, etc.) — **don't rewrite its Telnyx narrative wholesale**, that's an accurate historical record of what was actually tested at the time. Only fix the two literal filename references so the doc doesn't point at a deleted file.

- [ ] **Step 1: Add the migration**

Create `booking_engine/db/sql/09_shop_telephony_twilio_provider.sql`:

```sql
-- 09: shop_telephony.provider now defaults to 'twilio' (Telnyx -> Twilio
-- migration). Existing rows are untouched; nothing is live yet — see
-- docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md
ALTER TABLE voice_agent.shop_telephony
  ALTER COLUMN provider SET DEFAULT 'twilio';
```

- [ ] **Step 2: Fix the doc comments**

In `booking_engine/api/routes/voice_openai.py`, replace:

```python
identify the shop (from the X-Shop-Id SIP header Telnyx set), assemble the
```

With:

```python
identify the shop (from the X-Shop-Id SIP header we set), assemble the
```

In `booking_engine/services/forwarding_heartbeat.py`, replace:

```python
Path 1 = 'forward' setup (default, ~70-80% of shops): the salon's existing
carrier number forwards to our Telnyx DID. This heartbeat detects when that
```

With:

```python
Path 1 = 'forward' setup (default, ~70-80% of shops): the salon's existing
carrier number forwards to our Twilio DID. This heartbeat detects when that
```

In `booking_engine/services/realtime_session.py`, replace:

```python
OpenAI native SIP: Telnyx dials sip:{project};X-Shop-Id=..@sip.api.openai.com,
```

With:

```python
OpenAI native SIP: Twilio dials sip:{project};X-Shop-Id=..@sip.api.openai.com,
```

In `docs/DEPLOY_READINESS_BRIEF.md`, replace (line 115):

```
1. **Native SIP** (what `voice_texml.py` dials: `sip:$PROJECT_ID@sip.api.openai.com`):
```

With:

```
1. **Native SIP** (what `voice_twiml.py` dials: `sip:$PROJECT_ID@sip.api.openai.com`):
```

And replace (line 132, inside the architecture-divergence table):

```
| Entry | `voice_texml.py` `<Dial><Sip>` to OpenAI | `/realtime/token` ephemeral `client_secrets` (WebRTC/browser) |
```

With:

```
| Entry | `voice_twiml.py` `<Dial><Sip>` to OpenAI | `/realtime/token` ephemeral `client_secrets` (WebRTC/browser) |
```

Leave every other Telnyx mention in that file untouched — it's a dated brief, not living docs.

- [ ] **Step 3: Commit**

```bash
git add booking_engine/db/sql/09_shop_telephony_twilio_provider.sql \
        booking_engine/api/routes/voice_openai.py \
        booking_engine/services/forwarding_heartbeat.py \
        booking_engine/services/realtime_session.py \
        docs/DEPLOY_READINESS_BRIEF.md
git commit -m "chore(voice): update Telnyx references to Twilio in comments + DB default"
```

---

### Task 8: Swap infra — dependency, CI secrets, deploy script

**Files:**
- Modify: `booking_engine/requirements.txt`
- Modify: `.github/workflows/deploy.yml:78-79`
- Modify: `scripts/deploy-booking.sh:102-104`

- [ ] **Step 1: Swap the dependency**

In `booking_engine/requirements.txt`, replace:

```
telnyx>=2.0
```

With:

```
twilio>=9.0
```

- [ ] **Step 2: Swap the CI deploy secrets**

In `.github/workflows/deploy.yml`, replace:

```yaml
          TELNYX_API_KEY: ${{ secrets.TELNYX_API_KEY }}
          TELNYX_PUBLIC_KEY: ${{ secrets.TELNYX_PUBLIC_KEY }}
```

With:

```yaml
          TWILIO_ACCOUNT_SID: ${{ secrets.TWILIO_ACCOUNT_SID }}
          TWILIO_AUTH_TOKEN: ${{ secrets.TWILIO_AUTH_TOKEN }}
          TWILIO_BUNDLE_SID: ${{ secrets.TWILIO_BUNDLE_SID }}
```

- [ ] **Step 3: Swap the Lambda env vars in the deploy script**

In `scripts/deploy-booking.sh`, replace:

```bash
ENV_VARS+="TELNYX_API_KEY=${TELNYX_API_KEY:-},"
ENV_VARS+="TELNYX_PUBLIC_KEY=${TELNYX_PUBLIC_KEY:-},"
ENV_VARS+="TELNYX_DEFAULT_COUNTRY=${TELNYX_DEFAULT_COUNTRY:-IT},"
```

With:

```bash
ENV_VARS+="TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID:-},"
ENV_VARS+="TWILIO_AUTH_TOKEN=${TWILIO_AUTH_TOKEN:-},"
ENV_VARS+="TWILIO_DEFAULT_COUNTRY=${TWILIO_DEFAULT_COUNTRY:-EE},"
ENV_VARS+="TWILIO_BUNDLE_SID=${TWILIO_BUNDLE_SID:-},"
```

- [ ] **Step 4: Commit**

```bash
git add booking_engine/requirements.txt .github/workflows/deploy.yml scripts/deploy-booking.sh
git commit -m "chore(voice): swap deploy infra from Telnyx to Twilio secrets/env"
```

**Not a code step — manual ops action, note it and move on:** the GitHub repo secrets `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_BUNDLE_SID` need to be added (replacing `TELNYX_API_KEY`/`TELNYX_PUBLIC_KEY`) before the CI deploy job can run successfully. `TWILIO_BUNDLE_SID` only exists once the one-time Kairo entity Bundle is created and approved in the Twilio Console (see the migration spec) — until then, leave it unset; `purchase_number` treats it as optional.

---

### Task 9: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full unit test suite**

Run: `pytest tests/voice_gateway/ tests/booking_engine/ -v --ignore=tests/live_db --ignore=tests/integration`
Expected: PASS, no `telnyx` import errors anywhere

- [ ] **Step 2: Confirm no remaining functional Telnyx references**

Run: `grep -rn "telnyx" --include="*.py" booking_engine/ tests/ | grep -v Binary`
Expected: no output (all Telnyx code deleted; the design spec doc is the only remaining reference, and that's docs not code)

- [ ] **Step 3: Confirm the app still boots and registers routes**

Run: `python3 -c "from booking_engine.api.app import create_app; app = create_app(); print(sorted(r.path for r in app.routes if 'voice' in r.path))"`
Expected: list includes `/api/v1/voice/twiml/incoming` and `/api/v1/voice/numbers/...`, does NOT include `/api/v1/voice/texml/incoming` or `/api/v1/voice/telnyx/number-status`

- [ ] **Step 4: Final commit if anything was left uncommitted**

```bash
git status
```

If clean, this task needs no commit — it's verification of work already committed in Tasks 1-8.
