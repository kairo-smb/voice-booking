# Voice Tools

The 12 tools OpenAI calls during a live session, over MCP (`/mcp`, dispatched in-process — see [Architecture](../architecture.md#call-flow)) or directly via their own `/voice/tools/*` routes. All require `Authorization: Bearer <OPENAI_TOOL_SECRET>` (`require_tool_token`). Tool semantics/rules: [Voice Agent Logic](../voice-agent-logic.md).

> **Maintenance rule:** a tool added/removed/changed (in `safety_layer.py` or its route file) updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Tool | Route | File |
|---|---|---|
| `get_services` | `POST /voice/tools/get_services` | `voice_tools_catalog.py` |
| `get_staff_for_service` | `POST /voice/tools/get_staff_for_service` | `voice_tools_catalog.py` |
| `check_availability` | `POST /voice/tools/check_availability` | `voice_tools_booking.py` |
| `create_booking` | `POST /voice/tools/create_booking` | `voice_tools_booking.py` |
| `get_booking` | `POST /voice/tools/get_booking` | `voice_tools_booking.py` |
| `modify_booking` | `POST /voice/tools/modify_booking` | `voice_tools_booking.py` |
| `cancel_booking` | `POST /voice/tools/cancel_booking` | `voice_tools_booking.py` |
| `lookup_customer` | `POST /voice/tools/lookup_customer` | `voice_tools_identity.py` |
| `create_customer_from_call` | `POST /voice/tools/create_customer_from_call` | `voice_tools_identity.py` |
| `update_customer_from_call` | `POST /voice/tools/update_customer_from_call` | `voice_tools_identity.py` |
| `mark_outcome` | `POST /voice/tools/mark_outcome` | `voice_tools_lifecycle.py` |
| `escalate_to_merchant` | `POST /voice/tools/escalate_to_merchant` | `voice_tools_lifecycle.py` |

Session lifecycle webhooks (same auth, same "in-process, not agent-facing tools" category):

| Endpoint | File | Purpose |
|---|---|---|
| `POST /voice/events/session.started` | `voice_events.py` | assembles and returns the session prompt + tools (see [Voice Agent Logic](../voice-agent-logic.md#prompt-assembly)) |
| `POST /voice/events/session.turn` | `voice_events.py` | persists a transcript turn |
| `POST /voice/events/session.ended` | `voice_events.py` | finalizes the call row |

Outcome enum (`mark_outcome`): `booked \| rescheduled \| cancelled \| info \| abandoned \| escalated \| failed`.

Exact request/response JSON schemas: `_TOOL_SCHEMAS` in `booking_engine/services/safety_layer.py` (what OpenAI sees) and `booking_engine/api/voice_tool_models.py` (the Pydantic request/response models each route actually validates against).
