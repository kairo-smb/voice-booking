# Docsify Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `docs/knowledge/` — a Docsify-rendered, no-build-step documentation site covering this repo's technical architecture and voice-agent domain logic, mirroring `webapp/docs/knowledge/`'s structure and maintenance-rule convention (not CI automation).

**Architecture:** Plain markdown files under `docs/knowledge/`, rendered client-side by Docsify (CDN script, `index.html` + `_sidebar.md`). Every content page carries a `> **Maintenance rule:**` blockquote. `docs/DEPLOY_VOICE_AGENT.md` and `docs/INTEGRATION_GUIDE.md` are folded in and deleted. `CLAUDE.md` gets a short pointer block; its existing role as the append-only history log is untouched.

**Tech Stack:** Docsify 4 (CDN, no install), plain Markdown. Viewed via `npx --yes serve docs/knowledge`.

---

## Content facts gathered from source (reference for every task below)

Route inventory (from `booking_engine/api/app.py` + each router's `APIRouter(prefix=...)`):

| Router file | Mount prefix | Full paths | Auth |
|---|---|---|---|
| `shops.py` | `/api/v1` | `GET /api/v1/shops/{shop_id}` | **none** |
| `customers.py` | `/api/v1` | `GET, POST /api/v1/shops/{shop_id}/customers` | **none** |
| `services.py` | `/api/v1` | `GET /api/v1/shops/{shop_id}/services`, `GET /api/v1/shops/{shop_id}/staff`, `GET /api/v1/shops/{shop_id}/staff/{staff_id}/services` | **none** |
| `availability.py` | `/api/v1` | `GET /api/v1/shops/{shop_id}/availability` | **none** |
| `appointments.py` | `/api/v1` | `POST, GET /api/v1/shops/{shop_id}/appointments`, `PATCH .../appointments/{appointment_id}/cancel`, `PATCH .../appointments/{appointment_id}/reschedule` | **none** |
| `voice.py` | `/api/v1` | `GET /api/v1/shops/{shop_id}/voice/calls`, `GET .../voice/calls/{call_id}`, `PATCH .../voice/calls/{call_id}/link-customer`, `GET .../voice/analytics` | `require_control_plane_token` (Bearer `CONTROL_PLANE_SECRET`) |
| `voice_telephony.py` | `/api/v1` (prefix `/voice/numbers`) | `GET /api/v1/voice/numbers/search`, `POST .../provision`, `GET .../{shop_id}/setup-instructions`, `GET .../{shop_id}` | control-plane token |
| `voice_twiml.py` | `/api/v1` (prefix `/voice/twiml`) | `POST /api/v1/voice/twiml/incoming` | Twilio `X-Twilio-Signature` (validated via `RequestValidator(TWILIO_AUTH_TOKEN)`; **no-op if `TWILIO_AUTH_TOKEN` unset**) |
| `voice_openai.py` | none (top-level, prefix `/voice/openai`) | `POST /voice/openai/incoming` | signature checked **only if `OPENAI_WEBHOOK_SECRET` is set** — see `ponytail:` comment at top of the file, real known gap |
| `voice_config.py` | `/api/v1` (prefix `/voice/config`) | `GET /api/v1/voice/config/tones`, `GET /api/v1/voice/config/{shop_id}`, `PATCH /api/v1/voice/config/{shop_id}` | control-plane token |
| `voice_balance.py` | `/api/v1` (prefix `/voice/balance`) | `GET /api/v1/voice/balance/{shop_id}` | control-plane token |
| `voice_heartbeat.py` | `/api/v1` (prefix `/voice/heartbeat`) | `POST /api/v1/voice/heartbeat/forwarding` | control-plane token |
| `voice_tools_catalog.py` | none (prefix `/voice/tools`) | `POST /voice/tools/get_services`, `POST /voice/tools/get_staff_for_service` | `require_tool_token` (Bearer `OPENAI_TOOL_SECRET`) |
| `voice_tools_booking.py` | none (prefix `/voice/tools`) | `POST /voice/tools/check_availability`, `.../create_booking`, `.../get_booking`, `.../modify_booking`, `.../cancel_booking` | tool token |
| `voice_tools_identity.py` | none (prefix `/voice/tools`) | `POST /voice/tools/lookup_customer`, `.../create_customer_from_call`, `.../update_customer_from_call` | tool token |
| `voice_tools_lifecycle.py` | none (prefix `/voice/tools`) | `POST /voice/tools/mark_outcome`, `.../escalate_to_merchant` | tool token |
| `voice_events.py` | none (prefix `/voice/events`) | `POST /voice/events/session.started`, `.../session.turn`, `.../session.ended` | tool token |
| `voice_memos.py` | none (prefix `/voice/memos`) | `GET /voice/memos/{shop_id}`, `GET .../{shop_id}/count`, `PATCH .../{memo_id}` | control-plane token |
| `/mcp` (mounted directly) | — | MCP protocol endpoint OpenAI's Realtime client talks to; dispatches in-process to the `/voice/tools/*` handlers above | tool token (same secret, MCP-layer) |

`GET /health` — no auth, no prefix.

Response envelope: most routes wrap `{"data": ...}` (see `voice.py::_wrap`, and Pydantic `response_model`s elsewhere); error responses are `{"error": "...", "message": "..."}`; slot conflicts on booking return HTTP 409.

Env vars (from `booking_engine/config.py`, exhaustive):
`database_url`, `pool_min_size` (2), `pool_max_size` (10), `control_plane_secret`, `public_base_url`, `twilio_account_sid`, `twilio_auth_token`, `twilio_default_country` (`EE`), `twilio_bundle_sid`, `twilio_address_sid`, `openai_sip_project_id`, `openai_api_key`, `openai_realtime_model` (`gpt-realtime`), `openai_webhook_secret`, `openai_tool_secret`, `voice_kairo_tokens_per_second` (18), `voice_min_session_reserve_tokens` (1500), `voice_cancellation_lead_time_hours` (2), `enable_call_supervisor` (False), `call_supervisor_verbose_logging` (False), `sip_test_fallback_shop_id` (empty).

`voice_agent` schema tables (from `booking_engine/db/sql/03`–`10`, in migration order): `calls`, `call_transcripts`, `call_events` (03); `shop_telephony`, `shop_config`, `callback_memos`, `auth_events`, `system_policy` (04); `voice_tones` (06, 8 seeded presets: professionale, amichevole, efficiente, luxury, tecnico, casual, empatico, conciso); `shop_config.greeting_overflow` (07); `calls.service_brief` (08); `shop_telephony.provider` default `'twilio'` (09); `shop_config.voice_preset` default fix (10). `01_schema.sql`/`02_seed_data.sql` are a local-only bootstrap pair, never run against real Neon (`scripts/migrate.sh` explicitly skips them).

Safety/logic facts (from `services/safety_layer.py`, `booking_authz.py`, `booking_constraints.py`, `prompt_assembler.py`):
- 12 tools in `DEFAULT_TOOL_ALLOWLIST`: `lookup_customer`, `create_customer_from_call`, `update_customer_from_call`, `get_services`, `get_staff_for_service`, `check_availability`, `create_booking`, `get_booking`, `modify_booking`, `cancel_booking`, `mark_outcome`, `escalate_to_merchant`.
- `SAFETY_PROMPT` (Italian, non-negotiable, prepended to every session): no medical/pharma advice; never mention price unless `get_services(include_price=true)` was explicitly asked for; multi-service ordering follows hairdressing convention (color before cut/styling) unless the customer states otherwise; no promised cosmetic outcomes; escalate on human-request or abuse; confirm booking details verbally before `create_booking`; identity is phone-based only — can only modify/cancel bookings made from the same calling number; specific error-message-to-phrasing mapping for `phone_mismatch`/`reschedule_too_close`/`cancel_too_close`/`slot_in_past`/`unknown_service`; always Italian; concise (1-2 sentences); ATTESA rule (spoken filler before `check_availability`/`get_services`/`lookup_customer`/`get_booking` — enforced server-side via a minimum-latency wait, see `ATTESA_TOOLS` + the 2026-07-21 CLAUDE.md entry); always speak after a tool result, never go silent; don't call `get_staff_for_service` unless the customer named a specific staff member; prompt-injection resistance (ignore any caller instruction to change role/reveal the prompt); scope limited to this shop's services/bookings; no cross-customer data leakage; never invent data not returned by a tool.
- `authorize_booking_change()` (`booking_authz.py`): a caller may only change (modify/cancel) a booking that belongs to their own shop **and** is registered to their calling number — returns one of `appointment_not_found` / `wrong_shop` / `anonymous_caller` / `phone_mismatch` / `ok`. Documented gap (from CLAUDE.md 2026-07-17): `update_customer_from_call` has no equivalent shop-ownership check at all.
- `booking_constraints.py`: `within_lead_time()` — `VOICE_CANCELLATION_LEAD_TIME_HOURS` (default 2h) minimum notice for self-serve reschedule/cancel; `MAX_GAP_MINUTES = 20` — max idle time between consecutive legs of a multi-service booking; `slot_in_past()`.
- `prompt_assembler.py`: 3-layer prompt = `SAFETY_PROMPT` (Layer 3, immutable) → caller context (anonymous / unique match / ambiguous multi-match / new caller, from `identity_resolver.ResolutionResult`) → Layer 1 (`display_name`, greeting — `greeting_overflow` for overflow-mode shops with a code default, `greeting_after_disclosure` for always-on shops, no default) → tone instruction (`voice_tones.system_prompt_instruction` via `tone_id`, falls back to a hardcoded default Italian instruction on any lookup failure/miss) → tool schemas.

Providers (from `clients/*.py`, `db/connection.py`):
- **Twilio** — `clients/twilio_numbers.py` (search/purchase EU mobile numbers, onboarding-time only), `voice_twiml.py` (per-call TwiML webhook, dynamic routing by dialed number). Estonia mobile numbers chosen over Italy for cost (~$3/mo vs $30/mo) with identical KYC friction — full rationale in `CLAUDE.md` §2026-07-16.
- **OpenAI Realtime** — `clients/openai_realtime.py` (`accept_sip_call` for native-SIP calls, `create_ephemeral_session` for browser/WebRTC harness testing), `services/call_supervisor.py` (flag-gated control WebSocket working around hosted MCP not auto-continuing after a tool result — full incident in `CLAUDE.md` §2026-07-21 "Realtime + hosted MCP..." and §2026-07-21 "SIP call supervisor..."). Tool dispatch is in-process ASGI (`mcp_server.py`), not a real HTTP hop — `CLAUDE.md` §2026-07-24 "dead air" entry.
- **Neon Postgres** — `db/connection.py`, asyncpg pool (`pool_min_size=2`, `pool_max_size=10`, **no `pool.acquire()` timeout configured anywhere** — flagged, not yet actioned, in `CLAUDE.md` §2026-07-21 "Cost-gated pricing..."). Ephemeral copy-on-write branches for CI — `CLAUDE.md` §2026-07-18.
- **Push notifications** — `clients/push_notifications.py` is a **stub**: logs the event, does not actually push anywhere yet ("Plan C wires this to the webapp's existing notification infrastructure").

CLAUDE.md entries available for `decisions.md` (all `## ` headers, newest first, exact as of this plan):
1. 2026-07-24 — Repo cleanup: deleted dead docs/scripts, rewrote two stale docs, closed a dependency drift
2. 2026-07-24 — Root-caused session "dead air": tool calls were self-proxying over real HTTPS
3. 2026-07-21 — Reviewed voice-config WIP commit; found and closed a missing-migration gap for tone_id
4. 2026-07-21 — SIP call supervisor: production fix for the mute-after-MCP blocker (built)
5. 2026-07-21 — Cost-gated pricing + multi-service/multi-staff bookings
6. 2026-07-21 — Realtime + hosted MCP does NOT auto-speak tool results (prod blocker)
7. 2026-07-21 — MCP server_url must carry a trailing slash (prod + harness)
8. 2026-07-18 — CI/CD: ephemeral Neon branches, seed-data bug fix, Lambda removal
9. 2026-07-17 — Live tool-dispatch + security test coverage
10. 2026-07-16 — Telephony provider: Telnyx → Twilio

---

## Task 1: Scaffold the Docsify shell

**Files:**
- Create: `docs/knowledge/index.html`
- Create: `docs/knowledge/_sidebar.md`

- [ ] **Step 1: Create the directory and the Docsify loader**

```bash
mkdir -p docs/knowledge/api
```

Write `docs/knowledge/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Kairo Voice Booking — Knowledge Base</title>
  <meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/docsify@4/lib/themes/vue.css">
</head>
<body>
  <div id="app"></div>
  <script>
    window.$docsify = {
      name: 'Kairo Voice Booking — Knowledge Base',
      loadSidebar: true,
      subMaxLevel: 3,
      auto2top: true,
      search: {
        maxAge: 0,
        paths: 'auto',
        placeholder: 'Search…'
      }
    }
  </script>
  <script src="https://cdn.jsdelivr.net/npm/docsify@4"></script>
  <script src="https://cdn.jsdelivr.net/npm/docsify/lib/plugins/search.min.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write a placeholder sidebar (finalized in Task 14)**

Write `docs/knowledge/_sidebar.md`:

```markdown
- [Home](/)
- [Architecture](architecture.md)
- [Database](database.md)
- [Voice Agent Logic](voice-agent-logic.md)
- [Providers](providers.md)
- [Operations](operations.md)
- [Decisions](decisions.md)
- API
  - [Overview](api/README.md)
  - [Business API](api/business.md)
  - [Telephony Webhooks](api/telephony-webhooks.md)
  - [Voice Tools](api/voice-tools.md)
  - [Voice Control Plane](api/voice-control-plane.md)
```

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/index.html docs/knowledge/_sidebar.md
git commit -m "docs: scaffold docsify knowledge base shell"
```

---

## Task 2: README.md (homepage + maintenance rule)

**Files:**
- Create: `docs/knowledge/README.md`

- [ ] **Step 1: Write the homepage**

```markdown
# Kairo Voice Booking — Knowledge Base

Human-oriented documentation for the voice-booking `booking_engine` service: what exists, how it behaves, and the nuances that matter for continuing the work. This is **not** an implementation reference — the code is the only ground truth for exactly how something works. This knowledge base exists to capture what the code can't say for itself: purpose, current behavior, external dependencies, and hard-won gotchas.

## Contents

- **[Architecture](architecture.md)** — single-service topology, the call flow from Twilio through OpenAI's native SIP into this service, in-process MCP tool dispatch
- **[Database](database.md)** — the `business_app_core` / `voice_agent` schema ownership boundary, and every `voice_agent` table
- **[Voice Agent Logic](voice-agent-logic.md)** — the domain rules: safety prompt, booking authorization, lead-time/gap constraints, prompt assembly, the tone system
- **[Providers](providers.md)** — every external service (Twilio, OpenAI Realtime, Neon, push notifications): purpose, auth, hard rules
- **[Operations](operations.md)** — deploy, migrations, CI, env vars, secrets, live-call testing
- **[Decisions](decisions.md)** — a short index into `CLAUDE.md`'s history log, organized for lookup rather than chronology
- **[API](api/README.md)** — REST/webhook/tool contract docs for every route in `booking_engine/api/routes/`, grouped by who calls them

## Maintenance rule

**Any change that adds, removes, or changes a REST/voice-tool endpoint, a database table (in either `business_app_core` or `voice_agent`), a provider integration, or a safety/authz/booking-constraint rule updates the matching file here in the same change — not as a follow-up.** This is enforced by whoever (human or agent) makes the change, not by tooling. `CLAUDE.md` points here.

If this rule stops being followed and the docs rot again, the next escalation is an automated staleness check (e.g. CI failing when a route exists with no matching `api/*.md` entry) — add that when manual discipline demonstrably fails, not before.

## Viewing as a site

This folder is a [Docsify](https://docsify.js.org/) site — plain markdown, rendered client-side, no build step. To view it locally:

```bash
npx --yes serve docs/knowledge
```

Then open the printed URL. (Opening `index.html` directly via `file://` won't work — Docsify fetches the `.md` files over HTTP.)
```

- [ ] **Step 2: Verify the maintenance rule is present**

```bash
grep -q "Maintenance rule" docs/knowledge/README.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/README.md
git commit -m "docs: add knowledge base homepage and maintenance rule"
```

---

## Task 3: architecture.md

**Files:**
- Create: `docs/knowledge/architecture.md`

- [ ] **Step 1: Write the page**

```markdown
# Architecture

> **Maintenance rule:** a change to the service topology, the call flow, or the tool-dispatch mechanism updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Topology

```
Caller (phone) → Twilio (TwiML) → OpenAI Realtime API (native SIP, STT/LLM/TTS)
                                          ↕ MCP tool calls (in-process ASGI, /mcp)
                                    booking_engine (Fly.io, single service)
                                          ↕ SQL (asyncpg)
                                    Neon PostgreSQL (business_app_core + voice_agent)
```

One deployed service — `booking_engine`, a FastAPI app (`booking_engine/api/app.py`). There is no separate "voice gateway" process; an earlier two-service split was unified here (see `CLAUDE.md` §2026-07-21 "Realtime + hosted MCP..." and the architecture-divergence note it superseded).

## Call flow

1. A call reaches a Twilio number. Twilio POSTs to `POST /api/v1/voice/twiml/incoming` (`voice_twiml.py`), signature-checked against `TWILIO_AUTH_TOKEN`. The handler looks up the shop by the dialed number and returns TwiML that `<Dial><Sip>`s straight into OpenAI's SIP gateway, passing the shop id as a custom SIP header (`X-Shop-Id`, via Twilio's `<Dial><Sip>` query-string-after-host convention — see `services/realtime_session.py::build_sip_uri`).
2. OpenAI fires `realtime.call.incoming` to `POST /voice/openai/incoming` (`voice_openai.py`, top-level path, not under `/api/v1`). The handler reads `X-Shop-Id` back out of the SIP headers (or, QA-only, falls back to `SIP_TEST_FALLBACK_SHOP_ID` for a raw softphone test call with no Twilio in the path), resolves the caller by phone (`services/identity_resolver.py`), assembles the session prompt (`services/prompt_assembler.py` — see [Voice Agent Logic](voice-agent-logic.md)), and calls `accept_sip_call()` (`clients/openai_realtime.py`) with that prompt + the 12 tool schemas.
3. During the call, OpenAI calls tools over MCP against `/mcp` (mounted directly on this app in `app.py`, via `booking_engine/mcp_server.py`). Tool dispatch is **in-process** — `execute_tool()` uses an `ASGITransport(app=app)` call into the exact same running process rather than a real HTTP hop, wrapped in a 10s `asyncio.wait_for` (`TOOL_CALL_TIMEOUT_SECONDS`, `services/mcp_tools.py`) that returns a clean `{"ok": false, "error": "tool_timeout"}` on a stuck downstream call rather than hanging. This was a deliberate fix for real "dead air" latency caused by an earlier version that made a genuine outbound HTTPS request to the app's own public URL on every tool call — full incident in `CLAUDE.md` §2026-07-24.
4. If `ENABLE_CALL_SUPERVISOR` is set, a per-call background task (`services/call_supervisor.py`) opens its own control WebSocket to the accepted call and sends `response.create` on connect (greeting) and after each tool result (`response.output_item.done` for an `mcp_call`) — working around OpenAI's hosted MCP not auto-continuing after a tool result on its own. Off by default; see `CLAUDE.md` §2026-07-21 for why it exists and its current live-test status.
5. On hangup, the call is finalized via `voice_events.py`'s `session.*` webhooks (started/turn/ended), persisting to `voice_agent.calls`/`call_transcripts`/`call_events`.

## Auth boundaries

Three distinct secrets gate three distinct callers — see [API overview](api/README.md) for the full breakdown:
- `CONTROL_PLANE_SECRET` — the separate `webapp` Control Plane repo, reading/writing voice config, calls, analytics, telephony provisioning.
- `OPENAI_TOOL_SECRET` — OpenAI's Realtime tool/event calls (`/voice/tools/*`, `/voice/events/*`), and the MCP mount.
- Twilio request-signature verification (`TWILIO_AUTH_TOKEN`) — the TwiML webhook only.
- The OpenAI `realtime.call.incoming` webhook (`/voice/openai/incoming`) currently verifies a signature **only if `OPENAI_WEBHOOK_SECRET` is set** — unset, it accepts unsigned requests (a known, flagged gap; see the `ponytail:` comment at the top of `voice_openai.py`).
- The plain REST API (`shops`, `customers`, `services`, `availability`, `appointments`) has **no auth dependency at all** today.

## Alternate entrypoint (testing only)

`clients/openai_realtime.py::create_ephemeral_session` mints a browser/WebRTC session for local testing (`scripts/voice_test_server.py`, `scripts/run_webrtc_harness.sh`) — a different transport than production SIP calls. See [Operations](operations.md) for the live-SIP softphone test path, which exercises the real call flow above end-to-end without needing a funded Twilio number.
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/architecture.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/architecture.md
git commit -m "docs: add architecture page to knowledge base"
```

---

## Task 4: database.md

**Files:**
- Create: `docs/knowledge/database.md`

- [ ] **Step 1: Write the page**

```markdown
# Database

> **Maintenance rule:** a schema migration that adds/removes/renames a `voice_agent` table or column updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Ownership boundary

Two schemas in one Neon Postgres database:

| Schema | Owner | This repo's access |
|---|---|---|
| `business_app_core` | the `webapp` Control Plane repo | reads/writes narrowly through `booking_engine/db/queries.py`; **never alters its DDL** |
| `voice_agent` | this repo | owns it fully — DDL lives in `booking_engine/db/sql/`, applied in order by `scripts/migrate.sh` |

**Do not hand-copy `business_app_core`'s schema into a doc.** That has already gone stale and caused real bugs at least twice (`CLAUDE.md` §2026-07-24 "Repo cleanup..." and the schema-mismatch history it references). The accurate, current mapping is `booking_engine/db/queries.py`, exercised against real Neon-shaped data by `tests/live_db/*`. Read that file for column names, not this one.

## `voice_agent` schema — authoritative here

DDL: `booking_engine/db/sql/03_voice_agent_schema.sql` through `10_shop_config_voice_preset_default.sql`, applied in filename order. `01_schema.sql`/`02_seed_data.sql` are a **separate, local-only bootstrap pair** with fake data and unqualified table names — `scripts/migrate.sh` explicitly skips both; never run them against real Neon.

| Table | Added in | Purpose |
|---|---|---|
| `calls` | 03, extended 04/08 | one row per inbound call — caller number, matched/created customer, outcome, and (08) a structured hairstylist `service_brief` |
| `call_transcripts` | 03 | per-turn transcript rows for a call |
| `call_events` | 03 | tool-call/event log for a call |
| `shop_telephony` | 04, extended 09 | provisioned Twilio number per shop, `setup_path` (new/forward), `provider` (defaults `'twilio'` since 09) |
| `shop_config` | 04, extended 06/07/10 | Layer 1 voice config: `enabled`, `display_name`, greetings, `voice_preset`, `tone_id` (06, FK to `voice_tones`, replaced an inline `tone_preset` string), `business_hours`, `answer_mode`, token top-up settings |
| `callback_memos` | 04 | merchant callback reminders created by `escalate_to_merchant` |
| `auth_events` | 04 | identity-verification audit trail |
| `system_policy` | 04 | disclosure/consent text (seeded it-IT) |
| `voice_tones` | 06 | 8 seeded presets (`is_preset=true`) plus room for shop-authored custom tones (`created_by_shop_id`); seeded names: professionale, amichevole, efficiente, luxury, tecnico, casual, empatico, conciso |

`business_app_core.shops` also gained two columns directly in migration 03: `voice` (default `'alloy'`) and `language` (default `'it'`) — the one place this repo's migrations touch the other schema, both additive/nullable-safe.

## Cross-schema references

`voice_agent.calls` FKs into `business_app_core.shops`/`customers`/`appointments` — cross-schema foreign keys are used deliberately rather than duplicating those rows into `voice_agent`.

## Connection

`booking_engine/db/connection.py` — a single asyncpg pool (`pool_min_size=2`, `pool_max_size=10`, both from `Settings`). **No `pool.acquire()` timeout is configured anywhere in this codebase** — under enough concurrent calls the pool itself becomes a contention point with no bound on the wait (flagged, not yet actioned, in `CLAUDE.md` §2026-07-21 "Cost-gated pricing..."; not urgent while call volume is near zero).
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/database.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/database.md
git commit -m "docs: add database page to knowledge base"
```

---

## Task 5: voice-agent-logic.md

**Files:**
- Create: `docs/knowledge/voice-agent-logic.md`

- [ ] **Step 1: Write the page**

```markdown
# Voice Agent Logic

> **Maintenance rule:** a change to `safety_layer.py`, `booking_authz.py`, `booking_constraints.py`, `prompt_assembler.py`, or the tone system updates this file in the same change. See [README](README.md#maintenance-rule).

The domain rules the agent enforces — why they exist, not just that they do. Source: `booking_engine/services/{safety_layer,booking_authz,booking_constraints,prompt_assembler,identity_resolver}.py`.

---

## The 12 tools

`safety_layer.py::DEFAULT_TOOL_ALLOWLIST`: `lookup_customer`, `create_customer_from_call`, `update_customer_from_call`, `get_services`, `get_staff_for_service`, `check_availability`, `create_booking`, `get_booking`, `modify_booking`, `cancel_booking`, `mark_outcome`, `escalate_to_merchant`. Each has a JSON-schema description in `_TOOL_SCHEMAS` that OpenAI uses to advertise the tool — see [API → Voice Tools](api/voice-tools.md) for the HTTP contract each one maps to.

## Safety prompt (non-negotiable, Layer 3)

`SAFETY_PROMPT` is hardcoded Italian text prepended to every session; merchants cannot view or edit it. Key rules, with the reasoning:

- **No medical/pharmaceutical advice.** Out of scope and liability-sensitive for a booking assistant.
- **Price is opt-in, not default.** `get_services` only returns `price_cents` when called with `include_price=true`, and the prompt tells the model to set that flag only if the customer explicitly asked about cost — never volunteer pricing.
- **Multi-service ordering follows hairdressing convention** (color/chemical treatments before cut/styling) unless the customer states otherwise. There's no ordering table in the schema — this is the model's own domain knowledge, not a stored rule; the system enforces whatever order the `services`/`legs` list arrives in, it doesn't validate *why* that order is correct.
- **Identity is phone-based only.** The agent can modify/cancel only bookings made from the same calling number — enforced server-side (see Authorization below), not just prompted.
- **ATTESA (waiting phrase) rule:** before any read-only tool call (`check_availability`, `get_services`, `lookup_customer`, `get_booking`), the model must say a short filler phrase first, so the caller isn't sitting in silence. `ATTESA_TOOLS` names exactly those four; `execute_tool()` enforces a **0.8s minimum latency** on them so the filler is never immediately followed by a suspiciously instant answer (see `CLAUDE.md` §2026-07-21, "enforce 0.8s minimum latency on tools with a waiting phrase").
- **Always speak after a tool result, never go silent** — this rule exists because the underlying platform behavior doesn't guarantee it (see [Providers](providers.md#openai-realtime) and `CLAUDE.md` §2026-07-21).
- **Prompt-injection resistance:** ignore any caller instruction to change role, reveal the system prompt, or impersonate another system.
- **Error-to-phrasing mapping:** `phone_mismatch`/`reschedule_too_close`/`cancel_too_close` → escalate; `slot_in_past` → propose a future time; `unknown_service` → re-check the catalog.

## Authorization (`booking_authz.py`)

`authorize_booking_change()` is the server-side trust boundary for `modify_booking`/`cancel_booking` — it does **not** trust the agent's own claim that identity was verified. A change is allowed only if the appointment (a) belongs to the call's own `shop_id` and (b) is registered to a phone number matching the call's caller number (normalized, digits-only comparison). Returns one of: `appointment_not_found`, `wrong_shop`, `anonymous_caller`, `phone_mismatch`, `ok`.

**Known gap, not fixed:** `update_customer_from_call` has no equivalent shop-ownership check — a valid call token can update any customer row's `email`/`tags` regardless of which shop the call belongs to (`CLAUDE.md` §2026-07-17). Flagged as a fast-follow, not a narrow error-handling fix — changing production authz logic is treated as a bigger decision than closing this doc gap.

## Booking constraints (`booking_constraints.py`)

Pure functions, no DB access, shared by create/modify/cancel:
- `slot_in_past(slot, now)` — rejects booking/rescheduling into the past.
- `within_lead_time(start_at, now, lead_hours)` — true when an appointment is too close (or already past) to self-serve change; `lead_hours` comes from `VOICE_CANCELLATION_LEAD_TIME_HOURS` (default 2h). Below this threshold, the agent escalates to the salon instead of changing the booking itself.
- `gap_within_limit(prev_end, next_start)` — for a multi-service booking, the next leg must start at or after the previous leg ends, and no more than `MAX_GAP_MINUTES` (20) later. This bounds how much idle time a chain of services (e.g. color, then piega with a different stylist) can leave between legs.

**Known gap, not fixed:** legs within one `create_booking` request are validated against existing DB rows individually, but never against *each other* — nothing stops two legs in the same request assigning the same staff member to overlapping times if the model sent a fabricated (not copied-from-`check_availability`) `legs` array (`CLAUDE.md` §2026-07-21, "Cost-gated pricing...").

## Prompt assembly (`prompt_assembler.py`)

Four layers composed in order into the session prompt sent on `session.started`:
1. **Layer 3 — `SAFETY_PROMPT`** (above), immutable.
2. **Caller context** — built from `identity_resolver.py`'s `ResolutionResult`: anonymous caller ID → greet neutrally, ask for name + spoken phone number; unique phone match → greet by name, mention last visit / notes; multiple customers share this number → ask who the booking is for before proceeding; no match → treat as a new caller, only create a customer record once a name is confirmed.
3. **Layer 1 — shop identity** — `display_name`, and a greeting: `answer_mode == "overflow"` shops use `greeting_overflow` (falling back to a generated default `"Salve, sono l'assistente di {name}. Come posso aiutarla?"` if the shop hasn't written one) since they're standing in for busy staff; other shops use `greeting_after_disclosure` with no code fallback (shop-authored, via the webapp).
4. **Tone instruction** — resolved from `shop_config.tone_id` against `voice_agent.voice_tones`; any lookup failure, missing id, or unknown tone falls back to a hardcoded default Italian instruction ("clear and professional"), never a hard error.

## Tone system

8 seeded presets in `voice_tones` (see [Database](database.md)) — each is a `(name, description, system_prompt_instruction)` triple. Shops can eventually author custom tones (`created_by_shop_id` column exists) — not yet exposed in the webapp UI as of this writing.

## Call supervisor behavior

See [Architecture](architecture.md#call-flow) for the mechanism; the *behavioral* rule it exists to enforce is the "always speak after a tool result" rule above — `services/call_supervisor.py`'s `decide()` triggers exactly one `response.create` per tool result (via `response.output_item.done` on an `mcp_call`, guarded by `nudge_pending` to prevent double-nudging on parallel tool calls) and one on connect (the opening greeting, since the SIP accept path itself never triggers one).
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/voice-agent-logic.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/voice-agent-logic.md
git commit -m "docs: add voice agent logic page to knowledge base"
```

---

## Task 6: providers.md

**Files:**
- Create: `docs/knowledge/providers.md`

- [ ] **Step 1: Write the page**

```markdown
# Providers

Every external service this repo talks to: purpose, auth, and the hard rules that keep the integration safe. Verified against the actual client code.

> **Maintenance rule:** adding/removing/changing a provider integration updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Twilio

**Purpose:** inbound phone numbers and call routing. `clients/twilio_numbers.py` searches/purchases EU mobile numbers at onboarding time only; `api/routes/voice_twiml.py` handles the per-call dynamic TwiML webhook that routes an inbound call to the right shop and dials it into OpenAI.

**Key files:** `booking_engine/clients/twilio_numbers.py`, `booking_engine/api/routes/voice_twiml.py`, `booking_engine/api/routes/voice_telephony.py` (provisioning endpoints), `booking_engine/services/realtime_session.py::build_sip_uri` (how the shop id is attached to the outbound SIP dial).

**Env vars:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_DEFAULT_COUNTRY` (`EE`), `TWILIO_BUNDLE_SID`, `TWILIO_ADDRESS_SID`.

**Why Estonia, not Italy:** Estonia Mobile numbers are ~$3/mo vs. Italy Mobile's $30/mo (the only type Twilio sells there), with identical KYC friction (documents anywhere in the world, reused across every provisioned number via one Kairo-entity regulatory Bundle). Full country-by-country comparison in `CLAUDE.md` §2026-07-16.

**Gotcha:** `_twilio_signature_valid()` in `voice_twiml.py` validates `X-Twilio-Signature` against `TWILIO_AUTH_TOKEN` — this is a real no-op (always accepts) if `TWILIO_AUTH_TOKEN` is unset, which is the case until the Twilio account is funded and configured.

## OpenAI Realtime

**Purpose:** speech-to-text, LLM reasoning, text-to-speech, and voice activity detection for the live call — via **native SIP** (production path) or an ephemeral browser/WebRTC session (local testing only).

**Key files:** `booking_engine/clients/openai_realtime.py` (`accept_sip_call`, `create_ephemeral_session`), `booking_engine/api/routes/voice_openai.py` (`realtime.call.incoming` webhook), `booking_engine/services/call_supervisor.py`, `booking_engine/mcp_server.py` (hosted MCP tool mount).

**Env vars:** `OPENAI_SIP_PROJECT_ID`, `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL` (`gpt-realtime` — not `gpt-4o-realtime-preview`), `OPENAI_WEBHOOK_SECRET`, `OPENAI_TOOL_SECRET`, `ENABLE_CALL_SUPERVISOR`, `CALL_SUPERVISOR_VERBOSE_LOGGING`.

**Hard-won gotcha #1 — hosted MCP does not auto-continue.** After a tool call, the model's response ends (`response.done` fires *before* the tool even returns); the tool executes, `response.output_item.done` delivers the result, and then nothing — OpenAI does not open a new response to voice it. This directly contradicts the Responses-API "hosted MCP auto-continues" assumption. Full event-trace evidence and the fix (a server-side control WebSocket sending `response.create`) in `CLAUDE.md` §2026-07-21 (two entries: "Realtime + hosted MCP..." and "SIP call supervisor...").

**Hard-won gotcha #2 — `server_url` needs a trailing slash.** `app.mount("/mcp", ...)` makes Starlette 307-redirect bare `/mcp` → `/mcp/`, and OpenAI's Realtime MCP client does **not** follow that redirect for the tool-call POST body — it silently never calls the tool. Always point `server_url` at `/mcp/`. Root-caused via `fly logs`; full story in `CLAUDE.md` §2026-07-21 "MCP server_url must carry a trailing slash".

**Gotcha #3 — webhook signature is opt-in.** `voice_openai.py`'s `realtime.call.incoming` handler only verifies a signature when `OPENAI_WEBHOOK_SECRET` is set (see the `ponytail:` comment at the top of that file) — currently unwired, so the endpoint accepts unsigned requests.

## Neon PostgreSQL

**Purpose:** the shared database — `business_app_core` (owned by the `webapp` Control Plane) plus `voice_agent` (owned by this repo). See [Database](database.md).

**Key files:** `booking_engine/db/connection.py` (asyncpg pool).

**Env vars:** `DATABASE_URL` (pooler endpoint, port 5432, transaction mode).

**CI usage:** every DB-touching GitHub Actions workflow (`ci.yml`, `deploy-qa.yml`, `deploy-fly-prod.yml`) provisions a throwaway, copy-on-write Neon branch off production, migrates + tests against it, then deletes it — never touches the real QA/production branch until that passes. Full rationale (a real seed-data bug this caught) in `CLAUDE.md` §2026-07-18.

## Push notifications (stub, not wired)

**Purpose:** intended to alert merchants (low balance, new callback memo, etc.) on their devices.

**Key files:** `booking_engine/clients/push_notifications.py`.

**Current state:** `send_push()` only logs the event — "Plan C wires this to the webapp's existing notification infrastructure" is a comment, not yet built. Don't assume any push notification actually reaches a merchant device today.
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/providers.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/providers.md
git commit -m "docs: add providers page to knowledge base"
```

---

## Task 7: operations.md (absorbs docs/DEPLOY_VOICE_AGENT.md)

**Files:**
- Create: `docs/knowledge/operations.md`
- Read (for content, then delete in Task 14): `docs/DEPLOY_VOICE_AGENT.md`

- [ ] **Step 1: Write the page**

```markdown
# Operations

Deploy, migrations, CI, environment variables, and live-call testing.

> **Maintenance rule:** a change to CI, a migration workflow, a deploy step, or a required env var updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Branching & environments

Two Fly.io apps from the same `booking_engine/Dockerfile.fly`: production (`fly.toml`, app `kairo-booking-engine`, `min_machines_running = 0`) and QA (`fly.qa.toml`, app `kairo-booking-engine-qa`, `min_machines_running = 1`). Deploys automatically via GitHub Actions on push to `main` (production, `deploy-fly-prod.yml`) or `QA` (`deploy-qa.yml`) — both run tests and migration checks against a throwaway Neon branch first (see [Providers → Neon](providers.md#neon-postgresql)).

Manual deploy:
```bash
fly auth login
flyctl deploy --config fly.toml      # production
flyctl deploy --config fly.qa.toml   # QA
```

## Migrations

```bash
DATABASE_URL="$DATABASE_URL" ./scripts/migrate.sh
```
Applies every file in `booking_engine/db/sql/` in order, **except** `01_schema.sql`/`02_seed_data.sql` (a local-only bootstrap pair — the script skips them explicitly). See [Database](database.md) for what each migration adds.

## Secrets

`CONTROL_PLANE_SECRET` and `OPENAI_TOOL_SECRET` are Fly app secrets, not GitHub Actions secrets — `flyctl deploy` doesn't inject them:
```bash
fly secrets set CONTROL_PLANE_SECRET='...' OPENAI_TOOL_SECRET='...' --app kairo-booking-engine
```
Full env var list: `booking_engine/config.py`'s `Settings` class is the exhaustive source; the auth-relevant subset is restated per-provider in [Providers](providers.md).

## Post-deploy smoke test

Pick any active shop UUID from the DB:
```bash
URL='https://kairo-booking-engine.fly.dev'
SECRET='<CONTROL_PLANE_SECRET>'
SHOP_ID='<existing shop UUID>'
H="Authorization: Bearer $SECRET"

curl -s -o /dev/null -w '%{http_code} (expect 401)\n' "$URL/api/v1/voice/config/$SHOP_ID"
curl -s -H "$H" "$URL/api/v1/voice/config/$SHOP_ID" | jq
curl -s -H "$H" -H 'Content-Type: application/json' \
  -X PATCH "$URL/api/v1/voice/config/$SHOP_ID" \
  -d '{"greeting_after_disclosure":"Smoke test"}' | jq
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/calls" | jq
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/analytics" | jq
```
Pass: step 1 → `401`; steps 2-3 → `{"data": {...}}` with the expected config; steps 4-5 → `{"data": [...] | {...}}`, empty/zeroed for a fresh shop.

## Testing a real call without a phone

`scripts/voice_test_server.py`'s browser/WebRTC harness (`./scripts/run_webrtc_harness.sh`) is convenient but a different transport than production — real calls arrive over SIP. You don't need a funded Twilio number to test the real SIP path: OpenAI's SIP gateway accepts a call from *any* SIP client dialed straight at the project's SIP URI, firing the exact same `realtime.call.incoming` webhook a Twilio-forwarded call would.

1. Install a SIP softphone that supports TLS (e.g. [Linphone](https://www.linphone.org/en/), or `pjsua` from `pjproject`).
2. Get a shop UUID from the QA Neon branch, and the OpenAI SIP project id (same value as the `OPENAI_SIP_PROJECT_ID` Fly secret on `kairo-booking-engine-qa`).
3. Get the dial target and header:
   ```bash
   set -a; source .env; set +a
   python scripts/print_sip_test_uri.py <shop_id>
   ```
   This prints a bare dial URI (`sip:{project}@sip.api.openai.com;transport=tls`) and a separate custom header (`X-Shop-Id: {shop_id}`) — **a raw softphone dial has no Twilio in the path to attach that header for you.** Without it, the call reaches OpenAI but has no shop to route to and gets rejected before ringing. Add it via your softphone's custom-header support if it has one (`pjsua --help | grep -i header`, or a GUI client's custom-headers field).
4. Watch `fly logs -a kairo-booking-engine-qa`.

To also exercise the call-supervisor fix (greeting + post-tool speech) and see full debug output:
```bash
fly secrets set ENABLE_CALL_SUPERVISOR=true CALL_SUPERVISOR_VERBOSE_LOGGING=true --app kairo-booking-engine-qa
# test call, then: fly logs -a kairo-booking-engine-qa — confirm a "supervisor.greeted" line
fly secrets unset ENABLE_CALL_SUPERVISOR CALL_SUPERVISOR_VERBOSE_LOGGING --app kairo-booking-engine-qa
```
`CALL_SUPERVISOR_VERBOSE_LOGGING` also turns on caller-speech transcription (normally off) — keep it off outside a deliberate debug session, since it puts full conversation content into `fly logs`.

## Running tests locally

```bash
pytest tests/ --ignore=tests/live_db -v          # no DB needed
DATABASE_URL=postgresql://... pytest tests/live_db/ -v   # real/ephemeral Neon branch
```
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/operations.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/operations.md
git commit -m "docs: add operations page to knowledge base"
```

---

## Task 8: decisions.md

**Files:**
- Create: `docs/knowledge/decisions.md`

- [ ] **Step 1: Write the page**

```markdown
# Decisions

A short index into `CLAUDE.md`'s full history log — enough to find the relevant entry without reading the whole file. `CLAUDE.md` is the source of truth; this page is a lookup aid, not a duplicate, and is kept in sync by hand when `CLAUDE.md` gains a new entry worth indexing.

> **Maintenance rule:** a new `CLAUDE.md` entry that a future reader would plausibly search for gets a one-line pointer added here in the same change. See [README](README.md#maintenance-rule).

---

| Date | Decision | CLAUDE.md section |
|---|---|---|
| 2026-07-24 | Repo cleanup: deleted dead docs/scripts, rewrote stale docs, closed a dependency drift | §"Repo cleanup: deleted dead docs/scripts..." |
| 2026-07-24 | In-process MCP tool dispatch (fixed self-proxying "dead air" over real HTTPS) + a tool-call timeout | §"Root-caused session 'dead air'..." |
| 2026-07-21 | `voice_tones`/`tone_id` migration gap found and closed during a WIP review | §"Reviewed voice-config WIP commit..." |
| 2026-07-21 | Server-side call-supervisor WebSocket built to fix mute-after-tool-result | §"SIP call supervisor: production fix..." |
| 2026-07-21 | Cost-gated pricing (`include_price`) + multi-service/multi-staff chain bookings | §"Cost-gated pricing + multi-service/multi-staff bookings" |
| 2026-07-21 | Diagnosed: hosted MCP does not auto-speak tool results (prod blocker at the time) | §"Realtime + hosted MCP does NOT auto-speak tool results" |
| 2026-07-21 | `/mcp` needs a trailing slash — OpenAI doesn't follow the 307 | §"MCP server_url must carry a trailing slash" |
| 2026-07-18 | CI/CD moved to ephemeral Neon branches; AWS Lambda deploy path removed | §"CI/CD: ephemeral Neon branches, seed-data bug fix, Lambda removal" |
| 2026-07-17 | Added live tool-dispatch + security test coverage; found/fixed an FK-crash bug | §"Live tool-dispatch + security test coverage" |
| 2026-07-16 | Telephony provider: Telnyx → Twilio (Estonia mobile numbers) | §"Telephony provider: Telnyx → Twilio" |
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/decisions.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/decisions.md
git commit -m "docs: add decisions index page to knowledge base"
```

---

## Task 9: api/README.md

**Files:**
- Create: `docs/knowledge/api/README.md`

- [ ] **Step 1: Write the page**

```markdown
# API Reference

Contract documentation for `booking_engine/api/routes/*.py` — method, auth, and shape. Verified against the actual route handlers and `Depends()` chains, not inferred from naming.

> **Maintenance rule:** an endpoint added/removed/changed updates the matching file here in the same change. See [../README](../README.md#maintenance-rule).

---

## Response envelope

Most routes return `{"data": ...}` on success (see `voice.py::_wrap`, or a Pydantic `response_model` on the plain REST routes) and `{"error": "<code>", "message": "<human-readable>"}` on failure. A slot conflict on booking returns HTTP 409 with `{"error": "slot_taken", ...}`.

## Auth schemes — four distinct ones, don't mix them up

| Scheme | Header | Who | Routes |
|---|---|---|---|
| Control-plane bearer | `Authorization: Bearer <CONTROL_PLANE_SECRET>` | the `webapp` Control Plane | [Voice Control Plane](voice-control-plane.md) |
| Tool bearer | `Authorization: Bearer <OPENAI_TOOL_SECRET>` | OpenAI Realtime (tool calls + session events) | [Voice Tools](voice-tools.md), and the `/mcp` mount |
| Signature-verified webhook | `X-Twilio-Signature`, validated against `TWILIO_AUTH_TOKEN` | Twilio | [Telephony Webhooks](telephony-webhooks.md) (`voice_twiml.py` only — no-op if `TWILIO_AUTH_TOKEN` unset) |
| **None** | — | anyone who can reach the route | [Business API](business.md) (`shops`/`customers`/`services`/`availability`/`appointments`); `voice_openai.py`'s `/voice/openai/incoming` also has no *enforced* auth today — see [Telephony Webhooks](telephony-webhooks.md) |

Auth dependencies live in `booking_engine/api/deps.py` (`require_control_plane_token`, `require_tool_token`).

## Grouped pages

- **[Business API](business.md)** — plain CRUD REST: shops, staff, services, customers, availability, appointments.
- **[Telephony Webhooks](telephony-webhooks.md)** — the two inbound-call entrypoints (Twilio TwiML, OpenAI SIP accept).
- **[Voice Tools](voice-tools.md)** — the 12 OpenAI-callable tools, mounted via `/mcp` and dispatched in-process.
- **[Voice Control Plane](voice-control-plane.md)** — config, balance, heartbeat, telephony provisioning, calls/analytics — everything the webapp calls.
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/api/README.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/api/README.md
git commit -m "docs: add API overview page to knowledge base"
```

---

## Task 10: api/business.md

**Files:**
- Create: `docs/knowledge/api/business.md`

- [ ] **Step 1: Write the page**

```markdown
# Business API

Plain CRUD REST endpoints — the original pre-voice booking API. **No authentication on any route below** — this is a real, current gap, not a doc omission (verified: none of `shops.py`/`customers.py`/`services.py`/`availability.py`/`appointments.py` declare a `Depends()` auth check).

> **Maintenance rule:** an endpoint added/removed/changed here updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Method | Path | File | Notes |
|---|---|---|---|
| `GET` | `/api/v1/shops/{shop_id}` | `shops.py` | 404 via `ErrorResponse` model if not found |
| `GET` | `/api/v1/shops/{shop_id}/services` | `services.py` | |
| `GET` | `/api/v1/shops/{shop_id}/staff` | `services.py` | |
| `GET` | `/api/v1/shops/{shop_id}/staff/{staff_id}/services` | `services.py` | |
| `GET` | `/api/v1/shops/{shop_id}/customers` | `customers.py` | |
| `POST` | `/api/v1/shops/{shop_id}/customers` | `customers.py` | 201 on success |
| `GET` | `/api/v1/shops/{shop_id}/availability` | `availability.py` | see [Voice Agent Logic](../voice-agent-logic.md) for the underlying slot-search algorithm (`get_available_slots`/`get_available_slot_chains` in `booking_engine/db/queries.py`) |
| `POST` | `/api/v1/shops/{shop_id}/appointments` | `appointments.py` | 201, or 409 (`ErrorResponse`) on slot conflict |
| `GET` | `/api/v1/shops/{shop_id}/appointments` | `appointments.py` | |
| `PATCH` | `/api/v1/shops/{shop_id}/appointments/{appointment_id}/cancel` | `appointments.py` | 409 on conflict |
| `PATCH` | `/api/v1/shops/{shop_id}/appointments/{appointment_id}/reschedule` | `appointments.py` | 404 or 409 |

Exact request/response field types: the Pydantic models in `booking_engine/api/models.py`, or FastAPI's own generated schema at `/docs` (Swagger) / `/openapi.json` on a running instance — that's the live, always-current reference for shapes; this page is the narrative/auth layer on top of it.
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/api/business.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/api/business.md
git commit -m "docs: add business API page to knowledge base"
```

---

## Task 11: api/telephony-webhooks.md

**Files:**
- Create: `docs/knowledge/api/telephony-webhooks.md`

- [ ] **Step 1: Write the page**

```markdown
# Telephony Webhooks

The two entrypoints that start a call — see [Architecture → Call flow](../architecture.md#call-flow) for how they chain together.

> **Maintenance rule:** a change to either webhook's routing/auth updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

## `POST /api/v1/voice/twiml/incoming`

**File:** `voice_twiml.py`. Twilio calls this when a call reaches one of this repo's provisioned numbers. Looks up the shop by dialed number, returns TwiML that `<Dial><Sip>`s into OpenAI with the shop id attached as a custom SIP header.

**Auth:** `X-Twilio-Signature`, validated via `RequestValidator(TWILIO_AUTH_TOKEN)`. **No-op (always valid) if `TWILIO_AUTH_TOKEN` is unset.**

## `POST /voice/openai/incoming`

**File:** `voice_openai.py`. Note: **not** under `/api/v1` — mounted at the top level in `app.py`. OpenAI fires `realtime.call.incoming` here. Reads the shop id back out of the SIP headers (or `SIP_TEST_FALLBACK_SHOP_ID` on QA for a raw softphone test with no Twilio in the path), resolves the caller, assembles the session prompt, and calls `accept_sip_call()`.

**Auth:** signature verified only when `OPENAI_WEBHOOK_SECRET` is set (see the `ponytail:` comment at the top of the file) — currently unwired, so this endpoint accepts unsigned requests. Known gap, not yet closed.
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/api/telephony-webhooks.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/api/telephony-webhooks.md
git commit -m "docs: add telephony webhooks API page to knowledge base"
```

---

## Task 12: api/voice-tools.md

**Files:**
- Create: `docs/knowledge/api/voice-tools.md`

- [ ] **Step 1: Write the page**

```markdown
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
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/api/voice-tools.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/api/voice-tools.md
git commit -m "docs: add voice tools API page to knowledge base"
```

---

## Task 13: api/voice-control-plane.md

**Files:**
- Create: `docs/knowledge/api/voice-control-plane.md`

- [ ] **Step 1: Write the page**

```markdown
# Voice Control Plane API

Endpoints the `webapp` Control Plane calls to manage voice config, telephony numbers, balance, and call history. All require `Authorization: Bearer <CONTROL_PLANE_SECRET>` (`require_control_plane_token`).

> **Maintenance rule:** an endpoint added/removed/changed here updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Method | Path | File | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/voice/config/tones` | `voice_config.py` | list the 8 preset tones (+ any shop-authored ones) |
| `GET` | `/api/v1/voice/config/{shop_id}` | `voice_config.py` | read Layer 1 config |
| `PATCH` | `/api/v1/voice/config/{shop_id}` | `voice_config.py` | update any subset of `_PATCHABLE_FIELDS` (enabled, display_name, greetings, voice_preset, tone_id, business_hours, answer_mode, overflow_ring_count, services_to_mention, retention_days, manual_fallback_number, auto-topup settings) |
| `GET` | `/api/v1/voice/balance/{shop_id}` | `voice_balance.py` | token balance + warning tier |
| `POST` | `/api/v1/voice/heartbeat/forwarding` | `voice_heartbeat.py` | Path-2 (forward) silent-line heartbeat — meant to be hit by a scheduled job, not the webapp UI directly |
| `GET` | `/api/v1/voice/numbers/search` | `voice_telephony.py` | search available Twilio numbers |
| `POST` | `/api/v1/voice/numbers/provision` | `voice_telephony.py` | purchase + bind a number to a shop |
| `GET` | `/api/v1/voice/numbers/{shop_id}/setup-instructions` | `voice_telephony.py` | forwarding setup copy for the shop's existing carrier |
| `GET` | `/api/v1/voice/numbers/{shop_id}` | `voice_telephony.py` | current telephony config for a shop |
| `GET` | `/api/v1/shops/{shop_id}/voice/calls` | `voice.py` | paginated call list |
| `GET` | `/api/v1/shops/{shop_id}/voice/calls/{call_id}` | `voice.py` | full call detail: summary + transcript + events |
| `PATCH` | `/api/v1/shops/{shop_id}/voice/calls/{call_id}/link-customer` | `voice.py` | manually link an unmatched call to a customer |
| `GET` | `/api/v1/shops/{shop_id}/voice/analytics` | `voice.py` | volume/outcome/demand aggregates |
| `GET` | `/api/v1/voice/memos/{shop_id}` | `voice_memos.py` | list callback memos (from `escalate_to_merchant`) |
| `GET` | `/api/v1/voice/memos/{shop_id}/count` | `voice_memos.py` | unread count, for an Action Center badge |
| `PATCH` | `/api/v1/voice/memos/{memo_id}` | `voice_memos.py` | mark a memo read/actioned |

Customer-match enum (`voice.py`'s call responses): `existing \| created \| unmatched \| ambiguous`.
```

- [ ] **Step 2: Verify**

```bash
grep -q "Maintenance rule" docs/knowledge/api/voice-control-plane.md && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/api/voice-control-plane.md
git commit -m "docs: add voice control plane API page to knowledge base"
```

---

## Task 14: Consolidate — delete old docs, add CLAUDE.md pointer, finalize sidebar, verify the whole site

**Files:**
- Delete: `docs/DEPLOY_VOICE_AGENT.md`
- Delete: `docs/INTEGRATION_GUIDE.md`
- Modify: `CLAUDE.md`
- Modify: `docs/knowledge/_sidebar.md` (already correct from Task 1 — this step re-verifies it, not a rewrite)

- [ ] **Step 1: Confirm every page from Task 1's sidebar now exists**

```bash
for f in docs/knowledge/README.md docs/knowledge/architecture.md docs/knowledge/database.md \
         docs/knowledge/voice-agent-logic.md docs/knowledge/providers.md docs/knowledge/operations.md \
         docs/knowledge/decisions.md docs/knowledge/api/README.md docs/knowledge/api/business.md \
         docs/knowledge/api/telephony-webhooks.md docs/knowledge/api/voice-tools.md \
         docs/knowledge/api/voice-control-plane.md; do
  test -f "$f" && echo "OK $f" || echo "MISSING $f"
done
```
Expected: every line `OK ...`, no `MISSING` lines.

- [ ] **Step 2: Delete the two superseded docs (content now lives in the pages above)**

```bash
git rm docs/DEPLOY_VOICE_AGENT.md docs/INTEGRATION_GUIDE.md
```

- [ ] **Step 3: Add the CLAUDE.md pointer block**

Insert this block into `CLAUDE.md` immediately after the existing intro paragraph (after "...stays as the record of what was true and decided at the time.") and before the `---` divider that precedes the first dated entry:

```markdown

## Documentation

Human-oriented docs (architecture, database, voice-agent logic, providers,
operations, API reference) live in `docs/knowledge/` — a Docsify site, `npx
--yes serve docs/knowledge` to browse. This file stays what it already is:
the append-only decision/incident history. `docs/knowledge/decisions.md` is
a short index into it, kept in sync by hand.

**Any change that adds, removes, or changes a REST/voice-tool endpoint, a
database table, a provider integration, or a safety/authz/booking-constraint
rule updates the matching `docs/knowledge/*.md` file in the same change** —
not as a follow-up. See `docs/knowledge/README.md` for the full rule.
```

- [ ] **Step 4: Verify the CLAUDE.md edit**

```bash
grep -q "docs/knowledge/README.md" CLAUDE.md && echo OK
head -20 CLAUDE.md
```
Expected: `OK`, and the printed head shows the new block between the intro paragraph and the first `---`.

- [ ] **Step 5: Verify no tracked file still references the two deleted docs**

```bash
git grep -n "DEPLOY_VOICE_AGENT.md\|INTEGRATION_GUIDE.md" -- . || echo "CLEAN"
```
Expected: `CLEAN` (or only matches inside `docs/knowledge/*` pages that intentionally describe their own consolidation history — review any hit before proceeding).

- [ ] **Step 6: Smoke-test the site actually serves and every sidebar link resolves**

```bash
npx --yes serve docs/knowledge -l 4001 &
SERVER_PID=$!
sleep 2
curl -s -o /dev/null -w 'index: %{http_code}\n' http://localhost:4001/
curl -s -o /dev/null -w 'sidebar: %{http_code}\n' http://localhost:4001/_sidebar.md
curl -s -o /dev/null -w 'architecture: %{http_code}\n' http://localhost:4001/architecture.md
curl -s -o /dev/null -w 'api/voice-tools: %{http_code}\n' http://localhost:4001/api/voice-tools.md
kill $SERVER_PID
```
Expected: every line `200`.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: consolidate DEPLOY_VOICE_AGENT.md/INTEGRATION_GUIDE.md into knowledge base, point CLAUDE.md at it"
```

(The `git rm` from Step 2 is already staged; this commit picks it up alongside the `CLAUDE.md` change. Do not `git add -A` — stage only what this task touched, to avoid bundling in unrelated pending changes from other work.)

---

## Plan self-review notes (for the executor, not a task)

- Every page above carries the exact `> **Maintenance rule:**` marker string the verification greps check for.
- `decisions.md` intentionally does not duplicate `CLAUDE.md`'s narrative — one line + a section pointer per entry, per the approved design.
- `api/business.md`'s "no auth" note and `voice_openai.py`'s "signature optional" note are both real, current facts (verified via `grep`/`Read` during planning, not assumed) — worth a human's attention but out of scope to fix as part of a docs task.
- If a future task adds/removes a route, table, or provider, the maintenance-rule convention (not this plan) is what's supposed to catch it — this plan only builds the initial site.
