# Voice Tools

**The voice agent's tools no longer live in this repo.** Since 2026-09-28 they are marketing-engine's **customer agents** (`kairo-market-intel/src/lib/customer-agents/`), the same layer the WhatsApp agent runs on: one set of schemas, one per-session dispatch through `runTool`, one allow-list per surface. OpenAI Realtime reaches them over MCP at `{MARKET_INTEL_API_URL}/customer-agents/voice/mcp` (`src/routes/customer-agents-voice.ts`), and the rules the model follows come from `/customer-agents/voice/instructions`. See `AGENTS.md` §2026-09-28 for the why.

> **Maintenance rule:** a change to how the accept path points a call at those tools (`services/realtime_session.py`, `clients/customer_agents_voice.py`) updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

## What this repo still does for a call

- **Points the session at the tools.** `realtime_session.py::build_accept_payload` sets one `mcp` tool: `server_url` from `MARKET_INTEL_API_URL`, `authorization` = the per-call token (`services/call_token.py`, HMAC-signed `{shop_id, call_id}` with `VOICE_AGENT_TOOL_SECRET`, which marketing-engine verifies with the same value), `allowed_tools` = `CUSTOMER_AGENT_TOOLS` (the 11 names below). A stale name in that list silently filters a tool out of every call.
- **Owns the session row** the token names (`voice_agent.calls`) and its writes: marketing-engine calls back into [Sessions](sessions.md) for `escalate_to_owner`, `set_conversation_outcome` and the customer link.
- **Owns the persona** (voice, tone, greeting) — see [Voice Agent Logic](../voice-agent-logic.md#prompt-assembly).

## The tools (marketing-engine)

| Old `/voice/tools/*` name (deleted) | Customer-agent name | Backed by |
|---|---|---|
| `lookup_customer` | `customers_identify` | marketing-engine SQL, by the session's caller number |
| `create_customer_from_call` | `create_customer` | webapp `POST /api/v1/hair-salon/agent/customers` |
| `update_customer_from_call` | `update_customer` | webapp `PATCH /api/v1/hair-salon/agent/customers/{id}` |
| `get_services` + `get_staff_for_service` | `services_catalog` | marketing-engine SQL |
| `check_availability` | `availability_search` | webapp `/availability` (the only slot engine) |
| `create_booking` | `create_appointment` | webapp `POST /api/v1/hair-salon/agent/appointments` |
| `get_booking` | `appointments_upcoming` | marketing-engine SQL, by the session's caller number |
| `modify_booking` | `reschedule_appointment` | webapp `POST …/agent/appointments/{id}/reschedule` |
| `cancel_booking` | `cancel_appointment` | webapp `POST …/agent/appointments/{id}/cancel` |
| `escalate_to_merchant` | `escalate_to_owner` | this repo, `POST /sessions/{call_id}/escalation` |
| `mark_outcome` | `set_conversation_outcome` | this repo, `POST /sessions/{call_id}/outcome` |

## Session lifecycle webhooks (still here)

Auth `require_tool_token`.

| Endpoint | File | Purpose |
|---|---|---|
| `POST /voice/events/session.started` | `voice_events.py` | inserts the call row, returns the persona prompt + voice (no tools — they are marketing-engine's) |
| `POST /voice/events/session.turn` | `voice_events.py` | persists a transcript turn |
| `POST /voice/events/session.ended` | `voice_events.py` | finalizes the call row, charges the call |
