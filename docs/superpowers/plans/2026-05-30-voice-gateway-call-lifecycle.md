# Voice Gateway Call-Lifecycle Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Voice Gateway persist every call into the `voice_agent` schema: insert a `calls` row at session start (with phone→customer reconciliation), append every conversational turn into `call_transcripts`, log function calls / errors into `call_events`, and on hangup classify the call into `{outcome, outcome_reason, summary}` via a single OpenAI text request, then UPDATE the call row with that classification + `ended_at` + `duration_seconds`. Read `shops.voice` and `shops.language` when building the OpenAI Realtime session config.

**Architecture:** A new module `voice_gateway/call_lifecycle.py` exposes one class `CallSession` plus a small `classify_call()` helper. The realtime route (`voice_gateway/api/routes/realtime.py`) creates a `CallSession` at token issuance, attaches it to the WebRTC session, and the session's methods are called from the existing flow (token issued = start; function-call proxy = event log; transcript turns = transcript append). On hangup (signaled by client `/api/v1/realtime/end?call_id=...`), the gateway calls `classify_call()`, persists the outcome, and stamps the call. The voice gateway connects to the same Neon DB the Booking Engine uses (new `DATABASE_URL` setting added to the gateway).

**Tech Stack:** FastAPI, asyncpg (new dependency on the gateway), httpx (already present), OpenAI Python SDK (new — for the classification call). Reuse the booking-engine connection helpers by importing them.

**Prerequisites:**
- Plan 1 (`2026-05-30-voice-agent-schema-and-endpoints.md`) is fully shipped: schema, control-plane endpoints, and the `CONTROL_PLANE_SECRET` deployed.
- Voice gateway has Neon `DATABASE_URL` provisioned as a Fly secret.

**Spec:** `docs/superpowers/specs/2026-05-30-inbox-voice-agent-redesign.md` (lives in the webapp repo).

---

## File Structure

**Create:**
- `voice_gateway/call_lifecycle.py` — `CallSession` class + `classify_call()` helper
- `voice_gateway/db.py` — thin asyncpg pool init (reuses connection patterns from `booking_engine`)
- `voice_gateway/clients/openai_classifier.py` — small wrapper around the Responses API for outcome classification
- `tests/voice_gateway/test_call_lifecycle.py` — unit tests with DB mocked
- `tests/voice_gateway/test_openai_classifier.py` — wrapper unit tests with httpx mocked
- `tests/live_db/test_voice_gateway_persistence.py` — end-to-end persistence test against live DB

**Modify:**
- `voice_gateway/config.py` — add `database_url`, `openai_classifier_model` settings
- `voice_gateway/api/app.py` — init DB pool in lifespan
- `voice_gateway/api/routes/realtime.py` — call lifecycle hooks at token issuance, function-call proxy, and new end-call endpoint
- `voice_gateway/requirements.txt` — add `asyncpg`, `openai`

---

## Conventions

- Unit tests: `pytest tests/voice_gateway/ -v`
- Live-DB tests: `DATABASE_URL=... pytest tests/live_db/test_voice_gateway_persistence.py -v`
- Commit after every passing test

---

### Task 1: Add asyncpg pool to voice gateway

**Files:**
- Create: `voice_gateway/db.py`
- Modify: `voice_gateway/config.py`
- Modify: `voice_gateway/api/app.py`
- Modify: `voice_gateway/requirements.txt`
- Test: `tests/voice_gateway/test_db.py`

- [ ] **Step 1: Add `asyncpg` to requirements**

Append to `voice_gateway/requirements.txt`:
```
asyncpg==0.30.0
```

- [ ] **Step 2: Add `database_url` and classifier model to Settings**

Edit `voice_gateway/config.py`. Add fields to the `Settings` class:
```python
    database_url: str = ""
    openai_classifier_model: str = "gpt-4o-mini"
```

- [ ] **Step 3: Write failing test**

Create `tests/voice_gateway/test_db.py`:

