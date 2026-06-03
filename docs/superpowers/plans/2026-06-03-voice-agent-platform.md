# Voice Agent — Platform Implementation Plan (Plan A of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational voice-booking platform layer: schema migrations, Twilio Numbers API integration for Path-1 number provisioning, dynamic TwiML webhook with detach matrix, token meter mapping, and 3-tier warning + auto top-up.

**Architecture:** All work lands in `voice-booking` repo. Booking Engine (AWS Lambda + FastAPI/Mangum) owns the new endpoints. Schema migrations add additive columns to `business_app_core.customers` and `business_app_core.appointments`, and add new tables in `voice_agent`. Twilio is integrated only at call setup time (dynamic TwiML) and number provisioning — no mid-call orchestration.

**Tech Stack:** Python 3.11, FastAPI, asyncpg, Mangum, Twilio Python SDK, libphonenumber, AWS Lambda, Neon Postgres.

**Source spec:** `webapp/docs/superpowers/specs/2026-06-03-voice-agent-realtime-integration-design.md`

---

## File Structure

### New files

- `booking_engine/db/sql/04_voice_agent_v2.sql` — additive migration (customers fields, appointments fields, voice_agent.callback_memos, voice_agent.shop_config expansions, voice_agent.shop_telephony, voice_agent.system_policy seed, voice_agent.auth_events)
- `booking_engine/clients/twilio_numbers.py` — thin wrapper around Twilio Numbers API
- `booking_engine/clients/push_notifications.py` — sends voice_* push events (delegates to existing notification infra)
- `booking_engine/api/routes/voice_telephony.py` — `/voice/numbers/*` endpoints (search, provision, list)
- `booking_engine/api/routes/voice_twiml.py` — `/voice/twiml/incoming` dynamic TwiML webhook
- `booking_engine/api/routes/voice_config.py` — `/voice/config/*` PATCH/GET endpoints for Layer 1
- `booking_engine/services/token_meter.py` — voice-specific debit, warning thresholds, detach decision
- `booking_engine/services/phone_normalize.py` — wrapper around phonenumbers library
- `booking_engine/db/voice_telephony_queries.py` — DB queries for shop_telephony + shop_config
- `booking_engine/db/token_basket_queries.py` — extends existing basket queries with voice debit
- `tests/voice_gateway/test_twilio_numbers_client.py`
- `tests/voice_gateway/test_voice_twiml_webhook.py`
- `tests/voice_gateway/test_voice_config_routes.py`
- `tests/voice_gateway/test_voice_telephony_routes.py`
- `tests/voice_gateway/test_token_meter.py`
- `tests/voice_gateway/test_phone_normalize.py`

### Modified files

- `booking_engine/config.py` — add Twilio creds, OpenAI SIP project ID, push secrets, token rate config
- `booking_engine/app.py` — register new routers
- `requirements.txt` — add `twilio>=9.0`, `phonenumbers>=8.13`

---

### Task 1: Migration scaffold + phone normalization helper

**Files:**
- Create: `booking_engine/db/sql/04_voice_agent_v2.sql`
- Create: `booking_engine/services/phone_normalize.py`
- Create: `tests/voice_gateway/test_phone_normalize.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add phonenumbers dependency**

Append to `requirements.txt`:

```
phonenumbers>=8.13
twilio>=9.0
```

- [ ] **Step 2: Write failing test for phone normalization**

Create `tests/voice_gateway/test_phone_normalize.py`:

```python
"""Tests for the phone number normalization helper."""
from __future__ import annotations

import pytest

from booking_engine.services.phone_normalize import normalize_e164, digits_only


@pytest.mark.parametrize("raw,expected", [
    ("+39 320 123 4567", "+393201234567"),
    ("0039 320 1234567", "+393201234567"),
    ("320.123.4567", "+393201234567"),
    ("320-1234567", "+393201234567"),
    ("3201234567", "+393201234567"),
])
def test_normalize_e164_italian_variants(raw, expected):
    assert normalize_e164(raw, default_region="IT") == expected


def test_normalize_e164_returns_none_for_invalid():
    assert normalize_e164("not a phone", default_region="IT") is None
    assert normalize_e164("", default_region="IT") is None
    assert normalize_e164(None, default_region="IT") is None


@pytest.mark.parametrize("raw,expected", [
    ("+39 320 123 4567", "393201234567"),
    ("320-123-4567", "3201234567"),
    ("", ""),
    (None, ""),
])
def test_digits_only(raw, expected):
    assert digits_only(raw) == expected
```

- [ ] **Step 3: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_phone_normalize.py -v
```

