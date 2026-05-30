# Voice-Agent Schema + Control-Plane Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated `voice_agent` PostgreSQL schema (calls, transcripts, events), extend `shops` with `voice` + `language` columns, and expose all control-plane REST endpoints (`GET`/`PATCH /voice/config`, `GET /voice/calls`, `GET /voice/calls/{id}`, `PATCH /voice/calls/{id}/link-customer`, `GET /voice/analytics`) on the Booking Engine — protected by a shared bearer secret.

**Architecture:** Schema migration runs idempotently from `booking_engine/db/sql/`. Async queries via existing `execute / execute_one / execute_void` helpers in `booking_engine/db/connection.py`. Endpoints follow the existing per-resource router pattern under `booking_engine/api/routes/` and are mounted in `booking_engine/api/app.py`. Bearer auth applied only to voice routes via a `Depends()` dependency reading `CONTROL_PLANE_SECRET` from `Settings`.

**Tech Stack:** FastAPI, asyncpg (Neon PostgreSQL), Pydantic v2, pytest. Live-DB tests use the existing `tests/live_db/` harness requiring `DATABASE_URL`.

**Spec:** `docs/superpowers/specs/2026-05-30-inbox-voice-agent-redesign.md` (this is in the webapp repo; copy/link locally if needed).

---

## File Structure

**Create:**
- `booking_engine/db/sql/03_voice_agent_schema.sql` — new schema + tables + indexes + `shops` column additions
- `booking_engine/db/voice_queries.py` — all SQL functions for voice_agent + voice shop columns
- `booking_engine/api/routes/voice.py` — all `/voice/*` endpoints
- `booking_engine/api/voice_models.py` — Pydantic request/response models for voice endpoints
- `booking_engine/api/deps.py` — bearer-auth dependency `require_control_plane_token`
- `tests/booking_engine/test_voice_models.py` — model validation
- `tests/booking_engine/test_voice_routes.py` — route handlers with DB mocked
- `tests/booking_engine/test_deps.py` — bearer auth dependency
- `tests/live_db/test_voice_queries.py` — live-DB query tests

**Modify:**
- `booking_engine/config.py` — add `CONTROL_PLANE_SECRET` setting
- `booking_engine/api/app.py` — register `voice.router`
- `scripts/setup_neon.sh` — also apply `03_voice_agent_schema.sql`
- `docs/INTEGRATION_GUIDE.md` — add `voice_agent` schema section, new shop columns, endpoint list

---

## Conventions used throughout

- Run unit tests: `pytest tests/booking_engine/ -v`
- Run live-DB tests: `DATABASE_URL=postgresql://... pytest tests/live_db/ -v`
- Commit messages: conventional (`feat:`, `test:`, `chore:`)
- After every passing test, commit before moving to the next task

---

### Task 1: Schema migration file

**Files:**
- Create: `booking_engine/db/sql/03_voice_agent_schema.sql`

- [ ] **Step 1: Write the SQL file (idempotent, additive)**

```sql
-- Voice Agent control-plane schema.
-- Owns call lifecycle data written by the Voice Gateway.
-- Webapp Control Plane reads via HTTP (Booking Engine endpoints), never directly.

-- Additive columns on shops (read by Voice Gateway when building OpenAI session)
ALTER TABLE shops ADD COLUMN IF NOT EXISTS voice    TEXT NOT NULL DEFAULT 'alloy';
ALTER TABLE shops ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'it';

CREATE SCHEMA IF NOT EXISTS voice_agent;

CREATE TABLE IF NOT EXISTS voice_agent.calls (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id               UUID NOT NULL REFERENCES public.shops(id),
  twilio_call_sid       TEXT UNIQUE,
  caller_number         TEXT NOT NULL,
  customer_id           UUID REFERENCES public.customers(id),
  customer_match        TEXT NOT NULL CHECK (customer_match IN ('existing','created','unmatched','ambiguous')),
  started_at            TIMESTAMPTZ NOT NULL,
  ended_at              TIMESTAMPTZ,
  duration_seconds      INTEGER,
  outcome               TEXT CHECK (outcome IN ('booked','rescheduled','cancelled','info','abandoned','escalated','failed')),
  outcome_reason        TEXT,
  summary               TEXT,
  appointment_id        UUID REFERENCES public.appointments(id),
  requested_service_ids UUID[],
  requested_staff_id    UUID,
  error_code            TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_calls_shop_started  ON voice_agent.calls (shop_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_calls_shop_outcome  ON voice_agent.calls (shop_id, outcome);
CREATE INDEX IF NOT EXISTS idx_voice_calls_customer      ON voice_agent.calls (customer_id);

CREATE TABLE IF NOT EXISTS voice_agent.call_transcripts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id     UUID NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
  turn_index  INTEGER NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('caller','assistant','system')),
  text        TEXT NOT NULL,
  at          TIMESTAMPTZ NOT NULL,
  UNIQUE (call_id, turn_index)
);

CREATE TABLE IF NOT EXISTS voice_agent.call_events (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id     UUID NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
  at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  type        TEXT NOT NULL,
  payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_voice_call_events_call ON voice_agent.call_events (call_id, at);
```

- [ ] **Step 2: Modify `scripts/setup_neon.sh` to apply the new file**

Open the script and add a line applying `03_voice_agent_schema.sql` after the existing schema/seed lines. Typical pattern:

```bash
psql "$DATABASE_URL" -f booking_engine/db/sql/03_voice_agent_schema.sql
```

- [ ] **Step 3: Apply against a scratch Neon branch**

Run:
```bash
DATABASE_URL=postgresql://...branch... psql "$DATABASE_URL" -f booking_engine/db/sql/03_voice_agent_schema.sql
```
Expected: no errors. Re-run a second time to verify idempotency.

- [ ] **Step 4: Verify schema**

```bash
psql "$DATABASE_URL" -c "\dn voice_agent" -c "\dt voice_agent.*" -c "\d shops" | grep -E "voice|language|voice_agent"
```
Expected: `voice_agent` schema present; `calls`, `call_transcripts`, `call_events` tables; `voice` and `language` columns on `shops`.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/sql/03_voice_agent_schema.sql scripts/setup_neon.sh
git commit -m "feat(db): add voice_agent schema and shops.voice/language columns"
```

---

### Task 2: Settings — add CONTROL_PLANE_SECRET

**Files:**
- Modify: `booking_engine/config.py`
- Test: `tests/booking_engine/test_config.py`

- [ ] **Step 1: Read existing config and test files**

Run:
```bash
cat booking_engine/config.py tests/booking_engine/test_config.py
```
Note the existing `Settings` class fields and how they're tested.

- [ ] **Step 2: Write failing test**

Append to `tests/booking_engine/test_config.py`:

```python
def test_control_plane_secret_loaded(monkeypatch):
    from booking_engine.config import Settings
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret-123")
    s = Settings()
    assert s.control_plane_secret == "test-secret-123"