```python
"""Tests for the voice gateway DB pool helpers."""
from __future__ import annotations

import pytest

from voice_gateway.db import init_pool, close_pool, execute, execute_one, execute_void


@pytest.mark.asyncio
async def test_pool_raises_when_not_initialized(monkeypatch):
    monkeypatch.setattr("voice_gateway.db._pool", None, raising=False)
    with pytest.raises(RuntimeError):
        await execute("SELECT 1")
```

- [ ] **Step 4: Run test, verify failure**

`pytest tests/voice_gateway/test_db.py -v` → ImportError.

- [ ] **Step 5: Implement `voice_gateway/db.py`**

```python
"""asyncpg pool helpers for the voice gateway."""
from __future__ import annotations

from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(database_url: str, min_size: int = 1, max_size: int = 4) -> None:
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=min_size, max_size=max_size)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("voice_gateway DB pool not initialized")
    return _pool


async def execute(query: str, *args: Any) -> list[dict]:
    async with _require_pool().acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]


async def execute_one(query: str, *args: Any) -> dict | None:
    async with _require_pool().acquire() as conn:
        row = await conn.fetchrow(query, *args)
        return dict(row) if row else None


async def execute_void(query: str, *args: Any) -> None:
    async with _require_pool().acquire() as conn:
        await conn.execute(query, *args)
```

- [ ] **Step 6: Run test, verify pass**

`pytest tests/voice_gateway/test_db.py -v` → PASS.

- [ ] **Step 7: Wire into the app lifespan**

In `voice_gateway/api/app.py`, inside `lifespan`, after the booking client init:

```python
    from voice_gateway.db import init_pool, close_pool
    if settings.database_url:
        await init_pool(settings.database_url)
        logger.info("Voice gateway DB pool ready")
```

And in the teardown block:

```python
    await close_pool()
```

- [ ] **Step 8: Commit**

```bash
git add voice_gateway/config.py voice_gateway/db.py voice_gateway/api/app.py voice_gateway/requirements.txt tests/voice_gateway/test_db.py
git commit -m "feat(gateway): asyncpg pool for voice_agent schema writes"
```

---

### Task 2: Classification client (OpenAI Responses API)

**Files:**
- Create: `voice_gateway/clients/openai_classifier.py`
- Create: `tests/voice_gateway/test_openai_classifier.py`

- [ ] **Step 1: Write failing test**

```python
"""Tests for the OpenAI outcome classifier wrapper."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from voice_gateway.clients.openai_classifier import classify_call


@pytest.mark.asyncio
async def test_classify_call_parses_json_response():
    fake = AsyncMock()
    fake.post = AsyncMock(return_value=type("R", (), {
        "status_code": 200,
        "json": lambda self: {
            "output": [{
                "content": [{
                    "type": "output_text",
                    "text": json.dumps({
                        "outcome": "booked",
                        "outcome_reason": "Cliente ha prenotato taglio venerdi",
                        "summary": "Maria ha prenotato taglio venerdi alle 10:00",
                    }),
                }],
            }],
        },
        "raise_for_status": lambda self: None,
    })())
    with patch("voice_gateway.clients.openai_classifier.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = fake
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[{"role": "assistant", "text": "Ciao!"},
                        {"role": "caller", "text": "Vorrei prenotare"}],
            booked_appointment_id="abc",
        )
    assert result["outcome"] == "booked"
    assert "Maria" in result["summary"]


@pytest.mark.asyncio
async def test_classify_call_invalid_response_returns_failed():
    fake = AsyncMock()
    fake.post = AsyncMock(return_value=type("R", (), {
        "status_code": 200,
        "json": lambda self: {"output": [{"content": [{"type": "output_text",
                                                       "text": "not json"}]}]},
        "raise_for_status": lambda self: None,
    })())
    with patch("voice_gateway.clients.openai_classifier.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = fake
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["outcome"] == "failed"
    assert result["outcome_reason"] == "classification_invalid"


@pytest.mark.asyncio
async def test_classify_call_unknown_outcome_normalized_to_failed():
    fake = AsyncMock()
    fake.post = AsyncMock(return_value=type("R", (), {
        "status_code": 200,
        "json": lambda self: {"output": [{"content": [{"type": "output_text",
                                                       "text": json.dumps({
            "outcome": "definitely-not-real", "outcome_reason": "x", "summary": "x",
        })}]}]},
        "raise_for_status": lambda self: None,
    })())
    with patch("voice_gateway.clients.openai_classifier.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = fake
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["outcome"] == "failed"
```