Expected: import errors (module doesn't exist yet).

- [ ] **Step 4: Implement the helper**

Create `booking_engine/services/phone_normalize.py`:

```python
"""Phone number normalization utilities.

Wraps libphonenumber to convert salon-entered phone strings (varying formats)
and Twilio-provided caller IDs into a canonical comparable form.
"""
from __future__ import annotations

import phonenumbers


def normalize_e164(raw: str | None, *, default_region: str = "IT") -> str | None:
    """Return E.164 form (+39...) or None if input is not a valid phone number."""
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def digits_only(raw: str | None) -> str:
    """Return digits-only form for index-based comparison against generated columns."""
    if not raw:
        return ""
    return "".join(c for c in raw if c.isdigit())
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/voice_gateway/test_phone_normalize.py -v
```

Expected: 7 passing.

- [ ] **Step 6: Commit**

```
git add booking_engine/services/phone_normalize.py tests/voice_gateway/test_phone_normalize.py requirements.txt
git commit -m "feat(voice): phone normalization helper using libphonenumber"
```

---

### Task 2: Schema migration — customer additive columns

**Files:**
- Modify: `booking_engine/db/sql/04_voice_agent_v2.sql`
- Create: `tests/voice_gateway/test_migration_04.py`

- [ ] **Step 1: Write failing migration test**

Create `tests/voice_gateway/test_migration_04.py`:

```python
"""Smoke tests for migration 04 — confirms columns and tables exist after migrate."""
from __future__ import annotations

import pytest

from booking_engine.db import connection


@pytest.mark.asyncio
async def test_customers_has_source_column():
    row = await connection.execute_one(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='business_app_core' AND table_name='customers' "
        "AND column_name='source'"
    )
    assert row is not None


@pytest.mark.asyncio
async def test_customers_has_phone_normalized_column():
    row = await connection.execute_one(
        "SELECT column_name, generation_expression FROM information_schema.columns "
        "WHERE table_schema='business_app_core' AND table_name='customers' "
        "AND column_name='phone_normalized'"
    )
    assert row is not None


@pytest.mark.asyncio
async def test_customers_phone_normalized_index_exists():
    row = await connection.execute_one(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname='business_app_core' "
        "AND indexname='customers_shop_phone_normalized_idx'"
    )
    assert row is not None


@pytest.mark.asyncio
async def test_appointments_has_source_and_voice_call_id():
    rows = await connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='business_app_core' AND table_name='appointments' "
        "AND column_name IN ('source', 'voice_call_id', 'confirmation_status')"
    )
    names = {r['column_name'] for r in rows}
    assert names == {'source', 'voice_call_id', 'confirmation_status'}


@pytest.mark.asyncio
async def test_voice_agent_callback_memos_exists():
    row = await connection.execute_one(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='voice_agent' AND table_name='callback_memos'"
    )
    assert row is not None


@pytest.mark.asyncio
async def test_voice_agent_shop_telephony_exists():
    row = await connection.execute_one(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='voice_agent' AND table_name='shop_telephony'"
    )
    assert row is not None


@pytest.mark.asyncio
async def test_voice_agent_shop_config_has_fallback_columns():
    rows = await connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='voice_agent' AND table_name='shop_config' "
        "AND column_name IN ('manual_fallback_number', 'manual_fallback_normalized', "
        "'auto_topup_enabled', 'auto_topup_threshold_tokens', 'enabled')"
    )
    names = {r['column_name'] for r in rows}
    assert {'manual_fallback_number', 'manual_fallback_normalized',
            'auto_topup_enabled', 'auto_topup_threshold_tokens', 'enabled'}.issubset(names)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_migration_04.py -v
```

Expected: 7 failures (none of the columns/tables exist yet).

- [ ] **Step 3: Write the migration SQL**

Create `booking_engine/db/sql/04_voice_agent_v2.sql`:

```sql
-- 04_voice_agent_v2.sql
-- Additive migration to support live voice booking, identity resolution,
-- callback memos, telephony provisioning, and graceful token detach.
-- All changes are additive: no existing column is removed or retyped.

BEGIN;

-- 1. business_app_core.customers — additive
ALTER TABLE business_app_core.customers
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'
    CHECK (source IN ('manual','voice_agent','import','whatsapp')),
  ADD COLUMN IF NOT EXISTS created_by_call_id uuid,
  ADD COLUMN IF NOT EXISTS verified boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS phone_verified boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS phone_normalized text
    GENERATED ALWAYS AS (regexp_replace(coalesce(phone,''),'\D','','g')) STORED,
  ADD COLUMN IF NOT EXISTS household_of uuid REFERENCES business_app_core.customers(id),
  ADD COLUMN IF NOT EXISTS phone_shared_with uuid REFERENCES business_app_core.customers(id);

CREATE INDEX IF NOT EXISTS customers_shop_phone_normalized_idx
  ON business_app_core.customers(shop_id, phone_normalized)
  WHERE phone_normalized != '';

-- 2. business_app_core.appointments — additive
ALTER TABLE business_app_core.appointments
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'
    CHECK (source IN ('manual','voice_agent','whatsapp')),
  ADD COLUMN IF NOT EXISTS voice_call_id uuid,
  ADD COLUMN IF NOT EXISTS confirmation_status text NOT NULL DEFAULT 'confirmed'
    CHECK (confirmation_status IN ('confirmed','pending_sms_confirmation','verification_failed'));

-- 3. voice_agent.shop_telephony
CREATE TABLE IF NOT EXISTS voice_agent.shop_telephony (
  shop_id              uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  provider             text NOT NULL DEFAULT 'twilio',
  kairo_number         text NOT NULL,
  kairo_number_sid     text NOT NULL,
  salon_existing_number       text,
  salon_existing_normalized   text GENERATED ALWAYS AS
    (regexp_replace(coalesce(salon_existing_number,''),'\D','','g')) STORED,
  setup_path           text NOT NULL CHECK (setup_path IN ('new','forward')),
  provisioned_at       timestamptz NOT NULL DEFAULT now(),
  last_inbound_call_at timestamptz
);

-- 4. voice_agent.shop_config — extend if exists, create if missing
CREATE TABLE IF NOT EXISTS voice_agent.shop_config (
  shop_id              uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE voice_agent.shop_config
  ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS display_name text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS greeting_after_disclosure text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS voice_preset text NOT NULL DEFAULT 'warm_female',
  ADD COLUMN IF NOT EXISTS tone_preset text NOT NULL DEFAULT 'warm',
  ADD COLUMN IF NOT EXISTS business_hours jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS answer_mode text NOT NULL DEFAULT 'overflow'
    CHECK (answer_mode IN ('overflow','always_on')),
  ADD COLUMN IF NOT EXISTS overflow_ring_count smallint NOT NULL DEFAULT 4,
  ADD COLUMN IF NOT EXISTS services_to_mention uuid[] NOT NULL DEFAULT '{}'::uuid[],
  ADD COLUMN IF NOT EXISTS retention_days smallint NOT NULL DEFAULT 90,
  ADD COLUMN IF NOT EXISTS manual_fallback_number text,
  ADD COLUMN IF NOT EXISTS manual_fallback_normalized text
    GENERATED ALWAYS AS (regexp_replace(coalesce(manual_fallback_number,''),'\D','','g')) STORED,
  ADD COLUMN IF NOT EXISTS auto_topup_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS auto_topup_threshold_tokens integer,
  ADD COLUMN IF NOT EXISTS auto_topup_package_id uuid;

-- 5. voice_agent.callback_memos
CREATE TABLE IF NOT EXISTS voice_agent.callback_memos (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id         uuid NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
  shop_id         uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  customer_id     uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  caller_phone    text,
  reason          text NOT NULL,
  callback_window text,
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','actioned','dismissed')),
  actioned_by     uuid,
  actioned_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS callback_memos_shop_status_idx
  ON voice_agent.callback_memos(shop_id, status, created_at DESC);

-- 6. voice_agent.auth_events
CREATE TABLE IF NOT EXISTS voice_agent.auth_events (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id               uuid NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
  customer_id           uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  verification_question text NOT NULL,
  caller_answer_excerpt text,
  passed                boolean NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now()
);

-- 7. voice_agent.system_policy
CREATE TABLE IF NOT EXISTS voice_agent.system_policy (
  locale                   text PRIMARY KEY,
  disclosure_text          text NOT NULL,
  recording_consent_prompt text NOT NULL,
  policy_version           integer NOT NULL,
  effective_from           date NOT NULL DEFAULT current_date
);

INSERT INTO voice_agent.system_policy (locale, disclosure_text, recording_consent_prompt, policy_version)
VALUES (
  'it-IT',
  'Salve, questa chiamata è gestita da un assistente vocale automatico. La conversazione verrà trascritta per finalità di servizio. Continuando la chiamata acconsente al trattamento.',
  'Posso aiutarla con la sua prenotazione?',
  1
)
ON CONFLICT (locale) DO NOTHING;

-- 8. voice_agent.calls — extend with shop_id and identity links
ALTER TABLE voice_agent.calls
  ADD COLUMN IF NOT EXISTS shop_id uuid REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS matched_customer_id uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS created_customer_id uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS created_booking_id uuid REFERENCES business_app_core.appointments(id) ON DELETE SET NULL;

-- 9. business_app_core.ai_token_basket_events — additive
ALTER TABLE business_app_core.ai_token_basket_events
  ADD COLUMN IF NOT EXISTS voice_call_id uuid REFERENCES voice_agent.calls(id) ON DELETE SET NULL;

-- Allow 'voice_call' as a source value (CHECK constraint relaxation if present)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ai_token_basket_events_source_check'
  ) THEN
    ALTER TABLE business_app_core.ai_token_basket_events
      DROP CONSTRAINT ai_token_basket_events_source_check;
  END IF;
  ALTER TABLE business_app_core.ai_token_basket_events
    ADD CONSTRAINT ai_token_basket_events_source_check
    CHECK (source IN ('chat','voice_call','manual','system'));
END$$;

COMMIT;
```

- [ ] **Step 4: Apply the migration against the live dev DB**

```
psql $DATABASE_URL -f booking_engine/db/sql/04_voice_agent_v2.sql
```

Expected: `COMMIT` printed, no errors.

- [ ] **Step 5: Run smoke tests against the live DB**

```
DATABASE_URL=<dev-url> pytest tests/voice_gateway/test_migration_04.py -v
```

Expected: 7 passing.

- [ ] **Step 6: Commit**

```
git add booking_engine/db/sql/04_voice_agent_v2.sql tests/voice_gateway/test_migration_04.py
git commit -m "feat(voice): additive schema migration for v2 (telephony, memos, fallback config, identity links)"
```

---

### Task 3: Twilio Numbers API client

**Files:**
- Create: `booking_engine/clients/twilio_numbers.py`
- Create: `tests/voice_gateway/test_twilio_numbers_client.py`
- Modify: `booking_engine/config.py`

- [ ] **Step 1: Extend config with Twilio settings**

Add to `booking_engine/config.py` Settings class:

```python
# Twilio
twilio_account_sid: str = ""
twilio_auth_token: str = ""
twilio_default_country: str = "IT"
# OpenAI SIP routing
openai_sip_project_id: str = ""
# Token meter
voice_kairo_tokens_per_second: int = 18
voice_min_session_reserve_tokens: int = 1500
voice_max_overage_tokens: int = 5000
```

- [ ] **Step 2: Write failing tests for the Twilio client**

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
    fake_twilio.available_phone_numbers.return_value.local.list.return_value = [
        MagicMock(phone_number="+390212345678",
                  friendly_name="Milano",
                  locality="Milano",
                  region="Lombardia"),
        MagicMock(phone_number="+390212345679",
                  friendly_name="Milano",
                  locality="Milano",
                  region="Lombardia"),
    ]
    results = search_available_numbers(area_code="02", country="IT", limit=5,
                                       account_sid="AC", auth_token="tok")
    assert len(results) == 2
    assert results[0].phone_number == "+390212345678"
    assert isinstance(results[0], AvailableNumber)


def test_purchase_number_returns_sid(fake_twilio):
    fake_twilio.incoming_phone_numbers.create.return_value = MagicMock(
        sid="PN1234", phone_number="+390212345678"
    )
    result = purchase_number(
        phone_number="+390212345678",
        voice_url="https://api.example.com/voice/twiml/incoming",
        account_sid="AC", auth_token="tok",
    )
    assert result.sid == "PN1234"
    assert result.phone_number == "+390212345678"
    fake_twilio.incoming_phone_numbers.create.assert_called_once()
    kwargs = fake_twilio.incoming_phone_numbers.create.call_args.kwargs
    assert kwargs["phone_number"] == "+390212345678"
    assert kwargs["voice_url"] == "https://api.example.com/voice/twiml/incoming"
    assert kwargs["voice_method"] == "POST"
```

- [ ] **Step 3: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_twilio_numbers_client.py -v
```

Expected: import errors.

- [ ] **Step 4: Implement the client**

Create `booking_engine/clients/twilio_numbers.py`:

```python
"""Twilio Numbers API client — search and purchase IT geographic numbers.

Used only at onboarding time (Path 1). After purchase, the number's Voice URL
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
    """Return up to `limit` available numbers for the country and area code."""
    client = Client(account_sid, auth_token)
    kwargs = {"limit": limit}
    if area_code:
        kwargs["area_code"] = area_code
    found = client.available_phone_numbers(country).local.list(**kwargs)
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
) -> PurchasedNumber:
    """Purchase a number and bind its Voice URL to the dynamic TwiML webhook."""
    client = Client(account_sid, auth_token)
    result = client.incoming_phone_numbers.create(
        phone_number=phone_number,
        voice_url=voice_url,
        voice_method="POST",
    )
    return PurchasedNumber(sid=result.sid, phone_number=result.phone_number)
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/voice_gateway/test_twilio_numbers_client.py -v
```

Expected: 2 passing.

- [ ] **Step 6: Commit**

```
git add booking_engine/clients/twilio_numbers.py tests/voice_gateway/test_twilio_numbers_client.py booking_engine/config.py
git commit -m "feat(voice): Twilio Numbers API client for IT number search and provisioning"
```

---

### Task 4: Telephony provisioning routes

**Files:**
- Create: `booking_engine/db/voice_telephony_queries.py`
- Create: `booking_engine/api/routes/voice_telephony.py`
- Create: `tests/voice_gateway/test_voice_telephony_routes.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests for the routes**

Create `tests/voice_gateway/test_voice_telephony_routes.py`:

```python
"""Tests for /voice/numbers/* control-plane endpoints."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app


AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")


@pytest.mark.asyncio
async def test_search_numbers_requires_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/voice/numbers/search?area_code=02")
        assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_search_numbers_returns_list():
    from booking_engine.clients.twilio_numbers import AvailableNumber
    with patch("booking_engine.api.routes.voice_telephony.search_available_numbers",
               return_value=[
                   AvailableNumber(phone_number="+390212345678",
                                   friendly_name="x", locality="Milano", region="L"),
               ]):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/voice/numbers/search?area_code=02", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["phone_number"] == "+390212345678"


@pytest.mark.asyncio
async def test_provision_writes_telephony_row():
    from booking_engine.clients.twilio_numbers import PurchasedNumber
    with patch("booking_engine.api.routes.voice_telephony.purchase_number",
               return_value=PurchasedNumber(sid="PN1", phone_number="+390212345678")), \
         patch("booking_engine.db.voice_telephony_queries.upsert_telephony") as upsert:
        upsert.return_value = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/numbers/provision",
                headers=AUTH,
                json={
                    "shop_id": "00000000-0000-0000-0000-000000000001",
                    "phone_number": "+390212345678",
                    "setup_path": "new",
                },
            )
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["kairo_number"] == "+390212345678"
    upsert.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_telephony_routes.py -v
```

Expected: import / 404 failures.

- [ ] **Step 3: Implement DB queries**

Create `booking_engine/db/voice_telephony_queries.py`:

```python
"""DB access for voice_agent.shop_telephony."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db import connection


async def upsert_telephony(
    *,
    shop_id: UUID,
    provider: str,
    kairo_number: str,
    kairo_number_sid: str,
    salon_existing_number: str | None,
    setup_path: str,
) -> dict:
    return await connection.execute_one(
        """
        INSERT INTO voice_agent.shop_telephony
            (shop_id, provider, kairo_number, kairo_number_sid,
             salon_existing_number, setup_path)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (shop_id) DO UPDATE SET
            provider = EXCLUDED.provider,
            kairo_number = EXCLUDED.kairo_number,
            kairo_number_sid = EXCLUDED.kairo_number_sid,
            salon_existing_number = EXCLUDED.salon_existing_number,
            setup_path = EXCLUDED.setup_path,
            provisioned_at = now()
        RETURNING *
        """,
        shop_id, provider, kairo_number, kairo_number_sid,
        salon_existing_number, setup_path,
    )


async def get_telephony(shop_id: UUID) -> dict | None:
    return await connection.execute_one(
        "SELECT * FROM voice_agent.shop_telephony WHERE shop_id = $1",
        shop_id,
    )


async def get_telephony_by_kairo_number(kairo_number: str) -> dict | None:
    return await connection.execute_one(
        "SELECT * FROM voice_agent.shop_telephony WHERE kairo_number = $1",
        kairo_number,
    )
```

- [ ] **Step 4: Implement the routes**

Create `booking_engine/api/routes/voice_telephony.py`:

```python
"""Telephony provisioning + listing endpoints (Path 1 + Path 2 onboarding)."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token
from booking_engine.clients.twilio_numbers import (
    purchase_number,
    search_available_numbers,
)
from booking_engine.config import Settings, get_settings
from booking_engine.db import voice_telephony_queries as q

router = APIRouter(prefix="/voice/numbers", tags=["voice-telephony"])


class AvailableNumberOut(BaseModel):
    phone_number: str
    friendly_name: str
    locality: str
    region: str


class ProvisionIn(BaseModel):
    shop_id: UUID
    phone_number: str
    setup_path: str = Field(pattern="^(new|forward)$")
    salon_existing_number: str | None = None


class TelephonyOut(BaseModel):
    shop_id: UUID
    kairo_number: str
    kairo_number_sid: str
    setup_path: str
    salon_existing_number: str | None


@router.get("/search")
async def search(
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    area_code: str | None = Query(default=None, max_length=4),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    results = search_available_numbers(
        area_code=area_code,
        country=settings.twilio_default_country,
        limit=limit,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )
    return {"data": [AvailableNumberOut(**r.__dict__).model_dump() for r in results]}


@router.post("/provision")
async def provision(
    body: ProvisionIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    if body.setup_path == "forward" and not body.salon_existing_number:
        raise HTTPException(400, "salon_existing_number required for forward setup")

    voice_url = f"{settings.public_base_url}/voice/twiml/incoming"
    purchased = purchase_number(
        phone_number=body.phone_number,
        voice_url=voice_url,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )
    row = await q.upsert_telephony(
        shop_id=body.shop_id,
        provider="twilio",
        kairo_number=purchased.phone_number,
        kairo_number_sid=purchased.sid,
        salon_existing_number=body.salon_existing_number,
        setup_path=body.setup_path,
    )
    return {"data": TelephonyOut(
        shop_id=row["shop_id"],
        kairo_number=row["kairo_number"],
        kairo_number_sid=row["kairo_number_sid"],
        setup_path=row["setup_path"],
        salon_existing_number=row["salon_existing_number"],
    ).model_dump()}


@router.get("/{shop_id}")
async def get_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    row = await q.get_telephony(shop_id)
    if not row:
        return {"data": None}
    return {"data": TelephonyOut(
        shop_id=row["shop_id"],
        kairo_number=row["kairo_number"],
        kairo_number_sid=row["kairo_number_sid"],
        setup_path=row["setup_path"],
        salon_existing_number=row["salon_existing_number"],
    ).model_dump()}
```

- [ ] **Step 5: Add `public_base_url` to Settings**

In `booking_engine/config.py` Settings class, add:

```python
public_base_url: str = ""  # e.g. "https://api.kairo.it" — used to construct webhook URLs
```

- [ ] **Step 6: Register the router**

In `booking_engine/app.py`, add to the router includes:

```python
from booking_engine.api.routes import voice_telephony
app.include_router(voice_telephony.router)
```

- [ ] **Step 7: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_telephony_routes.py -v
```

Expected: 3 passing.

- [ ] **Step 8: Commit**

```
git add booking_engine/api/routes/voice_telephony.py booking_engine/db/voice_telephony_queries.py booking_engine/app.py booking_engine/config.py tests/voice_gateway/test_voice_telephony_routes.py
git commit -m "feat(voice): /voice/numbers/* control-plane endpoints (search, provision, get)"
```

---

### Task 5: Token meter service — debit + 3-tier warning + detach decision

**Files:**
- Create: `booking_engine/db/token_basket_queries.py`
- Create: `booking_engine/services/token_meter.py`
- Create: `tests/voice_gateway/test_token_meter.py`

- [ ] **Step 1: Write failing tests for the meter**

Create `tests/voice_gateway/test_token_meter.py`:

```python
"""Tests for token meter — debit, warning tiers, and detach decision."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.token_meter import (
    DetachReason,
    SessionDecision,
    compute_warning_tier,
    decide_session,
    record_voice_debit,
)