def test_control_plane_secret_default_empty(monkeypatch):
    from booking_engine.config import Settings
    monkeypatch.delenv("CONTROL_PLANE_SECRET", raising=False)
    s = Settings()
    assert s.control_plane_secret == ""
```

- [ ] **Step 3: Run test, verify failure**

`pytest tests/booking_engine/test_config.py -v -k control_plane`
Expected: FAIL — attribute not present.

- [ ] **Step 4: Add the field to `Settings`**

In `booking_engine/config.py`, add to the `Settings` class:
```python
    control_plane_secret: str = ""
```
And in its `Config` / env source make sure it picks up `CONTROL_PLANE_SECRET` (pydantic-settings reads upper-snake env vars by default; verify by reading the existing class definition).

- [ ] **Step 5: Run test, verify pass**

`pytest tests/booking_engine/test_config.py -v -k control_plane` → PASS.

- [ ] **Step 6: Commit**

```bash
git add booking_engine/config.py tests/booking_engine/test_config.py
git commit -m "feat(config): add CONTROL_PLANE_SECRET setting"
```

---

### Task 3: Bearer-auth dependency

**Files:**
- Create: `booking_engine/api/deps.py`
- Test: `tests/booking_engine/test_deps.py`

- [ ] **Step 1: Write failing test**

```python
"""Tests for shared API dependencies."""
import pytest
from fastapi import HTTPException
from booking_engine.api.deps import require_control_plane_token


class _Req:
    def __init__(self, header: str | None):
        self.headers = {} if header is None else {"authorization": header}


def _settings(secret: str):
    class S:
        control_plane_secret = secret
    return S()


