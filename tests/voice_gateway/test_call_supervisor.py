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