def test_compute_warning_tier_normal():
    assert compute_warning_tier(balance=5000, last_refill=10000) is None


def test_compute_warning_tier_30pct():
    # 3000 / 10000 == 30%; threshold inclusive
    assert compute_warning_tier(balance=3000, last_refill=10000) == "low_30pct"


def test_compute_warning_tier_10pct():
    assert compute_warning_tier(balance=1000, last_refill=10000) == "critical_10pct"


def test_compute_warning_tier_below_reserve():
    assert compute_warning_tier(balance=500, last_refill=10000) == "below_reserve"


@pytest.mark.asyncio
async def test_decide_session_attaches_when_balance_ok():
    with patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=5000)):
        decision = await decide_session(shop_id=uuid4(), enabled=True,
                                        min_reserve=1500)
    assert decision.attach is True
    assert decision.detach_reason is None


@pytest.mark.asyncio
async def test_decide_session_detaches_when_disabled():
    with patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=5000)):
        decision = await decide_session(shop_id=uuid4(), enabled=False,
                                        min_reserve=1500)
    assert decision.attach is False
    assert decision.detach_reason == DetachReason.DISABLED


@pytest.mark.asyncio
async def test_decide_session_detaches_when_below_reserve():
    with patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=500)):
        decision = await decide_session(shop_id=uuid4(), enabled=True,
                                        min_reserve=1500)
    assert decision.attach is False
    assert decision.detach_reason == DetachReason.BASKET_LOW