- [ ] **Step 2: Run, verify failure**

`pytest tests/voice_gateway/test_openai_classifier.py -v` → ImportError.

- [ ] **Step 3: Implement the classifier**

```python
"""Single-shot OpenAI Responses API call to classify a finished phone call."""
from __future__ import annotations

import json
from typing import Any

import httpx

_VALID_OUTCOMES = {
    "booked", "rescheduled", "cancelled", "info",
    "abandoned", "escalated", "failed",
}

_PROMPT = (
    "Sei un classificatore. Ricevi la trascrizione di una telefonata gia "
    "terminata fra un assistente vocale di un salone/ristorante/officina e "
    "un cliente. Restituisci SOLO JSON con tre campi: "
    "outcome (uno fra booked, rescheduled, cancelled, info, abandoned, "
    "escalated, failed), outcome_reason (frase breve in italiano), summary "
    "(1-2 frasi in italiano). Se l ID di un appuntamento e fornito, "
    "considera l esito 'booked' a meno che la trascrizione lo contraddica."
)


def _normalize(raw: dict[str, Any]) -> dict[str, str]:
    outcome = raw.get("outcome", "failed")
    if outcome not in _VALID_OUTCOMES:
        return {
            "outcome": "failed",
            "outcome_reason": "classification_invalid",
            "summary": str(raw)[:500],
        }
    return {
        "outcome": outcome,
        "outcome_reason": str(raw.get("outcome_reason", ""))[:500],
        "summary": str(raw.get("summary", ""))[:500],
    }


async def classify_call(
    *,
    api_key: str,
    model: str,
    transcript: list[dict[str, str]],
    booked_appointment_id: str | None,
) -> dict[str, str]:
    """Returns dict with keys outcome, outcome_reason, summary."""
    transcript_text = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    if booked_appointment_id:
        transcript_text += f"\n\n(Appuntamento creato: {booked_appointment_id})"

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": transcript_text or "(nessuna trascrizione disponibile)"},
        ],
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code != 200:
            return {"outcome": "failed",
                    "outcome_reason": f"classifier_http_{resp.status_code}",
                    "summary": ""}
        data = resp.json()
    try:
        text = data["output"][0]["content"][0]["text"]
        parsed = json.loads(text)
    except Exception:
        return {"outcome": "failed", "outcome_reason": "classification_invalid", "summary": ""}
    return _normalize(parsed)
```

- [ ] **Step 4: Run tests, verify pass**

`pytest tests/voice_gateway/test_openai_classifier.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add voice_gateway/clients/openai_classifier.py tests/voice_gateway/test_openai_classifier.py
git commit -m "feat(gateway): outcome classifier wrapper for OpenAI Responses API"
```

---

### Task 3: CallSession — lifecycle module

