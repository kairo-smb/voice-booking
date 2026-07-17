# Live Tool-Dispatch + Security Tests — Design

## Why

Exactly one agent implementation is actually deployed: booking_engine's SIP/MCP
path (`voice_openai.py` → `mcp_server.py` → `mcp_tools.execute_tool()` →
`/voice/tools/*` route handlers → `safety_layer.py` authz/constraints →
`queries.py`). `fly.toml` and `fly.qa.toml` both build only
`booking_engine/Dockerfile.fly`; `voice_gateway/` (5 inline tools, no authz,
its own unused `Dockerfile`) is dead code left over from the 2026-07-16
architecture decision recorded in project memory
(`project_architecture_divergence.md`) and is out of scope here — flagged as a
separate follow-up, not actioned in this work, since deleting it also means
deleting ~6 dependent test files and touches CI workflow files another agent
is currently modifying under the ephemeral-branch-CI spec.

Today's test coverage has a gap at exactly the layer that matters most for
"can we trust this in production": the 12 tools' HTTP dispatch path
(`execute_tool()` → route handler → `safety_layer` authz → `queries.py`) has
**zero live-database coverage**.

- `tests/voice_gateway/test_voice_tools_*.py` exercise the route handlers,
  but every DB call is mocked (`AsyncMock`/`patch`) — authz and constraint
  logic (cross-shop rejection, phone-mismatch rejection, lead-time checks)
  has never been proven against a real row or the real schema.
- `tests/live_db/*.py` exercise the query layer (`queries.py`,
  `voice_queries.py`) directly against real Postgres — but below the authz
  layer, not through `execute_tool()`/the route handlers OpenAI actually
  calls.
- `tests/voice_gateway/test_call_token.py` and `test_mcp_tools.py` unit-test
  token verification and dispatch routing, fully mocked — the real
  `verify_call_token` has never been exercised end-to-end through
  `execute_tool()` against a live app.

No test today would catch a regression where, say, `modify_booking` stops
checking `phone_mismatch` against real data, or a schema change silently
breaks a route handler's query while every mocked unit test keeps passing.

## Architecture

Add a new coverage tier under `tests/live_db/` (the directory CI already runs
as `pytest tests/live_db/ -q`) that calls `execute_tool()` — the exact
function OpenAI's real MCP path calls — against a bare FastAPI app fixture
holding only the `voice_tools_*` routers, with a real Postgres connection
pool (the existing `db_connection` autouse fixture in
`tests/live_db/conftest.py`) and no mocking of `verify_call_token`,
`safety_layer`, or `queries.py`.

This is deliberately the same test-target directory and DB-resolution
mechanism (`TEST_DATABASE_URL`, prod-host guard) the existing `tests/live_db/`
suite already uses — whatever real-Neon target the concurrently-developed
ephemeral-branch CI pipeline wires that command to, these new tests inherit
automatically. No CI workflow file is touched by this work.

Two alternatives considered and rejected:
- **Raw HTTP client hitting `/voice/tools/{name}` directly**, bypassing
  `execute_tool()`. Rejected: skips `execute_tool()`'s own token-verification
  gate, so the thing most worth proving (a tampered/expired token is
  actually rejected end-to-end) would go untested.
- **Calling route handler functions directly in Python**, bypassing FastAPI.
  Rejected: skips header/pydantic validation, so malformed-input handling
  (a real attack surface — OpenAI-generated tool-call arguments are
  effectively untrusted input) would go untested.

## Components

### `tests/live_db/conftest.py` (extend, don't replace)

Add one new fixture:
- `tool_app` — a bare `FastAPI()` with only the four `voice_tools_*` routers
  included (`voice_tools_catalog`, `voice_tools_booking`,
  `voice_tools_identity`, `voice_tools_lifecycle`), no lifespan (matches the
  existing `tests/booking_engine/test_routes/conftest.py` pattern — the
  module-level connection pool from the autouse `db_connection` fixture is
  what the route handlers actually use, not `app.state`).

Extend the existing `cleanup_customer_ids` / `cleanup_appointment_ids`
fixtures' pattern with two new ones, `cleanup_call_ids` and
`cleanup_memo_ids`, needed by the lifecycle/escalation write tests.

### `tests/live_db/test_tool_dispatch_reads.py` (new)

One test per read tool, via `execute_tool(name, args, token=<minted>,
secret=<settings.openai_tool_secret>, app=tool_app)`:
- `lookup_customer` — real seeded phone number returns the real customer row.
- `get_services` — real seeded shop returns real seeded services.
- `get_staff_for_service` — real seeded service returns qualified staff.
- `check_availability` — real seeded service/staff returns real open slots
  (proves the underlying `find_availability` schema assumptions hold through
  the full dispatch path, not just the query layer).