@pytest.mark.asyncio
async def test_record_voice_debit_writes_event():
    call_id = uuid4()
    shop_id = uuid4()
    with patch("booking_engine.db.token_basket_queries.insert_debit_event",
               new=AsyncMock(return_value=None)) as ins:
        await record_voice_debit(
            shop_id=shop_id, call_id=call_id,
            duration_seconds=180, tool_token_cost=200,
            tokens_per_second=18,
        )
    ins.assert_awaited_once()
    kwargs = ins.await_args.kwargs
    # 180 * 18 + 200 = 3440
    assert kwargs["tokens"] == 3440
    assert kwargs["source"] == "voice_call"
    assert kwargs["voice_call_id"] == call_id
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_token_meter.py -v
```

Expected: import errors.

- [ ] **Step 3: Implement basket queries**

Create `booking_engine/db/token_basket_queries.py`:

```python
"""DB access for ai_token_baskets and ai_token_basket_events."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db import connection


async def get_balance(shop_id: UUID) -> int:
    """Return current basket balance for the shop (0 if no basket)."""
    row = await connection.execute_one(
        "SELECT balance_tokens FROM business_app_core.ai_token_baskets "
        "WHERE shop_id = $1 LIMIT 1",
        shop_id,
    )
    return int(row["balance_tokens"]) if row else 0


async def get_last_refill_amount(shop_id: UUID) -> int:
    """Return the most recent positive basket-credit amount (for % thresholds)."""
    row = await connection.execute_one(
        """
        SELECT tokens FROM business_app_core.ai_token_basket_events
        WHERE shop_id = $1 AND tokens > 0
        ORDER BY created_at DESC LIMIT 1
        """,
        shop_id,
    )
    return int(row["tokens"]) if row else 0


async def insert_debit_event(
    *,
    shop_id: UUID,
    tokens: int,
    source: str,
    voice_call_id: UUID | None,
) -> None:
    await connection.execute_void(
        """
        INSERT INTO business_app_core.ai_token_basket_events
            (shop_id, tokens, source, voice_call_id, created_at)
        VALUES ($1, $2, $3, $4, now())
        """,
        shop_id, -abs(tokens), source, voice_call_id,
    )
    await connection.execute_void(
        """
        UPDATE business_app_core.ai_token_baskets
        SET balance_tokens = balance_tokens - $2,
            updated_at = now()
        WHERE shop_id = $1
        """,
        shop_id, abs(tokens),
    )
```

- [ ] **Step 4: Implement the token meter service**

Create `booking_engine/services/token_meter.py`:

```python
"""Token meter — warning tiers, detach decision, voice call debit."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import UUID

