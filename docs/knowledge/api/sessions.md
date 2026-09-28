# Sessions

Session-owned writes for the **customer agents** — the marketing-engine's common
layer (`src/lib/customer-agents/`), which runs the WhatsApp booking agent now and
the voice agent after Phase C. The agents' booking and customer writes go to the
webapp's `/api/v1/hair-salon/agent/*` routes; what stays here is the one row this
repo owns, `voice_agent.calls` — the session.

Route file: `booking_engine/api/routes/sessions.py`. Mounted at the **root**
(no `/api/v1`), like `/voice/tools/*`: the caller reaches it through
`VOICE_AGENT_TOOLS_URL`, which by contract carries no prefix.

> **Maintenance rule:** an endpoint added/removed/changed updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

## Auth and scoping (all three routes)

- `Authorization: Bearer <VOICE_AGENT_TOOL_SECRET>` (`require_tool_token`) — 401 otherwise.
- `X-Shop-Id: <shop uuid>` — required. The `voice_agent.calls` row `{call_id}` must
  belong to that shop, or the answer is **404 `{"ok": false, "data": null, "error": "unknown_session"}`**.
  A session of another shop and a session that does not exist get the same answer.

Every response is the tool `Envelope`: `{"ok": bool, "data": ... | null, "error": str | null}`.
A 404 always carries `error` in the body — that is how a caller tells a refusal
from a missing route.

## Routes

| Route | Body | Effect | `data` on success |
|---|---|---|---|
| `POST /sessions/{call_id}/customer` | `{customer_id}` | sets `calls.customer_id`, `customer_match='existing'`. 404 `unknown_customer` if the customer does not exist or is another shop's | `{"linked": true}` |
| `POST /sessions/{call_id}/escalation` | `{reason, customer_message, callback_window?}` | inserts a `callback_memos` row (reason = `"{reason} — {customer_message}"`), sets `outcome='escalated'` (the session's escalated flag — it also silences the WhatsApp agent on the thread), pushes `voice_new_memo` | `{"memo_id": "<uuid>"}` |
| `POST /sessions/{call_id}/outcome` | `{outcome, summary?}` | sets `calls.outcome` and `summary` | `{"marked": true}` |

`outcome` is one of `booked | rescheduled | cancelled | info | info_only | abandoned | escalated | failed`
— the `calls.outcome` CHECK values plus `info_only`, the agents' name for
`info`, stored as `info`. Anything else is a FastAPI 422.

The escalation and outcome bodies are the `/voice/tools/escalate_to_merchant` and
`/voice/tools/mark_outcome` handlers ([Voice Tools](voice-tools.md)), moved
unchanged; those two stay until the voice agent moves onto the common layer
(Phase C).