- `get_booking` — real seeded customer + appointment returns that
  appointment.

### `tests/live_db/test_tool_dispatch_writes.py` (new)

One test per write tool, via the same `execute_tool()` call, each asserting
the mutation actually landed in Postgres (a follow-up read, not just the
tool's return value):
- `create_customer_from_call` — row exists in `customers` + `phone_contacts`
  after the call.
- `update_customer_from_call` — field actually changed in the row.
- `create_booking` — row exists in `appointments`, correctly attached to the
  call.
- `modify_booking` (authorized path) — `slot_start`/`service_id` actually
  changed.
- `cancel_booking` (authorized path) — status actually flips to cancelled.
- `mark_outcome` — call row's outcome/summary actually persisted.
- `escalate_to_merchant` — memo row actually created.

### `tests/live_db/test_tool_dispatch_security.py` (new)

The layer with zero coverage today:
- **Token integrity, real path:** a call to `execute_tool()` with a tampered
  token (flipped signature byte) and with a token signed under the wrong
  secret both return `{"ok": False, "error": "unauthorized"}` — proving the
  real `verify_call_token`, not a mock, actually gates dispatch.
- **Cross-shop rejection:** using the two real seeded shops already in
  `conftest.py` (`SHOP_ID`, `SHOP_ID_2`), a call token minted for shop A's
  call attempting `modify_booking`/`cancel_booking` on an appointment that
  belongs to shop B's data is rejected — proves authz holds against real
  cross-tenant rows, not just the mocked unit test's fake IDs.
- **Phone-mismatch rejection:** a call whose real `caller_phone` doesn't
  match the real appointment owner's phone is rejected with
  `phone_mismatch`, using real `phone_contacts` rows (normalization
  included) rather than mocked equality checks.
- **Constraint enforcement against real timestamps:** `modify_booking` /
  `cancel_booking` against a real appointment inside the configured
  lead-time window is rejected (`reschedule_too_close` /
  `cancel_too_close`); against a real slot already in the past is rejected
  (`slot_in_past`).
- **Malformed/garbage arguments don't 500 or leak internals:** one
  representative write tool (`create_booking`) called with a missing
  required field and with a non-UUID `customer_id` returns a clean 4xx-style
  `{"ok": False, ...}` shape, not a raw traceback — OpenAI-generated tool
  arguments are effectively untrusted input reaching this layer.

## Data Flow

1. Test fixture seeds real rows (reusing existing `seeded_shop` /
   `conftest.py` fixture data — `SHOP_ID`, `STAFF_MIRCO`,
   `SVC_TAGLIO_DONNA`, etc.).
2. Test mints a real call token via `mint_call_token()` (or deliberately
   tampers with one, for the security tests) and calls
   `execute_tool(name, args, token=..., secret=..., app=tool_app)` — the
   same function `mcp_server.py` calls for a real OpenAI tool invocation.
3. `execute_tool()` verifies the token for real, then proxies
   (`ASGITransport`) to the real route handler, which runs real
   `safety_layer` authz/constraint checks and real `queries.py` SQL against
   the live connection pool.
4. Test asserts on `execute_tool()`'s return value and, for writes, a
   follow-up direct read confirming the row state in Postgres.
5. Cleanup fixtures delete any rows created during the test, same pattern as
   the existing `tests/live_db/` suite.

## Safety / Scope

- No CI workflow files are touched — these tests live under `tests/live_db/`
  and are picked up by the existing `pytest tests/live_db/` invocation
  automatically.
- Never runs against production — reuses `tests/live_db/conftest.py`'s
  existing `_PROD_HOST_FRAGMENT` guard unchanged.
- `voice_gateway/` dead-code deletion is explicitly out of scope for this
  work (see Why) — flagged as a follow-up recommendation only.
- No new dependencies — `httpx`, `fastapi.testclient`/`ASGITransport`,
  `pytest-asyncio` are already in use by the existing suite.
- Out of scope: load/performance testing, prompt-injection testing (already
  validated manually per `project_architecture_divergence.md`'s
  `chat_agent.py` note — not a DB-layer concern), and the local voice+MCP
  browser test harness (separate, already-planned, human-in-the-loop tool
  for manual conversation testing — this spec is the automated,
  CI-running counterpart).

## Testing

This spec's deliverable *is* a test suite — no meta-tests needed beyond the
tests themselves. Verification is running `pytest tests/live_db/ -v` against
a real Neon connection (`TEST_DATABASE_URL` pointed at the QA branch or an
ephemeral clone) and confirming all new tests pass, then deliberately
breaking one thing at a time (e.g., temporarily commenting out the
phone-mismatch check in `safety_layer.py`) to confirm the corresponding new
test actually fails — proving the security tests would catch a real
regression, not just exercise the happy path.