from booking_engine.db.token_basket_queries import (
    get_balance,
    get_last_refill_amount,
    insert_debit_event,
)


WarningTier = Literal["low_30pct", "critical_10pct", "below_reserve"]


class DetachReason(str, Enum):
    DISABLED = "disabled"
    BASKET_LOW = "basket_low"


@dataclass
class SessionDecision:
    attach: bool
    balance: int
    detach_reason: DetachReason | None


def compute_warning_tier(
    *, balance: int, last_refill: int, min_reserve: int = 1500
) -> WarningTier | None:
    """Return the current warning tier, or None if balance is healthy."""
    if balance < min_reserve:
        return "below_reserve"
    if last_refill <= 0:
        return None
    pct = balance / last_refill
    if pct <= 0.10:
        return "critical_10pct"
    if pct <= 0.30:
        return "low_30pct"
    return None


async def decide_session(
    *, shop_id: UUID, enabled: bool, min_reserve: int = 1500
) -> SessionDecision:
    """Decide whether to attach the AI for a new inbound call."""
    balance = await get_balance(shop_id)
    if not enabled:
        return SessionDecision(attach=False, balance=balance,
                               detach_reason=DetachReason.DISABLED)
    if balance < min_reserve:
        return SessionDecision(attach=False, balance=balance,
                               detach_reason=DetachReason.BASKET_LOW)
    return SessionDecision(attach=True, balance=balance, detach_reason=None)


async def record_voice_debit(
    *,
    shop_id: UUID,
    call_id: UUID,
    duration_seconds: int,
    tool_token_cost: int,
    tokens_per_second: int,
) -> None:
    """Debit a completed call's tokens from the shop basket."""
    tokens = duration_seconds * tokens_per_second + tool_token_cost
    await insert_debit_event(
        shop_id=shop_id,
        tokens=tokens,
        source="voice_call",
        voice_call_id=call_id,
    )
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/voice_gateway/test_token_meter.py -v
```

Expected: 8 passing.

- [ ] **Step 6: Commit**

```
git add booking_engine/services/token_meter.py booking_engine/db/token_basket_queries.py tests/voice_gateway/test_token_meter.py
git commit -m "feat(voice): token meter with warning tiers and detach decision"
```

---

### Task 6: Dynamic TwiML webhook with detach matrix

**Files:**
- Create: `booking_engine/api/routes/voice_twiml.py`
- Create: `tests/voice_gateway/test_voice_twiml_webhook.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests for the TwiML webhook**

Create `tests/voice_gateway/test_voice_twiml_webhook.py`:

```python
"""Tests for the dynamic TwiML webhook (per-call routing decision)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app


def _form_data(called: str = "+390212345678", from_: str = "+393201234567"):
    return {"Called": called, "From": from_, "CallSid": "CA123"}


def _config(enabled: bool = True, fallback: str | None = None):
    return {"shop_id": uuid4(), "enabled": enabled,
            "manual_fallback_number": fallback,
            "manual_fallback_normalized": (fallback or "").replace("+", "")}


def _telephony(salon_existing: str | None = None):
    return {"shop_id": uuid4(),
            "kairo_number": "+390212345678",
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
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/twiml/incoming", data=_form_data())
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
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/twiml/incoming", data=_form_data())
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
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/twiml/incoming", data=_form_data())
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
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/twiml/incoming", data=_form_data())
    assert "<Say" in r.text
    assert "<Dial" not in r.text


@pytest.mark.asyncio
async def test_twiml_returns_say_when_unknown_number():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/twiml/incoming", data=_form_data())
    assert r.status_code == 200
    assert "<Say" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_twiml_webhook.py -v
```

