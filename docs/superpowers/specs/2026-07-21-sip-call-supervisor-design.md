# SIP call supervisor — server-side Realtime control WebSocket

**Date:** 2026-07-21
**Status:** Design approved, pending spec review
**Related:** CLAUDE.md 2026-07-21 "Realtime + hosted MCP does NOT auto-speak tool
results (prod blocker)"; `scripts/voice_test_static/index.html` (harness that
already implements the client-side equivalent).

## Problem

In the OpenAI Realtime API with a hosted `mcp` tool, the model's response ends
(`response.done`) as soon as it emits a tool call — before the tool returns. The
tool runs server-side, `response.mcp_call.completed` / `response.output_item.done`
deliver the result, and then OpenAI does **not** open a new response to voice it.
The agent goes silent after every fetch. (Verified from a live harness event
trace: `response.done` arrives at output_index 0, then an orphaned
`mcp_call.completed` at index 1 with no successor response.)

The browser harness fixes this by sending `{"type": "response.create"}` after the
tool result over its data channel. The **production SIP path cannot**: the
`/voice/openai/incoming` webhook accepts the call
(`POST /v1/realtime/calls/{id}/accept`) and holds **no** connection to the
session, so nothing can send `response.create`. Two production gaps result:

1. **No greeting** — nothing triggers the opening `response.create`, so the agent
   waits for the caller to speak instead of greeting first.
2. **Mute after every tool** — no nudge, so the agent goes silent after each MCP
   fetch.

Not urgent only because Twilio is unfunded (no live inbound calls yet). Must be
solved before launch.

## Mechanism (confirmed)

After accepting a SIP call you may open a control WebSocket to the same session:

```
wss://api.openai.com/v1/realtime?call_id={call_id}
Authorization: Bearer {OPENAI_API_KEY}
```

It behaves like any Realtime connection: receive server events, send client
events (`response.create`, etc.). The `model` argument is not used — the session
was already configured by `/accept`. (Refs: OpenAI Realtime SIP guide,
server-side controls guide, Calls API reference.)

## Goals (v1)

- **Greet first:** send `response.create` on connect so the agent opens the call.
- **Nudge after tools:** send `response.create` after each MCP tool completes,
  when no response is active, so the agent voices the result (or chains the next
  tool).
- **Per-call event logging:** one structured JSON line per event to stdout
  (captured by `fly logs`) for production debugging.

## Non-goals (v1)

Barge-in / turn handling; post-call outcome capture from the event stream; DB
persistence of events; reconnect backoff beyond a single retry; multi-region
call affinity (not needed — see Concurrency).

## Approach (chosen: A — per-call in-process asyncio worker)

After `accept_sip_call` returns 200, the webhook spawns an `asyncio` task that
owns that call's control WebSocket for its lifetime. Runs inside the existing
Fly app process. The webhook still returns its 200 immediately (required before
OpenAI connects the call); the task runs in the background.

Rejected alternatives:
- **B) Separate control microservice** — new deployable + `call_id` handoff;
  over-engineered for current volume.
- **C) Prompt/config workaround** — disproven; Realtime does not auto-continue
  and no config flag is known to exist.

## Components

### `booking_engine/services/call_supervisor.py` (new)

Single purpose: drive one call's control channel to completion.

- `async def supervise(call_id: str, api_key: str) -> None`
  Opens the WS, sends the greeting `response.create`, then loops:
  `recv event → log_event(event) → for ev in decide(event, state): send(ev)`.
  Exits when the WS closes (call ended). The entire body is wrapped so any
  failure degrades only this one call and never touches call audio (media is
  OpenAI↔Twilio, independent of this control WS).