**Files:**
- Create: `voice_gateway/call_lifecycle.py`
- Create: `tests/voice_gateway/test_call_lifecycle.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for CallSession lifecycle (DB mocked at module-function level)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from voice_gateway.call_lifecycle import CallSession


@pytest.mark.asyncio
async def test_start_inserts_call_row_with_existing_match():
    shop = uuid4()
    customer_id = uuid4()
    with (
        patch("voice_gateway.call_lifecycle.execute",
              AsyncMock(return_value=[{"id": customer_id}])) as ex,
        patch("voice_gateway.call_lifecycle.execute_void",
              AsyncMock()) as ev,
    ):
        sess = CallSession(shop_id=shop, caller_number="+390000", twilio_call_sid="CA1")
        await sess.start()
        # First execute: lookup phone_contacts; then execute_void INSERT calls row
        ex.assert_awaited()
        ev.assert_awaited()
        assert sess.customer_match == "existing"
        assert sess.customer_id == customer_id
        assert sess.id is not None


@pytest.mark.asyncio
async def test_start_unmatched_when_no_phone_contact():
    with (
        patch("voice_gateway.call_lifecycle.execute",
              AsyncMock(return_value=[])),
        patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()),
    ):
        sess = CallSession(shop_id=uuid4(), caller_number="+390000", twilio_call_sid="CA2")
        await sess.start()
        assert sess.customer_match == "unmatched"
        assert sess.customer_id is None


@pytest.mark.asyncio
async def test_start_ambiguous_when_multiple_customers():
    cid1, cid2 = uuid4(), uuid4()
    with (
        patch("voice_gateway.call_lifecycle.execute",
              AsyncMock(return_value=[{"id": cid1}, {"id": cid2}])),
        patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()),
    ):
        sess = CallSession(shop_id=uuid4(), caller_number="+390000", twilio_call_sid="CA3")
        await sess.start()
        assert sess.customer_match == "ambiguous"
        assert sess.customer_id is None


@pytest.mark.asyncio
async def test_append_turn_writes_transcript():
    with patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()) as ev:
        sess = CallSession(shop_id=uuid4(), caller_number="+39",
                           twilio_call_sid=None)
        sess.id = uuid4()
        await sess.append_turn(role="assistant", text="Ciao",
                               at=datetime.now(timezone.utc))
        ev.assert_awaited_once()
        sql = ev.await_args.args[0]
        assert "call_transcripts" in sql


@pytest.mark.asyncio
async def test_log_event_writes_call_event():
    with patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()) as ev:
        sess = CallSession(shop_id=uuid4(), caller_number="+39",
                           twilio_call_sid=None)
        sess.id = uuid4()
        await sess.log_event("function_call", {"name": "book", "args": {}})
        sql = ev.await_args.args[0]
        assert "call_events" in sql


@pytest.mark.asyncio
async def test_set_appointment_remembers_id():
    sess = CallSession(shop_id=uuid4(), caller_number="+39", twilio_call_sid=None)
    aid = uuid4()
    sess.set_appointment(aid)
    assert sess.appointment_id == aid


@pytest.mark.asyncio
async def test_finalize_updates_call_with_outcome():
    classifier = AsyncMock(return_value={
        "outcome": "booked", "outcome_reason": "ok", "summary": "Maria booked",
    })
    with patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()) as ev:
        sess = CallSession(shop_id=uuid4(), caller_number="+39", twilio_call_sid=None)
        sess.id = uuid4()
        sess.transcript = [{"role": "caller", "text": "Vorrei prenotare"}]
        sess.set_appointment(uuid4())
        await sess.finalize(classifier=classifier, api_key="sk", model="gpt")
        classifier.assert_awaited_once()
        sql = ev.await_args.args[0]
        assert "UPDATE voice_agent.calls" in sql
```

- [ ] **Step 2: Run, verify failure**

`pytest tests/voice_gateway/test_call_lifecycle.py -v` → ImportError.

- [ ] **Step 3: Implement CallSession**