Expected: 404s / import errors.

- [ ] **Step 3: Implement shop_config queries (minimal)**

Create `booking_engine/db/voice_config_queries.py`:

```python
"""DB access for voice_agent.shop_config (Layer 1)."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db import connection


async def get_config(shop_id: UUID) -> dict | None:
    return await connection.execute_one(
        "SELECT * FROM voice_agent.shop_config WHERE shop_id = $1",
        shop_id,
    )


async def upsert_config(shop_id: UUID, **fields) -> dict:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f"${i+2}" for i in range(len(fields)))
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields.keys())
    sql = (
        f"INSERT INTO voice_agent.shop_config (shop_id, {cols}, updated_at) "
        f"VALUES ($1, {placeholders}, now()) "
        f"ON CONFLICT (shop_id) DO UPDATE SET {sets}, updated_at = now() "
        f"RETURNING *"
    )
    return await connection.execute_one(sql, shop_id, *fields.values())
```

- [ ] **Step 4: Implement the TwiML webhook**

Create `booking_engine/api/routes/voice_twiml.py`:

```python
"""Dynamic TwiML webhook — per-call routing decision.

Twilio calls this on every inbound call. We respond with either:
  - <Dial><Sip>OpenAI SIP endpoint</Sip></Dial>  when AI is attached
  - <Dial>fallback_number</Dial>                  when AI is detached and fallback is set
  - <Say>recorded message</Say>                   otherwise

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

- [ ] **Step 5: Register the router**

In `booking_engine/app.py`:

```python
from booking_engine.api.routes import voice_twiml
app.include_router(voice_twiml.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_twiml_webhook.py -v
```

Expected: 5 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_twiml.py booking_engine/db/voice_config_queries.py booking_engine/app.py tests/voice_gateway/test_voice_twiml_webhook.py
git commit -m "feat(voice): dynamic TwiML webhook with detach matrix and Path-2 loop safety"
```

---

### Task 7: Config GET/PATCH endpoints with fallback loop validation

**Files:**
- Create: `booking_engine/api/routes/voice_config.py`
- Create: `tests/voice_gateway/test_voice_config_routes.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_voice_config_routes.py`:

```python
"""Tests for /voice/config/{shop_id} GET and PATCH."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_get_config_returns_existing():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_config",
               new=AsyncMock(return_value={
                   "shop_id": shop_id, "enabled": True,
                   "display_name": "Salone Lucia",
                   "greeting_after_disclosure": "Ciao!",
                   "voice_preset": "warm_female", "tone_preset": "warm",
                   "business_hours": {}, "answer_mode": "overflow",
                   "overflow_ring_count": 4, "services_to_mention": [],
                   "retention_days": 90, "manual_fallback_number": None,
                   "auto_topup_enabled": False,
                   "auto_topup_threshold_tokens": None,
                   "auto_topup_package_id": None,
               })):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/voice/config/{shop_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["data"]["display_name"] == "Salone Lucia"


@pytest.mark.asyncio
async def test_patch_rejects_fallback_equals_forwarded():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_telephony",
               new=AsyncMock(return_value={
                   "salon_existing_normalized": "393900000000",
               })):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/voice/config/{shop_id}",
                headers=AUTH,
                json={"manual_fallback_number": "+39 390 000 0000"},
            )
    assert r.status_code == 400
    assert "loop" in r.json()["error"].lower()


@pytest.mark.asyncio
async def test_patch_accepts_distinct_fallback():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_telephony",
               new=AsyncMock(return_value={
                   "salon_existing_normalized": "393900000000",
               })), \
         patch("booking_engine.api.routes.voice_config.upsert_config",
               new=AsyncMock(return_value={
                   "shop_id": shop_id, "enabled": True,
                   "manual_fallback_number": "+393201234567",
                   "display_name": "", "greeting_after_disclosure": "",
                   "voice_preset": "warm_female", "tone_preset": "warm",
                   "business_hours": {}, "answer_mode": "overflow",
                   "overflow_ring_count": 4, "services_to_mention": [],
                   "retention_days": 90,
                   "auto_topup_enabled": False,
                   "auto_topup_threshold_tokens": None,
                   "auto_topup_package_id": None,
               })):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/voice/config/{shop_id}",
                headers=AUTH,
                json={"manual_fallback_number": "+393201234567"},
            )
    assert r.status_code == 200
    assert r.json()["data"]["manual_fallback_number"] == "+393201234567"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_config_routes.py -v
```

Expected: 404s / import errors.

- [ ] **Step 3: Implement the routes**

Create `booking_engine/api/routes/voice_config.py`:

```python
"""Voice agent Layer 1 config GET/PATCH endpoints."""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db.voice_config_queries import get_config, upsert_config
from booking_engine.db.voice_telephony_queries import get_telephony
from booking_engine.services.phone_normalize import digits_only

router = APIRouter(prefix="/voice/config", tags=["voice-config"])


_PATCHABLE_FIELDS = {
    "enabled", "display_name", "greeting_after_disclosure",
    "voice_preset", "tone_preset", "business_hours",
    "answer_mode", "overflow_ring_count",
    "services_to_mention", "retention_days",
    "manual_fallback_number",
    "auto_topup_enabled", "auto_topup_threshold_tokens", "auto_topup_package_id",
}


class ConfigPatch(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    greeting_after_disclosure: str | None = None
    voice_preset: str | None = Field(default=None, pattern=r"^(warm_female|neutral_female|neutral_male)$")
    tone_preset: str | None = Field(default=None, pattern=r"^(warm|professional|casual)$")
    business_hours: dict | None = None
    answer_mode: str | None = Field(default=None, pattern=r"^(overflow|always_on)$")
    overflow_ring_count: int | None = Field(default=None, ge=1, le=10)
    services_to_mention: list[UUID] | None = None
    retention_days: int | None = Field(default=None, ge=30, le=365)
    manual_fallback_number: str | None = None
    auto_topup_enabled: bool | None = None
    auto_topup_threshold_tokens: int | None = Field(default=None, ge=0)
    auto_topup_package_id: UUID | None = None


@router.get("/{shop_id}")
async def get_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict[str, Any]:
    row = await get_config(shop_id)
    return {"data": row}


@router.patch("/{shop_id}")
async def patch_for_shop(
    shop_id: UUID,
    body: ConfigPatch,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, exclude_none=False)
    payload = {k: v for k, v in payload.items() if k in _PATCHABLE_FIELDS}

    # Loop-safety validation: fallback must differ from forwarded number
    if payload.get("manual_fallback_number"):
        normalized_new = digits_only(payload["manual_fallback_number"])
        telephony = await get_telephony(shop_id)
        if telephony and telephony.get("salon_existing_normalized"):
            if normalized_new == telephony["salon_existing_normalized"]:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "Fallback number creates a forwarding loop "
                                     "with the salon's existing number."},
                )

    if not payload:
        existing = await get_config(shop_id)
        return {"data": existing}

    row = await upsert_config(shop_id, **payload)
    return {"data": row}
```

- [ ] **Step 4: Adapt HTTPException to error-envelope contract**

The test expects `r.json()["error"]`. Adjust by overriding response when detail is a dict.

Add to `booking_engine/api/routes/voice_config.py` at the bottom of `patch_for_shop`, or use a small adapter. Simpler: make HTTPException's `detail` a dict and verify FastAPI returns it under `detail` — adapt the test to read `detail.error` OR have the route return `JSONResponse(status_code=400, content={"error": "..."}`).

Easiest fix: replace the `raise HTTPException(...)` with:

```python
from fastapi.responses import JSONResponse
...
return JSONResponse(
    status_code=400,
    content={"error": "Fallback number creates a forwarding loop "
                     "with the salon's existing number."},
)
```

- [ ] **Step 5: Register the router**

In `booking_engine/app.py`:

```python
from booking_engine.api.routes import voice_config
app.include_router(voice_config.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_config_routes.py -v
```

Expected: 3 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_config.py booking_engine/app.py tests/voice_gateway/test_voice_config_routes.py
git commit -m "feat(voice): /voice/config GET and PATCH with Path-2 loop validation"
```

---

### Task 8: Push notification client + warning event emitter

**Files:**
- Create: `booking_engine/clients/push_notifications.py`
- Create: `booking_engine/services/balance_alerts.py`
- Create: `tests/voice_gateway/test_balance_alerts.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_balance_alerts.py`:

```python
"""Tests for the balance-alert emitter — sends push events on tier transitions."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.balance_alerts import maybe_emit_balance_alert


@pytest.mark.asyncio
async def test_emits_low_30pct_when_crossing_threshold():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=2999, last_refill=10000,
            previous_tier=None,
        )
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_balance_low_30pct"


@pytest.mark.asyncio
async def test_emits_critical_when_dropping_further():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=999, last_refill=10000,
            previous_tier="low_30pct",
        )
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_balance_critical_10pct"


@pytest.mark.asyncio
async def test_emits_detached_when_below_reserve():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=100, last_refill=10000,
            previous_tier="critical_10pct",
        )
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_detached"


@pytest.mark.asyncio
async def test_no_emit_when_tier_unchanged():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=2500, last_refill=10000,
            previous_tier="low_30pct",
        )
    push.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_balance_alerts.py -v
```

Expected: import errors.

- [ ] **Step 3: Implement push client (stub for now — wired in Plan C)**

Create `booking_engine/clients/push_notifications.py`:

```python
"""Push notification client (stub).

Plan C wires this to the webapp's existing notification infrastructure.
For now, this logs the event so behavior is observable and tests can mock it.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def send_push(
    *, shop_id: UUID, event: str, payload: dict[str, Any] | None = None
) -> None:
    """Send a push event to all merchant devices subscribed for this shop."""
    logger.info("push %s shop_id=%s payload=%s", event, shop_id, payload or {})
    # Plan C: forward to webapp /api/v1/notifications/push endpoint.
```

- [ ] **Step 4: Implement balance alert emitter**

Create `booking_engine/services/balance_alerts.py`:

```python
"""Balance alert emitter — fires push events on warning tier transitions."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from booking_engine.clients.push_notifications import send_push
from booking_engine.services.token_meter import compute_warning_tier


_EVENT_FOR_TIER = {
    "low_30pct": "voice_balance_low_30pct",
    "critical_10pct": "voice_balance_critical_10pct",
    "below_reserve": "voice_detached",
}


async def maybe_emit_balance_alert(
    *,
    shop_id: UUID,
    balance: int,
    last_refill: int,
    previous_tier: Literal["low_30pct", "critical_10pct", "below_reserve"] | None,
) -> None:
    """Emit a push event if the warning tier changed to a more severe level."""
    new_tier = compute_warning_tier(balance=balance, last_refill=last_refill)
    if new_tier is None or new_tier == previous_tier:
        return
    # Only emit when crossing to a more severe tier
    severity = {"low_30pct": 1, "critical_10pct": 2, "below_reserve": 3}
    prev_score = severity.get(previous_tier or "", 0)
    if severity[new_tier] <= prev_score:
        return
    await send_push(
        shop_id=shop_id,
        event=_EVENT_FOR_TIER[new_tier],
        payload={"balance": balance, "last_refill": last_refill},
    )
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/voice_gateway/test_balance_alerts.py -v
```

Expected: 4 passing.

- [ ] **Step 6: Commit**

```
git add booking_engine/clients/push_notifications.py booking_engine/services/balance_alerts.py tests/voice_gateway/test_balance_alerts.py
git commit -m "feat(voice): balance alert emitter for warning tiers and detach"
```

---

### Task 9: Wire balance alerts to debit flow + add /voice/balance/status endpoint

**Files:**
- Modify: `booking_engine/services/token_meter.py`
- Create: `booking_engine/api/routes/voice_balance.py`
- Create: `tests/voice_gateway/test_voice_balance_route.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing test for status endpoint**

Create `tests/voice_gateway/test_voice_balance_route.py`:

```python
"""Tests for /voice/balance/{shop_id} endpoint used by webapp banners."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_balance_status_includes_tier():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_balance.get_balance",
               new=AsyncMock(return_value=900)), \
         patch("booking_engine.api.routes.voice_balance.get_last_refill_amount",
               new=AsyncMock(return_value=10000)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/voice/balance/{shop_id}", headers=AUTH)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["balance"] == 900
    assert data["last_refill"] == 10000
    assert data["tier"] == "critical_10pct"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_balance_route.py -v
```

Expected: 404.

- [ ] **Step 3: Implement status endpoint**

Create `booking_engine/api/routes/voice_balance.py`:

```python
"""/voice/balance/{shop_id} — exposes current balance + warning tier for webapp banners."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db.token_basket_queries import get_balance, get_last_refill_amount
from booking_engine.services.token_meter import compute_warning_tier

router = APIRouter(prefix="/voice/balance", tags=["voice-balance"])


@router.get("/{shop_id}")
async def status(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    balance = await get_balance(shop_id)
    last_refill = await get_last_refill_amount(shop_id)
    tier = compute_warning_tier(balance=balance, last_refill=last_refill)
    return {"data": {"balance": balance, "last_refill": last_refill, "tier": tier}}
```

- [ ] **Step 4: Register the router**

In `booking_engine/app.py`:

```python
from booking_engine.api.routes import voice_balance
app.include_router(voice_balance.router)
```

- [ ] **Step 5: Wire balance-alert emission into `record_voice_debit`**

Modify `booking_engine/services/token_meter.py`. At the end of `record_voice_debit`, after the debit:

```python
async def record_voice_debit(
    *,
    shop_id: UUID,
    call_id: UUID,
    duration_seconds: int,
    tool_token_cost: int,
    tokens_per_second: int,
    previous_tier: str | None = None,
) -> None:
    tokens = duration_seconds * tokens_per_second + tool_token_cost
    await insert_debit_event(
        shop_id=shop_id, tokens=tokens, source="voice_call", voice_call_id=call_id,
    )
    # After debit, check whether we crossed a warning threshold
    from booking_engine.db.token_basket_queries import (
        get_balance, get_last_refill_amount,
    )
    from booking_engine.services.balance_alerts import maybe_emit_balance_alert

    balance = await get_balance(shop_id)
    last_refill = await get_last_refill_amount(shop_id)
    await maybe_emit_balance_alert(
        shop_id=shop_id, balance=balance, last_refill=last_refill,
        previous_tier=previous_tier,
    )
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_balance_route.py tests/voice_gateway/test_token_meter.py -v
```

Expected: token_meter tests still passing + balance route test passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_balance.py booking_engine/services/token_meter.py booking_engine/app.py tests/voice_gateway/test_voice_balance_route.py
git commit -m "feat(voice): /voice/balance status endpoint and alert emission on debit"
```

---

### Task 10: Path 2 forwarding heartbeat scheduler

**Files:**
- Create: `booking_engine/services/forwarding_heartbeat.py`
- Create: `tests/voice_gateway/test_forwarding_heartbeat.py`

- [ ] **Step 1: Write failing test**

Create `tests/voice_gateway/test_forwarding_heartbeat.py`:

```python
"""Tests for the Path-2 forwarding heartbeat — detects silent number outages."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.forwarding_heartbeat import (
    find_silent_forwarded_shops,
)