@pytest.mark.asyncio
async def test_missing_header_rejected():
    with pytest.raises(HTTPException) as exc:
        await require_control_plane_token.__wrapped__(_Req(None), _settings("s"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_rejected():
    with pytest.raises(HTTPException) as exc:
        await require_control_plane_token.__wrapped__(_Req("Bearer nope"), _settings("s"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_accepted():
    result = await require_control_plane_token.__wrapped__(_Req("Bearer good"), _settings("good"))
    assert result is True


@pytest.mark.asyncio
async def test_empty_server_secret_rejects_all():
    with pytest.raises(HTTPException) as exc:
        await require_control_plane_token.__wrapped__(_Req("Bearer anything"), _settings(""))
    assert exc.value.status_code == 503
```

Note: `__wrapped__` is the undecorated function — we wrap with `functools.wraps` in the implementation so tests can call it without FastAPI's dependency-injection machinery.

- [ ] **Step 2: Run test, verify failure**

`pytest tests/booking_engine/test_deps.py -v` → ImportError.

- [ ] **Step 3: Implement dependency**

Create `booking_engine/api/deps.py`:

```python
"""Shared FastAPI dependencies for the Booking Engine."""
from __future__ import annotations

import functools
from fastapi import Depends, HTTPException, Request

from booking_engine.config import Settings


def _get_settings() -> Settings:
    return Settings()


@functools.wraps(lambda *a, **k: None)
async def require_control_plane_token(
    request: Request,
    settings: Settings = Depends(_get_settings),
) -> bool:
    if not settings.control_plane_secret:
        raise HTTPException(status_code=503, detail="control plane disabled")
    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.control_plane_secret}"
    if header != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return True
```

- [ ] **Step 4: Run test, verify pass**

`pytest tests/booking_engine/test_deps.py -v` → all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/deps.py tests/booking_engine/test_deps.py
git commit -m "feat(api): add bearer-auth dependency for control-plane routes"
```

---

### Task 4: Pydantic models for voice endpoints

**Files:**
- Create: `booking_engine/api/voice_models.py`
- Test: `tests/booking_engine/test_voice_models.py`

- [ ] **Step 1: Write failing test**

```python
"""Validation tests for voice control-plane Pydantic models."""
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from pydantic import ValidationError

from booking_engine.api.voice_models import (
    VoiceConfigResponse,
    VoiceConfigUpdateRequest,
    CallSummary,
    CallDetail,
    TranscriptTurn,
    CallEvent,
    LinkCustomerRequest,
    VoiceAnalyticsResponse,
)

VALID_LANG = {"it", "en", "es"}
VALID_VOICES = {"alloy", "echo", "shimmer", "ash", "ballad", "coral", "sage", "verse"}


def test_voice_config_response_minimal():
    m = VoiceConfigResponse(is_active=True, voice="alloy", language="it")
    assert m.welcome_message is None and m.is_active is True


def test_voice_config_update_all_optional():
    m = VoiceConfigUpdateRequest()
    assert m.model_dump(exclude_unset=True) == {}


def test_voice_config_update_language_validated():
    with pytest.raises(ValidationError):
        VoiceConfigUpdateRequest(language="fr")


def test_voice_config_update_voice_validated():
    with pytest.raises(ValidationError):
        VoiceConfigUpdateRequest(voice="not-a-voice")


def test_call_summary_round_trip():
    payload = {
        "id": str(uuid4()),
        "caller_number": "+39000",
        "customer_id": None,
        "customer_match": "unmatched",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "duration_seconds": None,
        "outcome": None,
        "summary": None,
        "appointment_id": None,
    }
    m = CallSummary(**payload)
    assert m.customer_match == "unmatched"


def test_call_summary_rejects_unknown_outcome():
    with pytest.raises(ValidationError):
        CallSummary(
            id=str(uuid4()), caller_number="+39", customer_match="existing",
            started_at=datetime.now(timezone.utc), outcome="nope",
        )


def test_link_customer_request_requires_uuid():
    with pytest.raises(ValidationError):
        LinkCustomerRequest(customer_id="not-a-uuid")


def test_call_detail_aggregates():
    cs_id = uuid4()
    summary = CallSummary(
        id=cs_id, caller_number="+39", customer_match="existing",
        started_at=datetime.now(timezone.utc),
    )
    d = CallDetail(call=summary, transcript=[], events=[])
    assert d.call.id == cs_id


def test_analytics_response_shape():
    a = VoiceAnalyticsResponse(
        volume={"total": 0, "by_day": [], "avg_duration_sec": 0, "failure_rate": 0.0},
        outcomes={"booked": 0, "rescheduled": 0, "cancelled": 0, "info": 0,
                  "abandoned": 0, "escalated": 0, "failed": 0, "conversion_rate": 0.0},
        demand={"top_services": [], "top_staff": [],
                "by_hour": [], "by_dow": [], "after_hours_pct": 0.0},
    )
    assert a.volume["total"] == 0
```

- [ ] **Step 2: Run test, verify failure**

`pytest tests/booking_engine/test_voice_models.py -v` → ImportError.

- [ ] **Step 3: Implement models**

Create `booking_engine/api/voice_models.py`:

```python
"""Pydantic models for voice control-plane endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


Outcome = Literal[
    "booked", "rescheduled", "cancelled", "info",
    "abandoned", "escalated", "failed",
]
CustomerMatch = Literal["existing", "created", "unmatched", "ambiguous"]
Voice = Literal["alloy", "echo", "shimmer", "ash", "ballad", "coral", "sage", "verse"]
Language = Literal["it", "en", "es"]


class VoiceConfigResponse(BaseModel):
    welcome_message: str | None = None
    tone_instructions: str | None = None
    personality: str | None = None
    special_instructions: str | None = None
    voice: Voice
    language: Language
    is_active: bool


class VoiceConfigUpdateRequest(BaseModel):
    welcome_message: str | None = None
    tone_instructions: str | None = None
    personality: str | None = None
    special_instructions: str | None = None
    voice: Voice | None = None
    language: Language | None = None
    is_active: bool | None = None


class CallSummary(BaseModel):
    id: UUID
    caller_number: str
    customer_id: UUID | None = None
    customer_match: CustomerMatch
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    outcome: Outcome | None = None
    summary: str | None = None
    appointment_id: UUID | None = None


class TranscriptTurn(BaseModel):
    turn_index: int
    role: Literal["caller", "assistant", "system"]
    text: str
    at: datetime


class CallEvent(BaseModel):
    at: datetime
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CallDetail(BaseModel):
    call: CallSummary
    transcript: list[TranscriptTurn]
    events: list[CallEvent]


class LinkCustomerRequest(BaseModel):
    customer_id: UUID


class VolumeBlock(BaseModel):
    total: int
    by_day: list[dict[str, Any]]
    avg_duration_sec: int
    failure_rate: float


class OutcomesBlock(BaseModel):
    booked: int
    rescheduled: int
    cancelled: int
    info: int
    abandoned: int
    escalated: int
    failed: int
    conversion_rate: float


class DemandBlock(BaseModel):
    top_services: list[dict[str, Any]]
    top_staff: list[dict[str, Any]]
    by_hour: list[dict[str, Any]]
    by_dow: list[dict[str, Any]]
    after_hours_pct: float


class VoiceAnalyticsResponse(BaseModel):
    volume: dict[str, Any] | VolumeBlock
    outcomes: dict[str, Any] | OutcomesBlock
    demand: dict[str, Any] | DemandBlock
```

- [ ] **Step 4: Run test, verify pass**

`pytest tests/booking_engine/test_voice_models.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/voice_models.py tests/booking_engine/test_voice_models.py
git commit -m "feat(api): pydantic models for voice control-plane endpoints"
```

---

### Task 5: Query functions — voice config

**Files:**
- Create: `booking_engine/db/voice_queries.py`
- Test: `tests/live_db/test_voice_queries.py`

> Live-DB tests require `DATABASE_URL` set to a Neon branch with the new schema applied (Task 1).

- [ ] **Step 1: Write failing test**

Create `tests/live_db/test_voice_queries.py`:

```python
"""Live-DB tests for voice_agent queries."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from booking_engine.db.connection import init_connection, close_connection, execute_void
from booking_engine.config import Settings
from booking_engine.db import voice_queries as vq

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for live-DB tests",
)


@pytest.fixture(scope="module", autouse=True)
async def db_pool():
    await init_connection(Settings())
    yield
    await close_connection()


@pytest.fixture
async def seeded_shop():
    shop_id = uuid4()
    await execute_void(
        "INSERT INTO shops (id, name, is_active) VALUES ($1, 'Test Shop', true)",
        shop_id,
    )
    yield shop_id
    await execute_void("DELETE FROM voice_agent.calls WHERE shop_id = $1", shop_id)
    await execute_void("DELETE FROM shops WHERE id = $1", shop_id)


async def test_get_voice_config_returns_defaults(seeded_shop):
    cfg = await vq.get_voice_config(seeded_shop)
    assert cfg["voice"] == "alloy"
    assert cfg["language"] == "it"
    assert cfg["is_active"] is True


async def test_get_voice_config_missing_shop_returns_none():
    cfg = await vq.get_voice_config(uuid4())
    assert cfg is None


async def test_update_voice_config_partial(seeded_shop):
    updated = await vq.update_voice_config(
        seeded_shop, {"welcome_message": "Ciao!", "voice": "echo"}
    )
    assert updated["welcome_message"] == "Ciao!"
    assert updated["voice"] == "echo"
    assert updated["language"] == "it"  # untouched


async def test_update_voice_config_empty_payload_is_noop(seeded_shop):
    before = await vq.get_voice_config(seeded_shop)
    after = await vq.update_voice_config(seeded_shop, {})
    assert before == after
```

- [ ] **Step 2: Run test, verify failure**

`DATABASE_URL=... pytest tests/live_db/test_voice_queries.py -v` → ImportError.

- [ ] **Step 3: Implement `voice_queries.py` (config functions)**

```python
"""SQL query functions for voice_agent schema and shops.voice/language columns."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void


_VOICE_CONFIG_FIELDS = (
    "welcome_message", "tone_instructions", "personality", "special_instructions",
    "voice", "language", "is_active",
)
_ALLOWED_UPDATE_FIELDS = set(_VOICE_CONFIG_FIELDS)


async def get_voice_config(shop_id: UUID) -> dict | None:
    cols = ", ".join(_VOICE_CONFIG_FIELDS)
    return await execute_one(
        f"SELECT {cols} FROM shops WHERE id = $1",
        shop_id,
    )


async def update_voice_config(shop_id: UUID, patch: dict) -> dict | None:
    fields = [(k, v) for k, v in patch.items() if k in _ALLOWED_UPDATE_FIELDS]
    if not fields:
        return await get_voice_config(shop_id)
    set_clause = ", ".join(f"{k} = ${i+2}" for i, (k, _) in enumerate(fields))
    values = [v for _, v in fields]
    await execute_void(
        f"UPDATE shops SET {set_clause} WHERE id = $1",
        shop_id, *values,
    )
    return await get_voice_config(shop_id)
```

- [ ] **Step 4: Run live-DB tests, verify pass**

`DATABASE_URL=... pytest tests/live_db/test_voice_queries.py -v` → 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/voice_queries.py tests/live_db/test_voice_queries.py
git commit -m "feat(db): voice config get/update queries"
```

---

### Task 6: Query functions — calls list, detail, link customer

**Files:**
- Modify: `booking_engine/db/voice_queries.py`
- Modify: `tests/live_db/test_voice_queries.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/live_db/test_voice_queries.py`:

```python
async def test_list_calls_empty(seeded_shop):
    result = await vq.list_calls(seeded_shop, filters={}, cursor=None, limit=20)
    assert result == {"items": [], "next_cursor": None}


async def _insert_call(shop_id: UUID, *, started: datetime, outcome: str | None = None,
                       caller: str = "+39000", customer_match: str = "unmatched") -> UUID:
    cid = uuid4()
    await execute_void(
        "INSERT INTO voice_agent.calls "
        "(id, shop_id, caller_number, customer_match, started_at, ended_at, "
        " duration_seconds, outcome, summary) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        cid, shop_id, caller, customer_match, started,
        started + timedelta(minutes=2), 120, outcome,
        f"summary for {cid}",
    )
    return cid


async def test_list_calls_pagination_and_filter(seeded_shop):
    now = datetime.now(timezone.utc)
    ids = []
    for i in range(5):
        ids.append(await _insert_call(seeded_shop, started=now - timedelta(hours=i),
                                       outcome="booked" if i % 2 == 0 else "abandoned"))
    page1 = await vq.list_calls(seeded_shop, filters={"outcome": ["booked"]},
                                cursor=None, limit=2)
    assert len(page1["items"]) == 2
    assert all(c["outcome"] == "booked" for c in page1["items"])


async def test_get_call_detail_includes_transcript_and_events(seeded_shop):
    now = datetime.now(timezone.utc)
    call_id = await _insert_call(seeded_shop, started=now, outcome="booked")
    await execute_void(
        "INSERT INTO voice_agent.call_transcripts (call_id, turn_index, role, text, at) "
        "VALUES ($1, 0, 'assistant', 'Ciao', $2), ($1, 1, 'caller', 'Ciao!', $2)",
        call_id, now,
    )
    await execute_void(
        "INSERT INTO voice_agent.call_events (call_id, type, payload) "
        "VALUES ($1, 'function_call', '{\"name\": \"book\"}'::jsonb)",
        call_id,
    )
    detail = await vq.get_call_detail(seeded_shop, call_id)
    assert detail["call"]["id"] == call_id
    assert len(detail["transcript"]) == 2
    assert detail["transcript"][0]["role"] == "assistant"
    assert len(detail["events"]) == 1


async def test_get_call_detail_wrong_shop_returns_none(seeded_shop):
    now = datetime.now(timezone.utc)
    call_id = await _insert_call(seeded_shop, started=now)
    other_shop = uuid4()
    assert await vq.get_call_detail(other_shop, call_id) is None


async def test_link_customer_to_call(seeded_shop):
    now = datetime.now(timezone.utc)
    call_id = await _insert_call(seeded_shop, started=now, customer_match="unmatched")
    cust = uuid4()
    await execute_void(
        "INSERT INTO customers (id, shop_id, full_name) VALUES ($1, $2, 'Mario')",
        cust, seeded_shop,
    )
    updated = await vq.link_customer(seeded_shop, call_id, cust)
    assert updated["customer_id"] == cust
    assert updated["customer_match"] == "existing"
```

- [ ] **Step 2: Run, verify failure**

`DATABASE_URL=... pytest tests/live_db/test_voice_queries.py -v -k "list_calls or call_detail or link_customer"` → AttributeError.

- [ ] **Step 3: Implement the new query functions**

Append to `booking_engine/db/voice_queries.py`:

```python
import base64
import json
from datetime import datetime


_CALL_SUMMARY_COLS = (
    "id, caller_number, customer_id, customer_match, started_at, ended_at, "
    "duration_seconds, outcome, summary, appointment_id"
)


def _encode_cursor(started_at: datetime, call_id: UUID) -> str:
    raw = json.dumps({"t": started_at.isoformat(), "id": str(call_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    obj = json.loads(raw)
    return datetime.fromisoformat(obj["t"]), UUID(obj["id"])


async def list_calls(
    shop_id: UUID,
    filters: dict,
    cursor: str | None,
    limit: int = 20,
) -> dict:
    """Filters: { outcome: list[str], from: datetime, to: datetime, q: str }."""
    limit = max(1, min(limit, 100))
    where = ["shop_id = $1"]
    params: list = [shop_id]

    outcomes = filters.get("outcome") or []
    if outcomes:
        params.append(outcomes)
        where.append(f"outcome = ANY(${len(params)})")
    if filters.get("from"):
        params.append(filters["from"])
        where.append(f"started_at >= ${len(params)}")
    if filters.get("to"):
        params.append(filters["to"])
        where.append(f"started_at <= ${len(params)}")
    if filters.get("q"):
        params.append(f"%{filters['q']}%")
        where.append(f"(caller_number ILIKE ${len(params)} OR summary ILIKE ${len(params)})")
    if cursor:
        cursor_dt, cursor_id = _decode_cursor(cursor)
        params.append(cursor_dt)
        params.append(cursor_id)
        where.append(
            f"(started_at, id) < (${len(params)-1}, ${len(params)})"
        )

    sql = (
        f"SELECT {_CALL_SUMMARY_COLS} FROM voice_agent.calls "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY started_at DESC, id DESC LIMIT {limit + 1}"
    )
    rows = await execute(sql, *params)
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last["started_at"], last["id"])
        rows = rows[:limit]
    return {"items": rows, "next_cursor": next_cursor}


async def get_call_detail(shop_id: UUID, call_id: UUID) -> dict | None:
    call = await execute_one(
        f"SELECT {_CALL_SUMMARY_COLS} FROM voice_agent.calls "
        "WHERE shop_id = $1 AND id = $2",
        shop_id, call_id,
    )
    if not call:
        return None
    transcript = await execute(
        "SELECT turn_index, role, text, at FROM voice_agent.call_transcripts "
        "WHERE call_id = $1 ORDER BY turn_index",
        call_id,
    )
    events = await execute(
        "SELECT at, type, payload FROM voice_agent.call_events "
        "WHERE call_id = $1 ORDER BY at",
        call_id,
    )
    return {"call": call, "transcript": transcript, "events": events}


async def link_customer(shop_id: UUID, call_id: UUID, customer_id: UUID) -> dict | None:
    await execute_void(
        "UPDATE voice_agent.calls SET customer_id = $3, customer_match = 'existing' "
        "WHERE shop_id = $1 AND id = $2",
        shop_id, call_id, customer_id,
    )
    return await execute_one(
        f"SELECT {_CALL_SUMMARY_COLS} FROM voice_agent.calls "
        "WHERE shop_id = $1 AND id = $2",
        shop_id, call_id,
    )
```

- [ ] **Step 4: Run tests, verify pass**

`DATABASE_URL=... pytest tests/live_db/test_voice_queries.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/voice_queries.py tests/live_db/test_voice_queries.py
git commit -m "feat(db): voice calls list, detail, link-customer queries"
```

---

### Task 7: Query function — analytics aggregator

**Files:**
- Modify: `booking_engine/db/voice_queries.py`
- Modify: `tests/live_db/test_voice_queries.py`

- [ ] **Step 1: Append failing test**

```python
async def test_get_analytics_empty(seeded_shop):
    a = await vq.get_analytics(seeded_shop, from_dt=None, to_dt=None)
    assert a["volume"]["total"] == 0
    assert a["outcomes"]["conversion_rate"] == 0.0
    assert a["demand"]["after_hours_pct"] == 0.0


async def test_get_analytics_counts(seeded_shop):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await _insert_call(seeded_shop, started=now, outcome="booked")
    await _insert_call(seeded_shop, started=now - timedelta(hours=1), outcome="abandoned")
    await _insert_call(seeded_shop, started=now - timedelta(hours=2), outcome="failed")
    a = await vq.get_analytics(seeded_shop, from_dt=None, to_dt=None)
    assert a["volume"]["total"] == 3
    assert a["outcomes"]["booked"] == 1
    assert a["outcomes"]["abandoned"] == 1
    assert a["outcomes"]["failed"] == 1
    assert a["outcomes"]["conversion_rate"] == pytest.approx(0.5)
    assert a["volume"]["failure_rate"] == pytest.approx(1 / 3)
```

- [ ] **Step 2: Run, verify failure**

`DATABASE_URL=... pytest tests/live_db/test_voice_queries.py -v -k analytics` → AttributeError.

- [ ] **Step 3: Implement `get_analytics`**

Append to `booking_engine/db/voice_queries.py`:

```python
_OUTCOME_KEYS = ("booked", "rescheduled", "cancelled", "info",
                 "abandoned", "escalated", "failed")


async def get_analytics(
    shop_id: UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> dict:
    where = ["shop_id = $1"]
    params: list = [shop_id]
    if from_dt:
        params.append(from_dt)
        where.append(f"started_at >= ${len(params)}")
    if to_dt:
        params.append(to_dt)
        where.append(f"started_at <= ${len(params)}")
    w = " AND ".join(where)

    totals = await execute_one(
        f"SELECT COUNT(*) AS total, "
        f"COALESCE(AVG(duration_seconds), 0)::int AS avg_dur, "
        f"COUNT(*) FILTER (WHERE outcome = 'failed') AS failed "
        f"FROM voice_agent.calls WHERE {w}",
        *params,
    )
    total = totals["total"] or 0
    failed = totals["failed"] or 0
    failure_rate = (failed / total) if total else 0.0

    by_day_rows = await execute(
        f"SELECT (started_at AT TIME ZONE 'Europe/Rome')::date AS d, COUNT(*) AS c "
        f"FROM voice_agent.calls WHERE {w} GROUP BY d ORDER BY d",
        *params,
    )
    by_day = [{"date": r["d"].isoformat(), "count": r["c"]} for r in by_day_rows]

    outcome_rows = await execute(
        f"SELECT outcome, COUNT(*) AS c FROM voice_agent.calls "
        f"WHERE {w} GROUP BY outcome",
        *params,
    )
    outcome_counts = {k: 0 for k in _OUTCOME_KEYS}
    for row in outcome_rows:
        if row["outcome"] in outcome_counts:
            outcome_counts[row["outcome"]] = row["c"]
    non_failed = total - outcome_counts["failed"]
    conversion = (outcome_counts["booked"] / non_failed) if non_failed else 0.0

    top_services_rows = await execute(
        f"SELECT sid AS service_id, COUNT(*) AS c "
        f"FROM voice_agent.calls c, UNNEST(c.requested_service_ids) AS sid "
        f"WHERE {w} GROUP BY sid ORDER BY c DESC LIMIT 10",
        *params,
    )
    # Resolve names in a second query (small N)
    service_ids = [r["service_id"] for r in top_services_rows]
    name_map: dict = {}
    if service_ids:
        for row in await execute(
            "SELECT id, service_name FROM services WHERE id = ANY($1)",
            service_ids,
        ):
            name_map[row["id"]] = row["service_name"]
    top_services = [
        {"service_id": str(r["service_id"]),
         "name": name_map.get(r["service_id"], ""),
         "count": r["c"]}
        for r in top_services_rows
    ]

    top_staff_rows = await execute(
        f"SELECT requested_staff_id AS staff_id, COUNT(*) AS c "
        f"FROM voice_agent.calls WHERE {w} AND requested_staff_id IS NOT NULL "
        f"GROUP BY staff_id ORDER BY c DESC LIMIT 10",
        *params,
    )
    staff_ids = [r["staff_id"] for r in top_staff_rows]
    staff_name_map: dict = {}
    if staff_ids:
        for row in await execute(
            "SELECT id, full_name FROM staff WHERE id = ANY($1)",
            staff_ids,
        ):
            staff_name_map[row["id"]] = row["full_name"]
    top_staff = [
        {"staff_id": str(r["staff_id"]),
         "name": staff_name_map.get(r["staff_id"], ""),
         "count": r["c"]}
        for r in top_staff_rows
    ]

    by_hour_rows = await execute(
        f"SELECT EXTRACT(HOUR FROM started_at AT TIME ZONE 'Europe/Rome')::int AS h, "
        f"COUNT(*) AS c FROM voice_agent.calls WHERE {w} GROUP BY h ORDER BY h",
        *params,
    )
    by_hour = [{"hour": r["h"], "count": r["c"]} for r in by_hour_rows]

    by_dow_rows = await execute(
        f"SELECT EXTRACT(ISODOW FROM started_at AT TIME ZONE 'Europe/Rome')::int AS d, "
        f"COUNT(*) AS c FROM voice_agent.calls WHERE {w} GROUP BY d ORDER BY d",
        *params,
    )
    by_dow = [{"dow": r["d"] - 1, "count": r["c"]} for r in by_dow_rows]  # 0=Monday

    # after-hours: count calls whose hour lies outside any staff schedule on that DOW
    after_hours = await execute_one(
        f"SELECT COUNT(*) AS c FROM voice_agent.calls c "
        f"WHERE {w} AND NOT EXISTS ("
        f"  SELECT 1 FROM staff s JOIN staff_schedules sch ON sch.staff_id = s.id "
        f"  WHERE s.shop_id = $1 AND s.is_active = true "
        f"    AND sch.day_of_week = (EXTRACT(ISODOW FROM c.started_at "
        f"        AT TIME ZONE 'Europe/Rome')::int - 1) "
        f"    AND TO_CHAR((c.started_at AT TIME ZONE 'Europe/Rome'), 'HH24:MI') "
        f"        BETWEEN sch.start_time AND sch.end_time"
        f")",
        *params,
    )
    after_hours_pct = (after_hours["c"] / total) if total else 0.0

    return {
        "volume": {
            "total": total,
            "by_day": by_day,
            "avg_duration_sec": totals["avg_dur"] or 0,
            "failure_rate": failure_rate,
        },
        "outcomes": {**outcome_counts, "conversion_rate": conversion},
        "demand": {
            "top_services": top_services,
            "top_staff": top_staff,
            "by_hour": by_hour,
            "by_dow": by_dow,
            "after_hours_pct": after_hours_pct,
        },
    }
```

- [ ] **Step 4: Run tests, verify pass**

`DATABASE_URL=... pytest tests/live_db/test_voice_queries.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/voice_queries.py tests/live_db/test_voice_queries.py
git commit -m "feat(db): voice analytics aggregator query"
```

---

### Task 8: Routes — GET/PATCH /voice/config

**Files:**
- Create: `booking_engine/api/routes/voice.py`
- Modify: `booking_engine/api/app.py`
- Test: `tests/booking_engine/test_voice_routes.py`

- [ ] **Step 1: Write failing tests using FastAPI TestClient with DB mocked**

```python
"""Route handler tests for /voice/* endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from booking_engine.api.routes import voice
from booking_engine.config import Settings


def _app(secret: str = "test-secret") -> FastAPI:
    app = FastAPI()
    app.include_router(voice.router, prefix="/api/v1")
    # Override settings dep
    from booking_engine.api import deps
    app.dependency_overrides[deps._get_settings] = lambda: Settings(
        control_plane_secret=secret,
    )
    return app


HEADERS = {"Authorization": "Bearer test-secret"}


def test_get_config_unauthorized():
    client = TestClient(_app())
    r = client.get(f"/api/v1/shops/{uuid4()}/voice/config")
    assert r.status_code == 401


def test_get_config_not_found():
    with patch.object(voice.vq, "get_voice_config", AsyncMock(return_value=None)):
        client = TestClient(_app())
        r = client.get(f"/api/v1/shops/{uuid4()}/voice/config", headers=HEADERS)
        assert r.status_code == 404


def test_get_config_ok():
    fake = {
        "welcome_message": "Ciao",
        "tone_instructions": None, "personality": None, "special_instructions": None,
        "voice": "alloy", "language": "it", "is_active": True,
    }
    with patch.object(voice.vq, "get_voice_config", AsyncMock(return_value=fake)):
        client = TestClient(_app())
        r = client.get(f"/api/v1/shops/{uuid4()}/voice/config", headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["data"]["voice"] == "alloy"


def test_patch_config_validates_body():
    client = TestClient(_app())
    r = client.patch(
        f"/api/v1/shops/{uuid4()}/voice/config",
        headers=HEADERS, json={"language": "fr"},
    )
    assert r.status_code == 422


def test_patch_config_updates_and_returns_config():
    fake = {
        "welcome_message": "Aggiornato",
        "tone_instructions": None, "personality": None, "special_instructions": None,
        "voice": "echo", "language": "it", "is_active": True,
    }
    with patch.object(voice.vq, "update_voice_config",
                      AsyncMock(return_value=fake)) as upd:
        client = TestClient(_app())
        r = client.patch(
            f"/api/v1/shops/{uuid4()}/voice/config",
            headers=HEADERS, json={"welcome_message": "Aggiornato", "voice": "echo"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["voice"] == "echo"
        upd.assert_awaited_once()
```

- [ ] **Step 2: Run, verify failure**

`pytest tests/booking_engine/test_voice_routes.py -v` → ImportError.

- [ ] **Step 3: Create the routes file**

```python
"""Control-plane endpoints for the voice agent."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from booking_engine.api.deps import require_control_plane_token
from booking_engine.api.voice_models import (
    VoiceConfigResponse,
    VoiceConfigUpdateRequest,
    CallSummary,
    CallDetail,
    TranscriptTurn,
    CallEvent,
    LinkCustomerRequest,
    VoiceAnalyticsResponse,
)
from booking_engine.db import voice_queries as vq

router = APIRouter(
    tags=["voice"],
    dependencies=[Depends(require_control_plane_token)],
)


def _wrap(data) -> dict:
    return {"data": data}


@router.get("/shops/{shop_id}/voice/config")
async def get_voice_config(shop_id: UUID):
    cfg = await vq.get_voice_config(shop_id)
    if not cfg:
        return JSONResponse(
            status_code=404,
            content={"error": "shop_not_found", "message": f"Shop {shop_id} not found"},
        )
    return _wrap(VoiceConfigResponse(**cfg).model_dump(mode="json"))


@router.patch("/shops/{shop_id}/voice/config")
async def patch_voice_config(shop_id: UUID, body: VoiceConfigUpdateRequest):
    patch_dict = body.model_dump(exclude_unset=True)
    cfg = await vq.update_voice_config(shop_id, patch_dict)
    if not cfg:
        return JSONResponse(
            status_code=404,
            content={"error": "shop_not_found", "message": f"Shop {shop_id} not found"},
        )
    return _wrap(VoiceConfigResponse(**cfg).model_dump(mode="json"))
```

- [ ] **Step 4: Register router in `booking_engine/api/app.py`**

Inside `create_app`, in the existing route-include block, add:
```python
    from booking_engine.api.routes import voice  # noqa: WPS433
    app.include_router(voice.router, prefix="/api/v1")
```

- [ ] **Step 5: Run tests, verify pass**

`pytest tests/booking_engine/test_voice_routes.py -v -k config` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add booking_engine/api/routes/voice.py booking_engine/api/app.py tests/booking_engine/test_voice_routes.py
git commit -m "feat(api): GET/PATCH /voice/config endpoints"
```

---

### Task 9: Routes — GET /voice/calls (list) and GET /voice/calls/{id} (detail)

**Files:**
- Modify: `booking_engine/api/routes/voice.py`
- Modify: `tests/booking_engine/test_voice_routes.py`

- [ ] **Step 1: Append failing tests**

```python
def test_list_calls_filters_passed_through():
    fake = {"items": [], "next_cursor": None}
    with patch.object(voice.vq, "list_calls",
                      AsyncMock(return_value=fake)) as lc:
        client = TestClient(_app())
        shop = uuid4()
        r = client.get(
            f"/api/v1/shops/{shop}/voice/calls"
            f"?outcome=booked&outcome=abandoned&q=mario&limit=5",
            headers=HEADERS,
        )
        assert r.status_code == 200
        body = r.json()["data"]
        assert body == []
        kwargs = lc.await_args.kwargs
        assert kwargs["filters"]["outcome"] == ["booked", "abandoned"]
        assert kwargs["filters"]["q"] == "mario"
        assert kwargs["limit"] == 5


def test_list_calls_returns_items_and_cursor():
    item = {
        "id": uuid4(), "caller_number": "+39", "customer_id": None,
        "customer_match": "unmatched",
        "started_at": datetime.now(timezone.utc),
        "ended_at": None, "duration_seconds": None,
        "outcome": None, "summary": None, "appointment_id": None,
    }
    with patch.object(voice.vq, "list_calls",
                      AsyncMock(return_value={"items": [item], "next_cursor": "abc"})):
        client = TestClient(_app())
        r = client.get(f"/api/v1/shops/{uuid4()}/voice/calls", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert len(body["data"]) == 1
        assert body["next_cursor"] == "abc"


def test_get_call_detail_404():
    with patch.object(voice.vq, "get_call_detail", AsyncMock(return_value=None)):
        client = TestClient(_app())
        r = client.get(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}",
            headers=HEADERS,
        )
        assert r.status_code == 404


def test_get_call_detail_ok():
    call = {
        "id": uuid4(), "caller_number": "+39", "customer_id": None,
        "customer_match": "existing",
        "started_at": datetime.now(timezone.utc),
        "ended_at": None, "duration_seconds": None,
        "outcome": "booked", "summary": "ok", "appointment_id": None,
    }
    turn = {"turn_index": 0, "role": "assistant", "text": "Ciao",
            "at": datetime.now(timezone.utc)}
    ev = {"at": datetime.now(timezone.utc), "type": "function_call", "payload": {}}
    with patch.object(voice.vq, "get_call_detail", AsyncMock(return_value={
        "call": call, "transcript": [turn], "events": [ev],
    })):
        client = TestClient(_app())
        r = client.get(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}",
            headers=HEADERS,
        )
        assert r.status_code == 200
        assert len(r.json()["data"]["transcript"]) == 1
```

- [ ] **Step 2: Run, verify failure**

`pytest tests/booking_engine/test_voice_routes.py -v -k "list_calls or call_detail"` → 404 / AttributeError.

- [ ] **Step 3: Add handlers to `voice.py`**

```python
@router.get("/shops/{shop_id}/voice/calls")
async def list_calls(
    shop_id: UUID,
    outcome: Annotated[list[str] | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    filters = {"outcome": outcome, "from": from_, "to": to, "q": q}
    result = await vq.list_calls(shop_id, filters=filters, cursor=cursor, limit=limit)
    items = [CallSummary(**r).model_dump(mode="json") for r in result["items"]]
    return {"data": items, "next_cursor": result["next_cursor"]}


@router.get("/shops/{shop_id}/voice/calls/{call_id}")
async def get_call_detail(shop_id: UUID, call_id: UUID):
    detail = await vq.get_call_detail(shop_id, call_id)
    if not detail:
        return JSONResponse(
            status_code=404,
            content={"error": "call_not_found", "message": f"Call {call_id} not found"},
        )
    payload = CallDetail(
        call=CallSummary(**detail["call"]),
        transcript=[TranscriptTurn(**t) for t in detail["transcript"]],
        events=[CallEvent(**e) for e in detail["events"]],
    )
    return _wrap(payload.model_dump(mode="json"))
```

- [ ] **Step 4: Run tests, verify pass**

`pytest tests/booking_engine/test_voice_routes.py -v` → all PASS so far.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice.py tests/booking_engine/test_voice_routes.py
git commit -m "feat(api): GET /voice/calls (list) and /voice/calls/{id} (detail)"
```

---

### Task 10: Route — PATCH /voice/calls/{id}/link-customer

**Files:**
- Modify: `booking_engine/api/routes/voice.py`
- Modify: `tests/booking_engine/test_voice_routes.py`

- [ ] **Step 1: Append failing test**

```python
def test_link_customer_validates_body():
    client = TestClient(_app())
    r = client.patch(
        f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}/link-customer",
        headers=HEADERS, json={"customer_id": "not-a-uuid"},
    )
    assert r.status_code == 422


def test_link_customer_ok():
    updated = {
        "id": uuid4(), "caller_number": "+39",
        "customer_id": uuid4(), "customer_match": "existing",
        "started_at": datetime.now(timezone.utc),
        "ended_at": None, "duration_seconds": None,
        "outcome": None, "summary": None, "appointment_id": None,
    }
    with patch.object(voice.vq, "link_customer",
                      AsyncMock(return_value=updated)):
        client = TestClient(_app())
        r = client.patch(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}/link-customer",
            headers=HEADERS,
            json={"customer_id": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["customer_match"] == "existing"


def test_link_customer_404():
    with patch.object(voice.vq, "link_customer", AsyncMock(return_value=None)):
        client = TestClient(_app())
        r = client.patch(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}/link-customer",
            headers=HEADERS, json={"customer_id": str(uuid4())},
        )
        assert r.status_code == 404
```

- [ ] **Step 2: Run, verify failure**

`pytest tests/booking_engine/test_voice_routes.py -v -k link_customer` → 404.

- [ ] **Step 3: Add handler**

Append to `voice.py`:

```python
@router.patch("/shops/{shop_id}/voice/calls/{call_id}/link-customer")
async def link_customer(shop_id: UUID, call_id: UUID, body: LinkCustomerRequest):
    updated = await vq.link_customer(shop_id, call_id, body.customer_id)
    if not updated:
        return JSONResponse(
            status_code=404,
            content={"error": "call_not_found", "message": f"Call {call_id} not found"},
        )
    return _wrap(CallSummary(**updated).model_dump(mode="json"))
```

- [ ] **Step 4: Run tests, verify pass**

`pytest tests/booking_engine/test_voice_routes.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice.py tests/booking_engine/test_voice_routes.py
git commit -m "feat(api): PATCH /voice/calls/{id}/link-customer"
```

---

### Task 11: Route — GET /voice/analytics

**Files:**
- Modify: `booking_engine/api/routes/voice.py`
- Modify: `tests/booking_engine/test_voice_routes.py`

- [ ] **Step 1: Append failing test**

```python
def test_get_analytics_ok():
    fake = {
        "volume": {"total": 3, "by_day": [], "avg_duration_sec": 120,
                   "failure_rate": 0.0},
        "outcomes": {"booked": 2, "rescheduled": 0, "cancelled": 0,
                     "info": 0, "abandoned": 1, "escalated": 0, "failed": 0,
                     "conversion_rate": 2/3},
        "demand": {"top_services": [], "top_staff": [],
                   "by_hour": [], "by_dow": [], "after_hours_pct": 0.0},
    }
    with patch.object(voice.vq, "get_analytics",
                      AsyncMock(return_value=fake)) as ga:
        client = TestClient(_app())
        r = client.get(
            f"/api/v1/shops/{uuid4()}/voice/analytics"
            f"?from=2026-05-01T00:00:00Z&to=2026-05-31T23:59:59Z",
            headers=HEADERS,
        )
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["volume"]["total"] == 3
        assert body["outcomes"]["booked"] == 2
        ga.assert_awaited_once()
```

- [ ] **Step 2: Run, verify failure**

`pytest tests/booking_engine/test_voice_routes.py -v -k analytics` → 404.

- [ ] **Step 3: Add handler**

```python
@router.get("/shops/{shop_id}/voice/analytics")
async def get_analytics(
    shop_id: UUID,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
):
    result = await vq.get_analytics(shop_id, from_dt=from_, to_dt=to)
    return _wrap(VoiceAnalyticsResponse(**result).model_dump(mode="json"))
```

- [ ] **Step 4: Run tests, verify pass**

`pytest tests/booking_engine/test_voice_routes.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice.py tests/booking_engine/test_voice_routes.py
git commit -m "feat(api): GET /voice/analytics"
```

---

### Task 12: Integration guide update

**Files:**
- Modify: `docs/INTEGRATION_GUIDE.md`

- [ ] **Step 1: Add a "Voice Agent Control Plane" section after the existing "What the Control Plane Must Build" section**

Insert the following section (replace where the existing "Analytics (future)" subsection ends):

```markdown
---

## Voice Agent Control Plane (Schema: `voice_agent`)

The voice service persists every inbound call into a dedicated schema. The Control Plane reads this data via HTTP — never via direct SQL.

### Ownership

| Concern | Execution Layer | Control Plane |
|---------|:-:|:-:|
| `voice_agent.calls` table | Write (gateway) + Read (control endpoints) | None directly (HTTP only) |
| `voice_agent.call_transcripts` | Write (gateway) + Read (control endpoints) | None directly (HTTP only) |
| `voice_agent.call_events` | Write (gateway) + Read (control endpoints) | None directly (HTTP only) |
| `shops.voice`, `shops.language` (new columns) | Read (gateway) | Read + Write via control-plane API |
| Existing voice prompt fields on `shops` | Read (gateway) | Read + Write via control-plane API |

### Schema

See `booking_engine/db/sql/03_voice_agent_schema.sql` for the authoritative DDL.

### New REST endpoints

All require `Authorization: Bearer ${CONTROL_PLANE_SECRET}`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`   | `/api/v1/shops/{shop_id}/voice/config` | Read voice-agent config |
| `PATCH` | `/api/v1/shops/{shop_id}/voice/config` | Update config (any subset of fields) |
| `GET`   | `/api/v1/shops/{shop_id}/voice/calls` | Paginated list with filters |
| `GET`   | `/api/v1/shops/{shop_id}/voice/calls/{call_id}` | Full call detail (summary + transcript + events) |
| `PATCH` | `/api/v1/shops/{shop_id}/voice/calls/{call_id}/link-customer` | Manually link an unmatched call to a customer |
| `GET`   | `/api/v1/shops/{shop_id}/voice/analytics` | Volume + outcomes + demand aggregates |

### Outcome enum

`booked | rescheduled | cancelled | info | abandoned | escalated | failed`

### Customer match enum

`existing | created | unmatched | ambiguous`
```

- [ ] **Step 2: Commit**

```bash
git add docs/INTEGRATION_GUIDE.md
git commit -m "docs: voice agent control-plane schema and endpoints"
```

---

### Task 13: End-to-end smoke check against a live branch

**Files:** none (manual verification)

- [ ] **Step 1: Boot the booking engine locally**

```bash
CONTROL_PLANE_SECRET=local-secret DATABASE_URL=postgresql://... \
  uvicorn booking_engine.api.app:create_app --factory --port 8000
```

- [ ] **Step 2: Hit each endpoint with curl**

```bash
SHOP_ID=<existing shop UUID>
H='Authorization: Bearer local-secret'

curl -s -H "$H" "http://localhost:8000/api/v1/shops/$SHOP_ID/voice/config" | jq
curl -s -H "$H" -X PATCH "http://localhost:8000/api/v1/shops/$SHOP_ID/voice/config" \
  -H 'Content-Type: application/json' -d '{"welcome_message":"Test"}' | jq
curl -s -H "$H" "http://localhost:8000/api/v1/shops/$SHOP_ID/voice/calls" | jq
curl -s -H "$H" "http://localhost:8000/api/v1/shops/$SHOP_ID/voice/analytics" | jq
```
Expected: 200 with valid `{ "data": ... }` payloads. Calls list returns `{"data": [], "next_cursor": null}` for shops with no calls.

- [ ] **Step 2b: Hit unauthorized requests**

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://localhost:8000/api/v1/shops/$SHOP_ID/voice/config"
```
Expected: `401`.

- [ ] **Step 3: Deploy**

```bash
AWS_REGION=eu-central-1 DATABASE_URL=postgresql://... \
  CONTROL_PLANE_SECRET=$(openssl rand -hex 32) \
  ./scripts/deploy-booking.sh
```

(Adjust deploy script as needed to plumb the new env var into Lambda.)

- [ ] **Step 4: Note the deployed Function URL and the secret value**

These will be plugged into the webapp as `VOICE_AGENT_API_URL` and `VOICE_AGENT_SECRET` in the next plan.

- [ ] **Step 5: Final commit (if any deploy script tweaks)**

```bash
git add scripts/
git commit -m "chore(deploy): pass CONTROL_PLANE_SECRET to Lambda"
```