```python
"""CallSession — owns the persistence lifecycle for one inbound phone call."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Awaitable
from uuid import UUID, uuid4

from voice_gateway.db import execute, execute_one, execute_void


CustomerMatch = str  # 'existing'|'created'|'unmatched'|'ambiguous'


class CallSession:
    """Tracks the lifecycle of a single phone call from start to hangup."""

    def __init__(
        self,
        *,
        shop_id: UUID,
        caller_number: str,
        twilio_call_sid: str | None,
    ) -> None:
        self.shop_id = shop_id
        self.caller_number = caller_number
        self.twilio_call_sid = twilio_call_sid
        self.id: UUID | None = None
        self.customer_id: UUID | None = None
        self.customer_match: CustomerMatch = "unmatched"
        self.started_at: datetime | None = None
        self.appointment_id: UUID | None = None
        self.transcript: list[dict[str, str]] = []

    async def start(self) -> None:
        """Insert the calls row and resolve caller→customer."""
        matches = await execute(
            "SELECT c.id FROM customers c "
            "JOIN phone_contacts pc ON c.id = pc.customer_id "
            "WHERE c.shop_id = $1 AND pc.phone_number = $2",
            self.shop_id, self.caller_number,
        )
        if len(matches) == 0:
            self.customer_match = "unmatched"
        elif len(matches) == 1:
            self.customer_id = matches[0]["id"]
            self.customer_match = "existing"
        else:
            self.customer_match = "ambiguous"

        self.id = uuid4()
        self.started_at = datetime.now(timezone.utc)
        await execute_void(
            "INSERT INTO voice_agent.calls "
            "(id, shop_id, twilio_call_sid, caller_number, customer_id, "
            " customer_match, started_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            self.id, self.shop_id, self.twilio_call_sid, self.caller_number,
            self.customer_id, self.customer_match, self.started_at,
        )

    async def attach_new_customer(self, customer_id: UUID) -> None:
        """Called by the booking-client wrapper when the AI creates a customer mid-call."""
        self.customer_id = customer_id
        self.customer_match = "created"
        await execute_void(
            "UPDATE voice_agent.calls SET customer_id = $1, customer_match = 'created' "
            "WHERE id = $2",
            customer_id, self.id,
        )

    async def append_turn(self, *, role: str, text: str, at: datetime) -> None:
        if self.id is None:
            return
        turn_index = len(self.transcript)
        self.transcript.append({"role": role, "text": text})
        await execute_void(
            "INSERT INTO voice_agent.call_transcripts "
            "(call_id, turn_index, role, text, at) VALUES ($1, $2, $3, $4, $5)",
            self.id, turn_index, role, text, at,
        )

    async def log_event(self, type_: str, payload: dict[str, Any]) -> None:
        if self.id is None:
            return
        await execute_void(
            "INSERT INTO voice_agent.call_events (call_id, type, payload) "
            "VALUES ($1, $2, $3::jsonb)",
            self.id, type_, payload,
        )

    def set_appointment(self, appointment_id: UUID) -> None:
        self.appointment_id = appointment_id

    async def finalize(
        self,
        *,
        classifier: Callable[..., Awaitable[dict[str, str]]],
        api_key: str,
        model: str,
    ) -> None:
        """Hangup: run classifier, write outcome + ended_at + duration_seconds."""
        if self.id is None or self.started_at is None:
            return
        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - self.started_at).total_seconds())
        result = await classifier(
            api_key=api_key, model=model,
            transcript=self.transcript,
            booked_appointment_id=str(self.appointment_id) if self.appointment_id else None,
        )
        await execute_void(
            "UPDATE voice_agent.calls SET "
            "  ended_at = $1, duration_seconds = $2, "
            "  outcome = $3, outcome_reason = $4, summary = $5, "
            "  appointment_id = $6 "
            "WHERE id = $7",
            ended_at, duration,
            result["outcome"], result["outcome_reason"], result["summary"],
            self.appointment_id, self.id,
        )
```

- [ ] **Step 4: Run tests, verify pass**

`pytest tests/voice_gateway/test_call_lifecycle.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add voice_gateway/call_lifecycle.py tests/voice_gateway/test_call_lifecycle.py
git commit -m "feat(gateway): CallSession lifecycle module with phone reconciliation"
```

---

### Task 4: Wire CallSession into the realtime route

**Files:**
- Modify: `voice_gateway/api/routes/realtime.py`

> This task does not add new pytest coverage — the integration is exercised by the live-DB test in Task 5. The change set is mechanical: store a `CallSession` in `request.app.state.call_sessions` keyed by call id; create on `/token`, append turns from the function-call proxy, and finalize on a new `/end` endpoint.

- [ ] **Step 1: Add call-session storage init**

In `voice_gateway/api/app.py` `lifespan`, after the DB init:
```python
    app.state.call_sessions: dict[str, "CallSession"] = {}
```

- [ ] **Step 2: Build `CallSession` at `/token`**

In `voice_gateway/api/routes/realtime.py`, modify `get_realtime_token` so that after `shop` is fetched and before returning the token JSON it does:

```python
    from voice_gateway.call_lifecycle import CallSession
    caller_number = request.headers.get("x-caller-number") or "+0000"
    twilio_sid = request.headers.get("x-twilio-call-sid")
    sess = CallSession(shop_id=shop_id, caller_number=caller_number, twilio_call_sid=twilio_sid)
    try:
        await sess.start()
    except Exception as e:  # DB optional in dev
        sess.id = None
        request.app.state.call_sessions  # noop; logged below
    request.app.state.call_sessions[str(sess.id)] = sess
```