@pytest.mark.asyncio
async def test_finds_shops_with_no_inbound_in_5_days():
    six_days_ago = datetime.now(timezone.utc) - timedelta(days=6)
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    fake_rows = [
        {"shop_id": uuid4(), "kairo_number": "+39021",
         "last_inbound_call_at": six_days_ago, "setup_path": "forward"},
        {"shop_id": uuid4(), "kairo_number": "+39022",
         "last_inbound_call_at": one_day_ago, "setup_path": "forward"},
    ]
    with patch("booking_engine.services.forwarding_heartbeat.connection.execute",
               new=AsyncMock(return_value=fake_rows[:1])):
        results = await find_silent_forwarded_shops(threshold_days=5)
    assert len(results) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_forwarding_heartbeat.py -v
```

- [ ] **Step 3: Implement the heartbeat query**

Create `booking_engine/services/forwarding_heartbeat.py`:

```python
"""Path-2 forwarding heartbeat — finds shops whose forwarded number went silent.

Run nightly by a Lambda scheduled event. For each silent shop, emit a push
event 'forwarding_might_be_off'. Webapp surfaces this as a persistent banner.
"""
from __future__ import annotations

from booking_engine.db import connection


async def find_silent_forwarded_shops(*, threshold_days: int = 5) -> list[dict]:
    """Return Path-2 shops with no inbound call in the last `threshold_days`."""
    return await connection.execute(
        """
        SELECT shop_id, kairo_number, last_inbound_call_at, setup_path
        FROM voice_agent.shop_telephony
        WHERE setup_path = 'forward'
          AND (
              last_inbound_call_at IS NULL
              OR last_inbound_call_at < now() - ($1 || ' days')::interval
          )
        """,
        str(threshold_days),
    )
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/voice_gateway/test_forwarding_heartbeat.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add booking_engine/services/forwarding_heartbeat.py tests/voice_gateway/test_forwarding_heartbeat.py
git commit -m "feat(voice): Path-2 forwarding heartbeat detector (5-day silent threshold)"
```

---

### Task 11: Full-suite regression check + deploy prep

- [ ] **Step 1: Run the full test suite**

```
pytest tests/voice_gateway/ -v
```

Expected: all green (40+ tests across this plan + existing tests untouched).

- [ ] **Step 2: Update `requirements.txt` lock if used**

If the project uses `requirements.lock` or Poetry, regenerate. Otherwise verify `pip install -r requirements.txt` succeeds in a clean venv.

- [ ] **Step 3: Add deploy notes**

Append to `docs/DEPLOY_VOICE_AGENT.md` (existing file from prior session):

```markdown
## Plan A — Platform deploy notes (2026-06-03)

