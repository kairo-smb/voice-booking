# SIP Call Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-call server-side Realtime control WebSocket that makes the production SIP agent greet first and voice MCP tool results (instead of going mute), with structured per-call event logging.

**Architecture:** After `/voice/openai/incoming` accepts a SIP call, it spawns an in-process `asyncio` task (`supervise`) that opens `wss://api.openai.com/v1/realtime?call_id={call_id}`, sends an opening `response.create` (greeting), and on each server event runs a pure decider (`decide`) that sends `response.create` after each MCP tool completes when the session is idle. A pure `log_record` builds one structured stdout line per event. Gated behind `ENABLE_CALL_SUPERVISOR` (default off).

**Tech Stack:** Python 3.12, asyncio, `websockets` (already installed via `uvicorn[standard]`), FastAPI, pydantic-settings, pytest (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-07-21-sip-call-supervisor-design.md`

---

## File Structure

- **Create** `booking_engine/services/call_supervisor.py` — `SupervisorState`, `decide()`, `log_record()`, `supervise()`, `maybe_supervise()`, `_default_connect()`. One responsibility: drive one call's control channel.
- **Create** `tests/voice_gateway/test_call_supervisor.py` — unit tests for `decide`, `log_record`, `supervise` (fake WS), `maybe_supervise`.
- **Modify** `booking_engine/config.py` — add `enable_call_supervisor` field.
- **Modify** `booking_engine/api/routes/voice_openai.py` — one call to `maybe_supervise` after a successful accept.
- **Modify** `tests/booking_engine/test_config.py` — assert the new flag default.
- **Modify** `requirements.txt` — declare `websockets` as a first-class dep.

---

## Task 1: Config flag `enable_call_supervisor`

**Files:**
- Modify: `booking_engine/config.py`
- Test: `tests/booking_engine/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/booking_engine/test_config.py`:

```python
def test_enable_call_supervisor_defaults_false(monkeypatch):
    monkeypatch.delenv("ENABLE_CALL_SUPERVISOR", raising=False)
    from booking_engine.config import Settings
    assert Settings().enable_call_supervisor is False


def test_enable_call_supervisor_reads_env(monkeypatch):
    monkeypatch.setenv("ENABLE_CALL_SUPERVISOR", "true")
    from booking_engine.config import Settings
    assert Settings().enable_call_supervisor is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/booking_engine/test_config.py::test_enable_call_supervisor_defaults_false -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'enable_call_supervisor'`

- [ ] **Step 3: Write minimal implementation**

In `booking_engine/config.py`, add this field inside `Settings` (after `voice_cancellation_lead_time_hours`, before `model_config`):

```python
    # Spawn a per-call server-side Realtime control WebSocket (greeting + voice
    # tool results). Off by default; enable per environment for live SIP calls.
    enable_call_supervisor: bool = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/booking_engine/test_config.py -k enable_call_supervisor -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add booking_engine/config.py tests/booking_engine/test_config.py
git commit -m "feat(voice): add ENABLE_CALL_SUPERVISOR config flag"
```

---

## Task 2: `SupervisorState` + `decide()` core

**Files:**
- Create: `booking_engine/services/call_supervisor.py`
- Test: `tests/voice_gateway/test_call_supervisor.py`

The decider is pure and synchronous: it tracks whether a response is generating and emits `response.create` after an MCP tool completes when idle. `nudge_pending` prevents a second tool-completion event (parallel MCP calls or a duplicate) from firing a second `response.create` before the nudge's own `response.created` arrives.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_gateway/test_call_supervisor.py`:

```python
from booking_engine.services.call_supervisor import SupervisorState, decide

_TOOL_DONE = {
    "type": "response.output_item.done",
    "item": {"type": "mcp_call", "name": "get_services", "id": "mcp_1", "output": "{}"},
}


def test_decide_nudges_after_tool_when_idle():
    state = SupervisorState(response_active=False)
    out = decide(_TOOL_DONE, state)
    assert out == [{"type": "response.create"}]
    assert state.nudge_pending is True


def test_decide_suppresses_nudge_while_response_active():
    state = SupervisorState(response_active=True)
    assert decide(_TOOL_DONE, state) == []


def test_decide_dedupes_two_tool_completions_into_one_nudge():
    state = SupervisorState(response_active=False)
    first = decide(_TOOL_DONE, state)
    second = decide(_TOOL_DONE, state)  # before any response.created
    assert first == [{"type": "response.create"}]
    assert second == []


def test_decide_response_created_clears_pending_and_marks_active():
    state = SupervisorState(response_active=False, nudge_pending=True)
    assert decide({"type": "response.created"}, state) == []
    assert state.response_active is True
    assert state.nudge_pending is False


def test_decide_response_done_marks_idle():
    state = SupervisorState(response_active=True)
    assert decide({"type": "response.done"}, state) == []
    assert state.response_active is False


def test_decide_ignores_non_mcp_output_item_done():
    state = SupervisorState(response_active=False)
    ev = {"type": "response.output_item.done", "item": {"type": "message"}}
    assert decide(ev, state) == []


def test_decide_ignores_mcp_call_completed_event():
    # Only output_item.done triggers; mcp_call.completed is log-only.
    state = SupervisorState(response_active=False)
    assert decide({"type": "response.mcp_call.completed"}, state) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'booking_engine.services.call_supervisor'`

- [ ] **Step 3: Write minimal implementation**

Create `booking_engine/services/call_supervisor.py`:

```python
"""Per-call server-side Realtime control WebSocket for SIP calls.

The SIP accept path is fire-and-forget: OpenAI drives the call and, with hosted
MCP tools, does NOT auto-speak tool results or greet first. This worker opens a
control WS to the accepted call (wss://api.openai.com/v1/realtime?call_id=...)
and sends `response.create` to greet on connect and again after each MCP tool
completes, so the agent voices the result. See
docs/superpowers/specs/2026-07-21-sip-call-supervisor-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SupervisorState:
    response_active: bool = False
    nudge_pending: bool = False
    greeted: bool = False
    tool_started_at: dict[str, float] = field(default_factory=dict)


def decide(event: dict, state: SupervisorState) -> list[dict]:
    """Pure decision core: mutate response/nudge state, return client events to send."""
    etype = event.get("type")
    if etype == "response.created":
        state.response_active = True
        state.nudge_pending = False
        return []
    if etype == "response.done":
        state.response_active = False
        return []
    if etype == "response.output_item.done" and (event.get("item") or {}).get("type") == "mcp_call":
        if not state.response_active and not state.nudge_pending:
            state.nudge_pending = True
            return [{"type": "response.create"}]
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/call_supervisor.py tests/voice_gateway/test_call_supervisor.py
git commit -m "feat(voice): call supervisor decide() core (greet/nudge/dedup)"
```

---

## Task 3: `log_record()` — structured per-event log line

**Files:**
- Modify: `booking_engine/services/call_supervisor.py`
- Test: `tests/voice_gateway/test_call_supervisor.py`

`log_record` builds the dict that gets `json.dumps`'d to stdout. It records a start timestamp when an `mcp_call` item is added and computes `latency_ms` when it completes.

- [ ] **Step 1: Write the failing test**

Append to `tests/voice_gateway/test_call_supervisor.py`:

```python
from booking_engine.services.call_supervisor import log_record


def test_log_record_marks_tool_start():
    state = SupervisorState()
    ev = {"type": "response.output_item.added",
          "item": {"type": "mcp_call", "id": "mcp_1", "name": "get_services"}}
    rec = log_record("call_A", ev, state)
    assert rec["call_id"] == "call_A"
    assert rec["event"] == "response.output_item.added"
    assert "mcp_1" in state.tool_started_at


def test_log_record_computes_latency_on_done():
    state = SupervisorState()
    added = {"type": "response.output_item.added",
             "item": {"type": "mcp_call", "id": "mcp_1", "name": "get_services"}}
    done = {"type": "response.output_item.done",
            "item": {"type": "mcp_call", "id": "mcp_1", "name": "get_services", "output": "{}"}}
    log_record("call_A", added, state)
    rec = log_record("call_A", done, state)
    assert rec["tool"] == "get_services"
    assert isinstance(rec["latency_ms"], int) and rec["latency_ms"] >= 0
    assert "mcp_1" not in state.tool_started_at  # popped


def test_log_record_plain_event():
    rec = log_record("call_A", {"type": "response.created"}, SupervisorState())
    assert rec == {"call_id": "call_A", "event": "response.created"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -k log_record -v`
Expected: FAIL with `ImportError: cannot import name 'log_record'`

- [ ] **Step 3: Write minimal implementation**

Add to `booking_engine/services/call_supervisor.py` (add `import time` at the top with the other imports):

```python
import time


def log_record(call_id: str, event: dict, state: SupervisorState) -> dict:
    """Build one structured log line; track/compute MCP tool latency as a side effect."""
    etype = event.get("type")
    item = event.get("item") or {}
    rec: dict = {"call_id": call_id, "event": etype}
    if etype == "response.output_item.added" and item.get("type") == "mcp_call":
        if item.get("id"):
            state.tool_started_at[item["id"]] = time.monotonic()
    elif etype == "response.output_item.done" and item.get("type") == "mcp_call":
        rec["tool"] = item.get("name")
        started = state.tool_started_at.pop(item.get("id"), None)
        if started is not None:
            rec["latency_ms"] = round((time.monotonic() - started) * 1000)
    return rec
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -k log_record -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/call_supervisor.py tests/voice_gateway/test_call_supervisor.py
git commit -m "feat(voice): call supervisor structured per-event logging"
```

---

## Task 4: `supervise()` + `_default_connect()` + declare `websockets`

**Files:**
- Modify: `booking_engine/services/call_supervisor.py`
- Modify: `requirements.txt`
- Test: `tests/voice_gateway/test_call_supervisor.py`

`supervise` is the async glue: connect, greet once, then loop `recv → log → decide → send`. A clean WS close means the call ended (return). An exception means an unexpected drop — retry once, then give up. The WS connect is injectable (`connect=`) so tests use a fake WS with no network.

- [ ] **Step 1: Write the failing test**

Append to `tests/voice_gateway/test_call_supervisor.py`:

```python
import json
import pytest
from booking_engine.services.call_supervisor import supervise


class _FakeWS:
    """Fake control WS: yields scripted server events, records sent client events."""
    def __init__(self, events):
        self._events = [json.dumps(e) for e in events]
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for raw in self._events:
            yield raw


def _connect_returning(ws):
    def _connect(call_id, api_key):
        return ws
    return _connect


async def test_supervise_greets_then_nudges_after_tool():
    ws = _FakeWS([
        {"type": "response.created"},
        {"type": "response.done"},
        {"type": "response.output_item.added",
         "item": {"type": "mcp_call", "id": "mcp_1", "name": "get_services"}},
        {"type": "response.output_item.done",
         "item": {"type": "mcp_call", "id": "mcp_1", "name": "get_services", "output": "{}"}},
    ])
    await supervise("call_A", "key", connect=_connect_returning(ws))
    # First sent event is the greeting; last is the post-tool nudge.
    assert ws.sent[0] == {"type": "response.create"}
    assert ws.sent[-1] == {"type": "response.create"}
    assert ws.sent.count({"type": "response.create"}) == 2


async def test_supervise_dedupes_parallel_tool_completions():
    ws = _FakeWS([
        {"type": "response.done"},
        {"type": "response.output_item.done",
         "item": {"type": "mcp_call", "id": "mcp_1", "name": "get_services", "output": "{}"}},
        {"type": "response.output_item.done",
         "item": {"type": "mcp_call", "id": "mcp_2", "name": "check_availability", "output": "{}"}},
    ])
    await supervise("call_A", "key", connect=_connect_returning(ws))
    # greeting + exactly one nudge (second completion suppressed by nudge_pending)
    assert ws.sent.count({"type": "response.create"}) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -k supervise -v`
Expected: FAIL with `ImportError: cannot import name 'supervise'`

- [ ] **Step 3: Write minimal implementation**

Add to `booking_engine/services/call_supervisor.py` (add `import json`, `import logging` at the top; add `logger = logging.getLogger(__name__)` after imports):

```python
import json
import logging

logger = logging.getLogger(__name__)

_WS_URL = "wss://api.openai.com/v1/realtime?call_id={call_id}"


def _default_connect(call_id: str, api_key: str):
    import websockets  # local import: only needed when a real call runs
    return websockets.connect(
        _WS_URL.format(call_id=call_id),
        additional_headers={"Authorization": f"Bearer {api_key}"},
    )


async def supervise(call_id: str, api_key: str, *, connect=_default_connect) -> None:
    """Own one call's control WS: greet on connect, voice tool results, log events.

    Best-effort and isolated: call audio flows OpenAI<->Twilio independently of
    this WS, so any failure here degrades only this call (no greeting/nudge), it
    never drops the call. Clean close = call ended. One reconnect on drop.
    """
    state = SupervisorState()
    for attempt in (1, 2):
        try:
            async with connect(call_id, api_key) as ws:
                if not state.greeted:
                    await ws.send(json.dumps({"type": "response.create"}))
                    state.greeted = True
                    logger.info(json.dumps({"call_id": call_id, "event": "supervisor.greeted"}))
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    logger.info(json.dumps(log_record(call_id, event, state)))
                    for client_ev in decide(event, state):
                        await ws.send(json.dumps(client_ev))
            return  # clean close: the call ended
        except Exception:
            logger.exception("call_supervisor error call_id=%s attempt=%s", call_id, attempt)
    logger.warning(json.dumps({"call_id": call_id, "event": "supervisor.gave_up"}))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -v`
Expected: all tests pass (12 total)

- [ ] **Step 5: Declare the dependency**

In `requirements.txt`, under the `# HTTP client` section (after the `httpx` line), add:

```
# WebSocket client (server-side Realtime call control); already present via uvicorn[standard]
websockets>=15.0
```

- [ ] **Step 6: Commit**

```bash
git add booking_engine/services/call_supervisor.py tests/voice_gateway/test_call_supervisor.py requirements.txt
git commit -m "feat(voice): call supervisor WS worker (greet + voice tool results)"
```

---

## Task 5: `maybe_supervise()` + wire into the incoming webhook

**Files:**
- Modify: `booking_engine/services/call_supervisor.py`
- Modify: `booking_engine/api/routes/voice_openai.py`
- Test: `tests/voice_gateway/test_call_supervisor.py`

`maybe_supervise` centralises the flag check + task spawn so the webhook change is a single line and the spawn logic is testable without the DB-heavy handler.

- [ ] **Step 1: Write the failing test**

Append to `tests/voice_gateway/test_call_supervisor.py`:

```python
import asyncio
from booking_engine.services import call_supervisor as cs


class _Settings:
    def __init__(self, enabled):
        self.enable_call_supervisor = enabled
        self.openai_api_key = "key"


def test_maybe_supervise_spawns_when_enabled(monkeypatch):
    spawned = []

    async def _fake_supervise(call_id, api_key, **kw):
        return None

    def _fake_create_task(coro):
        spawned.append(coro)
        coro.close()  # avoid "coroutine never awaited" warning
        return None

    monkeypatch.setattr(cs, "supervise", _fake_supervise)
    monkeypatch.setattr(cs.asyncio, "create_task", _fake_create_task)
    cs.maybe_supervise("call_A", _Settings(enabled=True))
    assert len(spawned) == 1


def test_maybe_supervise_skips_when_disabled(monkeypatch):
    spawned = []
    monkeypatch.setattr(cs.asyncio, "create_task", lambda coro: spawned.append(coro))
    cs.maybe_supervise("call_A", _Settings(enabled=False))
    assert spawned == []


def test_maybe_supervise_skips_without_call_id(monkeypatch):
    spawned = []
    monkeypatch.setattr(cs.asyncio, "create_task", lambda coro: spawned.append(coro))
    cs.maybe_supervise("", _Settings(enabled=True))
    assert spawned == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -k maybe_supervise -v`
Expected: FAIL with `AttributeError: module 'booking_engine.services.call_supervisor' has no attribute 'maybe_supervise'`

- [ ] **Step 3: Write minimal implementation**

Add to `booking_engine/services/call_supervisor.py` (add `import asyncio` at the top):

```python
import asyncio


def maybe_supervise(call_id: str, settings) -> None:
    """Spawn the supervisor task when enabled and we have a call id. No-op otherwise."""
    if call_id and getattr(settings, "enable_call_supervisor", False):
        asyncio.create_task(supervise(call_id, settings.openai_api_key))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -k maybe_supervise -v`
Expected: 3 passed

- [ ] **Step 5: Wire into the webhook**

In `booking_engine/api/routes/voice_openai.py`:

Add the import (with the other `booking_engine` imports near the top):

```python
from booking_engine.services.call_supervisor import maybe_supervise
```

Replace the final accept block (currently):

```python
    ok = await accept_sip_call(
        call_id=call_id, payload=payload, api_key=settings.openai_api_key,
    )
    return {"status": "accepted" if ok else "accept_failed"}
```

with:

```python
    ok = await accept_sip_call(
        call_id=call_id, payload=payload, api_key=settings.openai_api_key,
    )
    if ok:
        # Own the call's control channel: greet + voice tool results. Gated by
        # ENABLE_CALL_SUPERVISOR; no-op when off. Fire-and-forget by design.
        maybe_supervise(call_id, settings)
    return {"status": "accepted" if ok else "accept_failed"}
```

- [ ] **Step 6: Run the full supervisor + webhook route tests**

Run: `pytest tests/voice_gateway/test_call_supervisor.py -v`
Expected: all pass (15 total)

Run: `pytest tests/ -q`
Expected: no new failures introduced (pre-existing `tests/live_db/*` failures that require a seeded DB / `TEST_DATABASE_URL` are unrelated to this change).

- [ ] **Step 7: Commit**

```bash
git add booking_engine/services/call_supervisor.py booking_engine/api/routes/voice_openai.py tests/voice_gateway/test_call_supervisor.py
git commit -m "feat(voice): spawn call supervisor after SIP accept (flag-gated)"
```

---

## Manual verification (pre-launch, not automatable)

These require live telephony + OpenAI and are out of scope for CI:

1. Set `ENABLE_CALL_SUPERVISOR=true` on the QA Fly app (`fly secrets set ENABLE_CALL_SUPERVISOR=true -a kairo-booking-engine-qa`).
2. Place a real inbound SIP call to the QA number.
3. In `fly logs -a kairo-booking-engine-qa`, confirm: a `supervisor.greeted` line, the agent greets first, and after a tool the JSON log shows the tool event + `latency_ms`, and the agent voices the result (no mute).
4. Only after this passes, enable in production.

---

## Notes for the implementer

- `asyncio_mode = auto` (see `pytest.ini`) — async test functions need no decorator.
- `websockets` 15.x uses `additional_headers=` (not the older `extra_headers=`). Do not change this.
- Do NOT retain the `asyncio.create_task` return value in `maybe_supervise` — the task manages its own lifetime and logs its own exit; this is intentional fire-and-forget.
- `Settings` uses `env_prefix: ""`, so the field `enable_call_supervisor` reads the `ENABLE_CALL_SUPERVISOR` env var automatically.