Then include `call_id` in the response body so the client can refer to it:
```python
    payload["call_id"] = str(sess.id) if sess.id else None
```

- [ ] **Step 3: Append transcript turns from the function-call proxy**

In the existing function-call proxy handler (the route that the WebRTC client posts function calls to), look up the session by `call_id` (passed by the client) and call `await sess.log_event("function_call", {...})` before forwarding, and `await sess.log_event("function_result", {...})` after.

For booking actions, when the booking client returns an appointment, also:
```python
    sess.set_appointment(UUID(appt["id"]))
```

For `create_customer`:
```python
    await sess.attach_new_customer(UUID(new_cust["id"]))
```

- [ ] **Step 4: Add a transcript-turn endpoint**

The OpenAI Realtime client streams text deltas to the browser; on each completed turn the browser POSTs:

```python
class TurnIn(BaseModel):
    call_id: str
    role: str          # 'caller' | 'assistant' | 'system'
    text: str


@router.post("/transcript")
async def post_transcript(request: Request, body: TurnIn):
    sess = request.app.state.call_sessions.get(body.call_id)
    if not sess:
        return {"ok": False}
    from datetime import datetime, timezone
    await sess.append_turn(role=body.role, text=body.text,
                           at=datetime.now(timezone.utc))
    return {"ok": True}
```

- [ ] **Step 5: Add `/end` endpoint that finalizes**

```python
class EndIn(BaseModel):
    call_id: str


@router.post("/end")
async def end_call(request: Request, body: EndIn):
    from voice_gateway.clients.openai_classifier import classify_call
    settings_app = request.app.state
    sess = request.app.state.call_sessions.pop(body.call_id, None)
    if not sess:
        return {"ok": False}
    try:
        await sess.finalize(
            classifier=classify_call,
            api_key=settings_app._openai_key,
            model=getattr(settings_app, "_classifier_model", "gpt-4o-mini"),
        )
    except Exception as e:
        await sess.log_event("error", {"phase": "finalize", "detail": str(e)})
    return {"ok": True, "outcome": None}
```

And expose the classifier model in the lifespan:
```python
    app.state._classifier_model = settings.openai_classifier_model
```

- [ ] **Step 6: Update the static `index.html` test UI to call `/end` on hangup and `/transcript` per turn**

Open `voice_gateway/static/index.html`. In the WebRTC client code, when the user clicks "End Call" or the session terminates, send `POST /api/v1/realtime/end` with the `call_id` returned by `/token`. Also POST every text delta to `/api/v1/realtime/transcript`. (The exact JS edit is small and lives at the UI's session-end handler — minimal change.)

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ --ignore=tests/live_db -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add voice_gateway/api/app.py voice_gateway/api/routes/realtime.py voice_gateway/static/index.html
git commit -m "feat(gateway): wire CallSession into token, transcript, and end-call routes"
```

---

### Task 5: Read shops.voice and shops.language when building session config

**Files:**
- Modify: `voice_gateway/api/routes/realtime.py`
- Modify: `voice_gateway/clients/booking_client.py` (if shop fields are filtered)

- [ ] **Step 1: Confirm `voice` and `language` come back from the booking client**

Open `voice_gateway/clients/booking_client.py` and find `get_shop`. If it filters to a fixed list of fields, add `voice` and `language` to that list. If it returns the full shop record, no change needed.

- [ ] **Step 2: Use `voice` and `language` when building OpenAI session config**

In `realtime.py`, after fetching `shop`:

```python
    voice = shop.get("voice") or "alloy"
    language = shop.get("language") or "it"
```

Where the OpenAI Realtime token request body is built, set:
```python
        "voice": voice,
```
And in the instructions string, replace the hardcoded `"Rispondi SEMPRE in italiano"` line with:
```python
        f"Rispondi SEMPRE in {language}",
```

- [ ] **Step 3: Manual smoke test**

Save a config from the webapp (Plan 2, Task 9) setting `voice=echo` and `language=en`, then start a call. The OpenAI session should use the `echo` voice and the assistant should respond in English.

- [ ] **Step 4: Commit**

```bash
git add voice_gateway/api/routes/realtime.py voice_gateway/clients/booking_client.py
git commit -m "feat(gateway): read shops.voice and shops.language for session config"
```

---

### Task 6: Live-DB end-to-end persistence test

**Files:**
- Create: `tests/live_db/test_voice_gateway_persistence.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end: simulate a call lifecycle and assert persistence in voice_agent.*"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from voice_gateway.db import init_pool, close_pool, execute, execute_one, execute_void
from voice_gateway.call_lifecycle import CallSession


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)