- `def decide(event: dict, state: SupervisorState) -> list[dict]`
  The pure, synchronous, unit-testable core. Mirrors the harness JS:
  - `response.created` → `response_active = True`, `nudge_pending = False`;
    return `[]`.
  - `response.done` → `response_active = False`; return `[]`.
  - **tool completion — exactly one trigger event:** `response.output_item.done`
    with `item.type == "mcp_call"`. If `not response_active and not
    nudge_pending`, set `nudge_pending = True` and return
    `[{"type": "response.create"}]`; else `[]`.
  - anything else (including `response.mcp_call.completed`) → `[]` (log-only).
  Because a single WS delivers events **in order**, `response.done` always
  precedes the tool-completion event (as in the captured trace), so no
  timer/debounce is required — simpler than the browser version.

  **Why the `nudge_pending` guard (not just `response_active`):** the nudge's own
  `response.create` hasn't produced `response.created` yet at the moment we emit
  it, so a second tool-completion event in the same batch (parallel MCP calls, or
  a stray duplicate) would still see `response_active == False` and fire a second
  `response.create` → "conversation already has an active response" error /
  double turn. `nudge_pending` blocks that until the next `response.created`
  clears it. Triggering on a single event type (`output_item.done`, not also
  `mcp_call.completed`) removes the other duplicate source.

- `SupervisorState` — a small dataclass: `response_active: bool`,
  `nudge_pending: bool`, `greeted: bool`, and `tool_started_at: dict[item_id,
  float]` for latency logging. Local to the task; no shared/global state.
  `greeted` guards the opening `response.create` so a reconnect does not re-greet
  mid-call.

- `def log_event(call_id, event, state) -> None`
  Emits `logger.info(json.dumps({...}))` with `call_id`, event `type`, tool
  `name` (when present), and computed latency on tool completion.

### `booking_engine/api/routes/voice_openai.py` (modified)

After the existing accept:

```python
ok = await accept_sip_call(call_id=call_id, payload=payload, api_key=settings.openai_api_key)
if ok and settings.enable_call_supervisor:
    asyncio.create_task(supervise(call_id, settings.openai_api_key))
return {"status": "accepted" if ok else "accept_failed"}
```

The task reference is intentionally fire-and-forget; it self-terminates on WS
close. (We accept the standard "create_task reference not retained" lint note —
the task manages its own lifetime and logs its own exit.)

### `booking_engine/config.py` (modified)

Add `enable_call_supervisor: bool = False` — an env flag
(`ENABLE_CALL_SUPERVISOR`) so the worker is enabled deliberately. Prod path is
unchanged until flipped. Harmless now (no live calls).

### `requirements.txt` (modified)

Declare `websockets>=15.0` explicitly. Already installed transitively via
`uvicorn[standard]`; we now use it directly, so it should be a first-class dep.

## Data flow

```
realtime.call.incoming (webhook)
  → assemble session, accept_sip_call → 200
  → return 200 to OpenAI (required)  ── and ──  asyncio.create_task(supervise)
                                                   │
supervise():                                       ▼
  connect wss?call_id=…  →  send response.create (greet)
  loop: recv event → log_event → decide → send(client events)
        (tool completes → decide returns response.create → agent voices result)
  WS closes (call ends) → task exits
```

## Error handling / robustness

- **Isolation:** call audio does not flow through this WS; a worker crash
  degrades that call to the current (greeting-less, mute-after-tool) behavior but
  never drops the call.
- **Connect failure:** log an error and give up (call proceeds without
  greeting/nudge).
- **Unexpected mid-call drop:** one reconnect attempt, then give up.
- **Per-event exceptions:** caught and logged; the loop continues.
- **No double-response:** `decide` only emits `response.create` when
  `response_active` is false; ordering is deterministic on a single WS.

## Concurrency / scale

The WS is outbound and keyed by `call_id` on whichever Fly machine handled the
webhook, so multiple Fly machines need no shared state or session affinity. One
lightweight asyncio task per concurrent call; volume is currently zero and
expected low.

## Testing

- **Unit (the runnable check):** drive `decide()` with recorded event fixtures —
  (1) greeting fires once on connect, (2) nudge fires after a tool-completion
  event when idle, (3) nudge suppressed while a response is active, (4) two
  tool-completion events before the next `response.created` yield only **one**
  `response.create` (`nudge_pending` dedup), (5) latency computed on tool
  completion. No live OpenAI.
- **Integration:** a real inbound SIP call is a manual pre-launch check (cannot
  be automated without live telephony + OpenAI).

## Rollout

1. Ship behind `ENABLE_CALL_SUPERVISOR=false`.
2. Enable on QA, place a manual SIP test call, confirm greeting + post-tool
   speech in `fly logs`.
3. Enable in production when live calls go live.