### New env vars on Lambda
- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `OPENAI_SIP_PROJECT_ID` — OpenAI project ID for SIP routing
- `PUBLIC_BASE_URL` — public URL of the API Gateway in front of Lambda (used for TwiML voice_url)
- `VOICE_KAIRO_TOKENS_PER_SECOND` — default 18
- `VOICE_MIN_SESSION_RESERVE_TOKENS` — default 1500
- `VOICE_MAX_OVERAGE_TOKENS` — default 5000

### Migration
Run `psql $DATABASE_URL -f booking_engine/db/sql/04_voice_agent_v2.sql` once against prod.

### Scheduled job
Add an EventBridge rule that POSTs to `/voice/heartbeat/forwarding` nightly at 09:00 Europe/Rome.
```

- [ ] **Step 4: Commit**

```
git add docs/DEPLOY_VOICE_AGENT.md
git commit -m "docs(voice): Plan A deploy notes (env vars, migration, heartbeat schedule)"
```

---

## Done definition for Plan A

- All 11 tasks committed.
- `pytest tests/voice_gateway/` passes cleanly.
- Migration `04_voice_agent_v2.sql` applies cleanly on a fresh DB and on the live dev DB.
- Following endpoints exist and respond per spec: `GET /voice/numbers/search`, `POST /voice/numbers/provision`, `GET /voice/numbers/{shop_id}`, `POST /voice/twiml/incoming`, `GET /voice/config/{shop_id}`, `PATCH /voice/config/{shop_id}`, `GET /voice/balance/{shop_id}`.
- Token meter computes correct warning tiers and detach decisions.
- Push event emitter is wired (stub) and ready for Plan C webapp integration.

Plan B builds on this foundation: 12 agent tools, identity resolution, session event webhooks, and memo creation on `escalate_to_merchant`.