@pytest.fixture(scope="module", autouse=True)
async def db():
    await init_pool(os.environ["DATABASE_URL"])
    yield
    await close_pool()


@pytest.fixture
async def shop():
    sid = uuid4()
    await execute_void(
        "INSERT INTO shops (id, name, is_active) VALUES ($1, 'TestShop', true)", sid,
    )
    yield sid
    await execute_void("DELETE FROM voice_agent.calls WHERE shop_id = $1", sid)
    await execute_void("DELETE FROM shops WHERE id = $1", sid)


async def test_full_call_lifecycle_persists(shop):
    classifier = AsyncMock(return_value={
        "outcome": "booked", "outcome_reason": "ok", "summary": "Maria prenotata",
    })

    sess = CallSession(shop_id=shop, caller_number="+390000000",
                       twilio_call_sid=f"CA{uuid4().hex[:8]}")
    await sess.start()
    assert sess.customer_match == "unmatched"

    now = datetime.now(timezone.utc)
    await sess.append_turn(role="assistant", text="Ciao", at=now)
    await sess.append_turn(role="caller", text="Vorrei prenotare", at=now)
    await sess.log_event("function_call", {"name": "book_appointment"})
    await sess.finalize(classifier=classifier, api_key="sk", model="gpt-4o-mini")

    row = await execute_one(
        "SELECT outcome, duration_seconds, summary "
        "FROM voice_agent.calls WHERE id = $1", sess.id,
    )
    assert row["outcome"] == "booked"
    assert row["duration_seconds"] is not None

    turns = await execute(
        "SELECT role, text FROM voice_agent.call_transcripts "
        "WHERE call_id = $1 ORDER BY turn_index", sess.id,
    )
    assert [t["role"] for t in turns] == ["assistant", "caller"]

    events = await execute(
        "SELECT type FROM voice_agent.call_events WHERE call_id = $1", sess.id,
    )
    assert any(e["type"] == "function_call" for e in events)
```

- [ ] **Step 2: Run live-DB test, verify pass**

`DATABASE_URL=... pytest tests/live_db/test_voice_gateway_persistence.py -v` → PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/live_db/test_voice_gateway_persistence.py
git commit -m "test(gateway): live-db end-to-end call lifecycle persistence"
```

---

### Task 7: Deploy and post-deploy verification

**Files:** none (deploy + manual checks)

- [ ] **Step 1: Set Fly secrets on the voice gateway**

```bash
fly secrets set \
  DATABASE_URL=postgresql://...pooler... \
  OPENAI_CLASSIFIER_MODEL=gpt-4o-mini
```
`OPENAI_KEY` should already be set from earlier deploys; verify with `fly secrets list`.

- [ ] **Step 2: Deploy**

```bash
./scripts/deploy-voice.sh
```

- [ ] **Step 3: Run a real call through the test UI**

Open the deployed gateway URL, start a call against a test shop, exchange a few turns, end the call. Then in the webapp's `/inbox` Conversations tab, refresh and confirm: the call appears, has a summary, an outcome, a transcript, and (if booked) a "Go to appointment" link.

- [ ] **Step 4: Test the linking flow**

For a call with `customer_match = unmatched`, click the badge, paste a customer UUID, confirm the row updates and the badge becomes `existing` after refresh.

- [ ] **Step 5: Final commit if any deploy script tweaks were needed**

```bash
git add scripts/
git commit -m "chore(deploy): pass DATABASE_URL and classifier model to voice gateway"
```
