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
