# Customer Agents Common Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One set of customer-facing booking agents, in marketing-engine's `runTool` architecture, used by WhatsApp now and by Voice next. The webapp is the only slot-search engine and the only booking-write engine. Every salon-local time uses `shops.timezone`.

**Architecture:** marketing-engine gains `src/lib/customer-agents/`: one `CUSTOMER_TOOL_SCHEMAS` plus a per-session dispatch that runs through `runTool` (with `recordAgentCall`). There are two surfaces over it. `whatsapp` is the existing in-process turn loop. `voice` is an MCP endpoint that OpenAI Realtime calls directly. Reads are in-process SQL, the same pattern as `business-advisor/grounding.ts`. Slot search is the webapp `/availability` route. Writes go to new engine-authenticated webapp routes that call the existing repository functions (`createAppointment` with `source: 'whatsapp' | 'voice_agent'`, which already enforces overlap, absence, shop hours and shift). Session-owned writes (escalation memo, outcome, session↔customer link) stay in voice-booking, which owns `voice_agent.*`.

**Tech Stack:** TypeScript/Express/postgres.js/Anthropic SDK (marketing-engine), Next.js 15 route handlers/postgres.js (webapp), Python/FastAPI/asyncpg (voice-booking).

**Decided by the owner (2026-09-28), not open for re-litigation:** the `runTool` architecture is mandatory; there is one slot engine (webapp `availability_search`), with only a thin window-filtering wrapper on top; the naming table below; no "BLOCCO RUOLO / PRIVACY / AMBITO" prose in prompts, because the allow-list is the perimeter; no AI self-introduction (risk under AI Act art. 50 accepted, recorded in voice-booking AGENTS.md); booking confirmation is composed in code; the customer phone always comes from the session, never from the model; the WhatsApp/Meta auth/onboarding path is frozen and must not be touched.

**Global rules for every task:**
- Do not push. Commit locally on the repo's current branch (`QA`, or `dev_error_tracking` in marketing-engine, which tracks `QA`). The coordinator pushes all three repos together, because every QA push resets the QA database.
- webapp: another process commits in that working tree. `git add` your files by name, never `git add -A` or `git add .`.
- Verification commands:
  - voice-booking: `python -m pytest tests/ --ignore=tests/live_db --ignore=tests/live_twilio -q`
  - marketing-engine (`cd kairo-market-intel`): `npx tsc --noEmit -p . && npx jest tests/whatsapp tests/customer-agents`, and the full `npx jest` before the final commit
  - webapp: `npm run verify`
- Any change to an endpoint, table, provider integration or safety rule updates the matching `docs/knowledge/*.md` in the same repo, in the same commit (voice-booking rule; the webapp and marketing-engine have equivalent docs under `docs/`).

---

## Naming table (approved)

Reads are domain first (`<domain>_<aspect>`). Writes are verb first (`<verb>_<object>`). Use "appointment" everywhere: never "booking" or "visit".

| Old (voice-booking `/voice/tools/*`) | New agent name | Backed by |
|---|---|---|
| `lookup_customer` | `customers_identify` | in-process SQL: `customers` by session phone |
| `create_customer_from_call` | `create_customer` | webapp `POST /api/v1/hair-salon/agent/customers` |
| `update_customer_from_call` | `update_customer` | webapp `PATCH /api/v1/hair-salon/agent/customers/{id}` |
| `get_services` + `get_staff_for_service` | `services_catalog` | in-process SQL: the existing `grounding.servicesCatalog`, customer variant |
| `check_availability` | `availability_search` | webapp `/availability` + window wrapper |
| `create_booking` | `create_appointment` | webapp `POST /api/v1/hair-salon/agent/appointments` |
| `get_booking` | `appointments_upcoming` | in-process SQL, scoped to the session phone |
| `modify_booking` | `reschedule_appointment` | webapp `POST /api/v1/hair-salon/agent/appointments/{id}/reschedule` |
| `cancel_booking` | `cancel_appointment` | webapp `POST /api/v1/hair-salon/agent/appointments/{id}/cancel` |
| `escalate_to_merchant` | `escalate_to_owner` | voice-booking `POST /sessions/{call_id}/escalation` |
| `mark_outcome` | `set_conversation_outcome` | voice-booking `POST /sessions/{call_id}/outcome` |
| business-advisor `reschedule_visit` | `reschedule_appointment` | owner-chat write proposal (rename only) |

---

## File map

### marketing-engine (`kairo-market-intel/`)

Create:
- `src/lib/customer-agents/session.ts`: `CustomerSession` type and `loadSession(shopId, callId, channel)`. Reads the shop name and timezone from `business_app_core.shops` and the phone from `voice_agent.calls.caller_number`, keyed on `id = callId AND shop_id = shopId`.
- `src/lib/customer-agents/time.ts`: `dateContext(nowIso, tz)`, moved out of `whatsapp/agent.ts` and taking a timezone; `zonedNow(tz)`; `localYmd(date, tz)`.
- `src/lib/customer-agents/schemas.ts`: `CUSTOMER_TOOL_SCHEMAS`, `CUSTOMER_TOOL_NAMES`.
- `src/lib/customer-agents/reads.ts`: `customersIdentify`, `servicesCatalogForCustomer`, `appointmentsUpcoming` (in-process SQL).
- `src/lib/customer-agents/availability.ts`: the window wrapper around `searchAvailability`.
- `src/lib/customer-agents/writes.ts`: webapp and voice-booking HTTP calls for the write agents.
- `src/lib/customer-agents/dispatch.ts`: `customerDispatch(session)` → `Record<name, ToolFn>`.
- `src/lib/customer-agents/confirmation.ts`: `confirmationText(appointment, session)`, deterministic.
- `src/lib/customer-agents/prompt.ts`: `customerRules(channel)` (shared rules) and `whatsappSystem(session, nowIso)`.
- `src/routes/customer-agents-voice.ts` (Phase C): MCP endpoint plus the voice instructions endpoint.
- `tests/customer-agents/*.test.ts`

Modify:
- `src/lib/business-advisor/tools.ts`:
  - export `executeCustomerTool`
  - `runTool` rethrows `TransportError`
  - `agent_call.ok=false` when a result is `{ok:false}` or `{error}`
  - rename `reschedule_visit` → `reschedule_appointment`
- `src/lib/whatsapp/agent.ts`: `runTurn` uses the common layer, and the deterministic confirmation ends the turn. Delete the catalogue/intake/grounding blocks.
- `src/lib/whatsapp/tools.ts`: delete, because every export moves to `customer-agents/`. Keep only `TransportError`, relocated to `customer-agents/writes.ts`.
- `src/routes/whatsapp-agent.ts`: the request shrinks to `{shop_id, call_id, messages, now}`.
- `src/lib/webapp/client.ts`: `searchAvailability` passes `now_iso`; add `agentWrite(path, body)`.
- `src/lib/i18n/prompts/{it,en,es}.ts` and the webapp `apply-action` dispatch: `reschedule_visit` → `reschedule_appointment`.

### webapp

Create:
- `src/app/api/v1/hair-salon/agent/_lib/agentAuth.ts`: `requireAgent(req, body)` → `{shopId}` or 401/400. Uses `isEngineCall` plus `shop_id`, and verifies the shop exists.
- `src/app/api/v1/hair-salon/agent/customers/route.ts` (POST)
- `src/app/api/v1/hair-salon/agent/customers/[id]/route.ts` (PATCH)
- `src/app/api/v1/hair-salon/agent/appointments/route.ts` (POST)
- `src/app/api/v1/hair-salon/agent/appointments/[id]/reschedule/route.ts` (POST)
- `src/app/api/v1/hair-salon/agent/appointments/[id]/cancel/route.ts` (POST)
- `src/lib/agent/caller.ts`: `sameCaller(customerPhoneNormalized, callerPhone)`, digits-only comparison.
- tests next to each route (`route.test.ts`) and `src/lib/agent/caller.test.ts`

Modify:
- `src/lib/db/repositories/customers.repo.ts`: add `createAgentCustomer`.
- `src/lib/db/repositories/appointments.repo.ts`:
  - `rescheduleAppointment` enforces overlap, absence, hours and shift for non-manual sources
  - add `cancelAppointmentByAgent`
  - replace hardcoded Rome with the shop timezone in the functions touched here
- `src/app/api/v1/hair-salon/availability/route.ts`:
  - use `getShopTimezone` + `zonedToUtc` instead of `romeToUtc`
  - drop slots before `now_iso` (+ lead time) when the date is today
- `src/lib/db/repositories/availability.repo.ts`: replace `AT TIME ZONE 'Europe/Rome'` with the shop timezone (a parameter)
- `src/app/api/v1/action-center/apply-action/dispatch.ts`, `src/types/agent-actions.ts`: `reschedule_visit` → `reschedule_appointment`
- `docs/knowledge/*` (engine-callable routes section)

### voice-booking

Create:
- `booking_engine/api/routes/sessions.py`: `POST /sessions/{call_id}/escalation`, `POST /sessions/{call_id}/outcome`, `POST /sessions/{call_id}/customer` (auth: `require_tool_token`; the session row must match `X-Shop-Id`)
- `tests/booking_engine/test_sessions_routes.py`

Modify:
- `booking_engine/services/messaging/wa_agent.py` + `booking_engine/clients/marketing_agent.py`: send `{shop_id, call_id, messages, now}` only
- `booking_engine/db/whatsapp_thread_queries.py`: the thread's `customer_id` falls back to the session's `calls.customer_id`
- `booking_engine/db/whatsapp_queries.py:sent_today`: shop-local midnight
- Phase C:
  - `booking_engine/services/voice_openai.py`: MCP `server_url` → marketing-engine; instructions fetched from marketing-engine
  - `booking_engine/services/prompt_assembler.py`: slimmed to voice/persona config only

Delete (Phase A, the old slot engine; Phase C, the voice tool layer):
- Phase A:
  - `db/voice_tool_queries.py::find_availability, _closest, _CANDIDATES, any_staff_could_ever_serve`
  - `db/queries.py::get_available_slots, get_available_slot_chains` and helpers `_eligible_staff_for_leg, _staff_day_windows, _overlaps_existing, _iter_leg0_candidates, _try_extend_chain`
  - `api/routes/availability.py` (+ mount in `api/app.py`, `AvailabilityResponse` in `api/models.py`)
  - the `check_availability` route + `CheckAvailabilityIn/AvailabilityLeg/AvailabilityChain/BookingServiceIn`
  - their tests: `test_find_availability.py`, `test_routes/test_availability.py`, `test_availability_helper.py`, the chain tests in `test_queries.py`, `live_db/test_availability.py`, the availability part of `integration/test_booking_flow.py`
- Phase C:
  - `api/routes/voice_tools_{booking,catalog,identity,lifecycle}.py`
  - `mcp_server.py`, `services/mcp_tools.py`
  - the tool schemas/allowlist/`ATTESA_TOOLS` in `services/safety_layer.py`
  - `db/queries.py::create_appointment_chain` and `reschedule_appointment`, if no caller is left (check `api/routes/appointments.py` first)
  - `db/voice_tool_queries.py` booking/customer writes
  - their tests
  - `docs/knowledge/api/voice-tools.md` rewritten as a pointer to the marketing-engine agents

---

# PHASE A — WhatsApp on the common layer, one engine

## A1 (webapp) — Engine-authenticated agent write routes

### Task A1.1: caller matching helper

**Files:** Create `src/lib/agent/caller.ts`, `src/lib/agent/caller.test.ts`

- [ ] **Step 1: Failing test**

```ts
import { describe, expect, it } from 'vitest'
import { sameCaller } from './caller'

describe('sameCaller', () => {
  it('matches on digits only, with or without + and spaces', () => {
    expect(sameCaller('393931605283', '+39 393 160 5283')).toBe(true)
  })
  it('never matches an empty or missing number', () => {
    expect(sameCaller(null, '+393931605283')).toBe(false)
    expect(sameCaller('393931605283', '')).toBe(false)
  })
  it('does not match a different number', () => {
    expect(sameCaller('393931605283', '+393496140811')).toBe(false)
  })
})
```

- [ ] **Step 2:** `npx vitest run src/lib/agent/caller.test.ts`. Expect FAIL (module not found).
- [ ] **Step 3: Implement**

```ts
/** The authorization rule every customer-agent write rests on: a customer may
 *  only act on records carrying their own number. The number is the session's
 *  (Meta-verified on WhatsApp, caller ID on voice), never the model's. */
const digits = (s: string | null | undefined) => (s ?? '').replace(/\D/g, '')

export function sameCaller(customerPhone: string | null | undefined, callerPhone: string | null | undefined): boolean {
  const a = digits(customerPhone)
  const b = digits(callerPhone)
  return a.length >= 6 && a === b
}
```

- [ ] **Step 4:** Re-run the test. Expect PASS.
- [ ] **Step 5:** `git add src/lib/agent/caller.ts src/lib/agent/caller.test.ts && git commit -m "feat(agent): caller-number matching for customer-agent writes"`

### Task A1.2: `requireAgent` auth helper

**Files:** Create `src/app/api/v1/hair-salon/agent/_lib/agentAuth.ts`, `agentAuth.test.ts`

Contract:
- `requireAgent(req: NextRequest, body: { shop_id?: string }): Promise<string>` returns the shopId.
- It throws a `Response` (the same pattern as `getShopId`, whose callers `catch (err) { if (err instanceof Response) return err }`):
  - 401 if `!isEngineCall(req)`
  - 400 if `shop_id` is missing or not a UUID
  - 404 if no `business_app_core.shops` row has that id

- [ ] **Step 1:** Write tests for the three refusals and the happy path. Mock `@/lib/db` `sql` the way the existing route tests do (copy the mocking pattern from `src/app/api/v1/hair-salon/availability/route.test.ts` if it exists, otherwise from the nearest `route.test.ts` that mocks `sql`).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement with `isEngineCall` from `../../_lib/engineCaller`, plus `SELECT 1 FROM business_app_core.shops WHERE id = ${shopId}`.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit: `feat(agent): engine auth for customer-agent routes`.

### Task A1.3: `createAgentCustomer` + `POST /agent/customers`

**Files:**
- Modify `src/lib/db/repositories/customers.repo.ts`
- Create `src/app/api/v1/hair-salon/agent/customers/route.ts` + `route.test.ts`

Repository contract:

```ts
/** A customer created by an assistant (WhatsApp or voice). `verified=false`
 *  is what puts it in Clienti → «Creati dall'assistente» until the owner opens
 *  it. Idempotent on the number: the same caller asking twice gets the same
 *  row, never a duplicate. */
export async function createAgentCustomer(
  shopId: string,
  input: { full_name: string; phone: string; source: 'whatsapp' | 'voice_agent'; created_by_call_id?: string | null },
): Promise<{ customer: DbCustomer; created: boolean }>
```

Implementation:
1. `SELECT * FROM business_app_core.customers WHERE shop_id=$1 AND regexp_replace(coalesce(phone,''),'\D','','g') = regexp_replace($2,'\D','','g') AND lower(full_name)=lower($3) LIMIT 1`. If found, return `{customer, created:false}`.
2. Otherwise `INSERT (shop_id, full_name, phone, source, verified, phone_verified, created_by_call_id, tags) VALUES (…, false, $source='whatsapp', $call, ARRAY[$tag])`, where tag is `'nuovo da WhatsApp'` for `whatsapp` and `'nuovo da chiamata vocale'` for `voice_agent`.
   - `phone_verified` is true for `whatsapp`, because Meta verified the number, and false for `voice_agent`.
   - Check the actual column list against `00_baseline.sql` `CREATE TABLE business_app_core.customers` before writing the INSERT. Only use columns that exist.

Route `POST`, body `{shop_id, full_name, phone, source, created_by_call_id?}`:
- 422 if `full_name` is blank or has fewer than 2 characters, or `phone` has fewer than 6 digits, or `source` is not in `('whatsapp','voice_agent')`
- 200 `{data: {customer_id, full_name, created}}`

Tests:
- a new customer is created with `verified=false` and the tag
- a second identical call returns `created:false` and the same id
- 401 without the secret
- 422 on a blank name

- [ ] Steps 1–5 as TDD. Commit: `feat(agent): assistant-created customers endpoint`.

### Task A1.4: `PATCH /agent/customers/{id}`

Body `{shop_id, caller_phone, full_name?, email?}`. Only these two fields; notes and tags are the owner's.
- 404 if the customer is not in the shop
- **403 `caller_mismatch`** if `!sameCaller(customer.phone, caller_phone)`
- Uses the existing `updateCustomer(shopId, id, patch)`; check its signature at `customers.repo.ts:170`
- Returns `{data:{customer_id}}`

Tests: mismatch → 403; happy path.

- [ ] TDD steps. Commit: `feat(agent): customer self-update endpoint`.

### Task A1.5: `POST /agent/appointments`

Body:

```ts
{
  shop_id: string
  customer_id: string
  caller_phone: string
  source: 'whatsapp' | 'voice_agent'
  assignments: { service_id: string; staff_id: string; start_time: string /* ISO with offset or Z */ }[]
  notes?: string
}
```

Rules, in order:
1. The customer is in the shop (404), and `sameCaller(customer.phone, caller_phone)` holds (403 `caller_mismatch`). A customer created by `create_customer` in this same session carries the session phone, so this passes.
2. Every `assignments[].start_time` parses and is in the future (422 `slot_in_past`).
3. Call `createAppointment(shopId, { customer_id, staff_id: assignments[0].staff_id, service_ids: assignments.map(a=>a.service_id), start_time: assignments[0].start_time, assignments: assignments.map(a => ({ serviceId: a.service_id, staffId: a.staff_id, start: a.start_time })), source, notes })`.
   - **Check the real `AppointmentAssignment` field names in `src/types/hair-salon.ts`** and map to them exactly. The line above shows intent, not field names.
4. Map repository errors the same way `apply-action/dispatch.ts:236-239` does:
   - overlap → 409 `slot_taken`
   - absent → 409 `staff_absent`
   - shop-closed or off-shift → 409 `outside_hours`
   - anything else → 500
5. Return 200 `{data: {appointment_id, start_time, end_time, services: [{service_id, service_name, staff_id, staff_name, start_time, end_time}]}}`, read back with `getAppointmentById`. The deterministic confirmation text is built from this, so the names must be present.

Concurrency: wrap step 3 in `sql.begin` with `SELECT pg_advisory_xact_lock(hashtext(${staffId}))` for each distinct staff id, sorted, so two agents cannot both pass the overlap check. If `createAppointment` already opens its own transaction (`sql.begin` at `appointments.repo.ts:626`), add an optional `tx` parameter or take the lock inside it. Pick the smallest change and note it in the commit message.

Tests:
- a caller mismatch is refused
- a past slot is refused
- an overlap maps to 409 `slot_taken`
- the happy path returns service and staff names
- `source` is passed through, not `'manual'`

- [ ] TDD steps. Commit: `feat(agent): assistant booking endpoint on the agenda's own write path`.

### Task A1.6: reschedule + cancel

- `rescheduleAppointment` (`appointments.repo.ts:819`): first read it fully.
  - If it does not run `assertNoOverlap` / `assertNotAbsent` for the moved blocks, add them, plus `assertShopOpen` / `assertStaffOnShift`, **only when a new `source` parameter is not `'manual'`**. The agenda's behaviour stays unchanged.
  - Replace `AT TIME ZONE 'Europe/Rome'` in this function with the shop timezone from `getShopTimezone(shopId)`.
- `POST /agent/appointments/{id}/reschedule`, body `{shop_id, caller_phone, source, assignments[]}`:
  - The appointment must be in the shop, its customer must satisfy `sameCaller`, its status must not be `cancelled`, `completed` or `no_show`, and the start must be in the future.
  - The new date is derived from `assignments[0].start_time` in the shop timezone.
  - Same error mapping as A1.5.
- `POST /agent/appointments/{id}/cancel`, body `{shop_id, caller_phone}`:
  - Same ownership, status and future checks.
  - Then `updateAppointmentStatus(id, 'cancelled')`. Check that `'cancelled'` is the actual enum value in `AppointmentStatus`.
  - Returns `{data:{appointment_id, status:'cancelled'}}`.

Tests per route: mismatch 403, past 409 `appointment_in_past`, happy path.

- [ ] TDD steps. Commit: `feat(agent): assistant reschedule/cancel with ownership checks`.

### Task A1.7: availability route — shop timezone and no past slots

**Files:** `src/app/api/v1/hair-salon/availability/route.ts`, `src/lib/db/repositories/availability.repo.ts`, tests

- `getAvailabilityDay(shopId, date, serviceIds, excludeId, tz)`: replace every `'Europe/Rome'` literal (lines ~76–105) with a `${tz}` parameter.
- The route resolves `tz = await getShopTimezone(shopId)` and converts candidate minutes with `zonedToUtc(date, hhmm, tz)` instead of `romeToUtc`.
- New optional body field `now_iso`. When `date` equals today in `tz` (compute with `Intl.DateTimeFormat('en-CA',{timeZone: tz})`), drop candidates whose first block starts before `now + 30 min`.
  - With no `now_iso`, use the server `Date.now()`. The browser panel does not send it and gets the same filter.
- Tests:
  - a candidate earlier today is dropped, a later one is kept
  - a shop in `Europe/London` gets `start_iso` shifted by an hour compared with Rome for the same wall-clock slot

- [ ] TDD steps. Commit: `fix(availability): shop timezone and no past slots`.

### Task A1.8: rename `reschedule_visit` → `reschedule_appointment`

- `src/types/agent-actions.ts`, `src/app/api/v1/action-center/apply-action/dispatch.ts`, and any UI that switches on the type (`grep -rn reschedule_visit src`).
- Keep accepting the old name for one release as an alias in the zod schema, because proposals stored in open chats may still carry it.
- Commit: `refactor(agent): reschedule_visit → reschedule_appointment`.

### Task A1.9: docs + verify

- Document the five `/agent/*` routes (auth, body, errors) in the webapp's `docs/knowledge` providers/API page, next to `/availability`.
- `npm run verify` must exit 0.
- Commit: `docs(agent): engine-callable customer-agent routes`.

---

## A2 (marketing-engine) — The common layer and the WhatsApp surface

### Task A2.1: `TransportError` and `runTool` semantics

**Files:** `src/lib/business-advisor/tools.ts`, `tests/customer-agents/runtool.test.ts`

- Export `class TransportError extends Error` from `src/lib/customer-agents/writes.ts`, and import it in `tools.ts`.
- In `runTool`:
  - if `fn` throws a `TransportError`, record `agent_call` with ok=false and **rethrow**
  - every other throw keeps becoming `{error}`
  - a returned value `v` with `v?.ok === false || typeof v?.error === 'string'` is recorded with `ok:false, error`
- Export:

```ts
export async function executeCustomerTool(
  dispatch: Record<string, ToolFn>, shopId: string, name: string,
  args: Record<string, unknown>, ctx: AgentCtx,
): Promise<unknown> {
  return runTool(dispatch, shopId, name, args, ctx)
}
```

- Tests:
  - a thrown `TransportError` propagates
  - a thrown `Error` becomes `{error}`
  - `{ok:false}` is recorded as not-ok: mock `recordAgentCall` and assert its argument
- Commit: `feat(agents): runTool keeps transport failures distinct; executeCustomerTool`.

### Task A2.2: session + time

**Files:** `src/lib/customer-agents/session.ts`, `time.ts`, tests

```ts
export type Channel = 'whatsapp' | 'voice'
export interface CustomerSession {
  shopId: string; callId: string; channel: Channel
  shopName: string; timezone: string
  /** voice_agent.calls.caller_number — Meta-verified on WhatsApp, caller ID on voice. */
  phone: string
}
export async function loadSession(shopId: string, callId: string, channel: Channel): Promise<CustomerSession | null>
```

- SQL:

```sql
SELECT s.name, s.timezone, c.caller_number
FROM voice_agent.calls c
JOIN business_app_core.shops s ON s.id = c.shop_id
WHERE c.id = $callId AND c.shop_id = $shopId
```

- Returns null when there is no row. An invalid timezone falls back to `'Europe/Rome'`, validated with `Intl.DateTimeFormat(undefined,{timeZone})` in a try.
- `time.ts`: move `dateContext` from `whatsapp/agent.ts` to `dateContext(nowIso: string, tz: string)`.
  - Step through days from **local noon** (`zonedNoon(ymd, tz)`) instead of +24h, which removes the DST caveat.
  - Keep the existing test expectations: `'lunedì 28 settembre 2026, ore 13:39'` and `'giovedì 1 ottobre 2026 = 2026-10-01'` for Rome. Add a `Europe/London` case that shows `12:39`.
  - The instruction line becomes: `Per le date e gli orari usa sempre l'ora del salone (fuso ${tz}).`
- Commit: `feat(customer-agents): session context from the call row; tz-aware date context`.

### Task A2.3: schemas

**File:** `src/lib/customer-agents/schemas.ts`

Exactly these tools (Italian descriptions, because the whole agent is Italian-first):

```ts
export const CUSTOMER_TOOL_SCHEMAS: Anthropic.Tool[] = [
  { name: 'customers_identify',
    description: "Chi sta scrivendo: cerca i clienti del salone con il numero di questa conversazione. Chiamalo all'inizio. 0 risultati = cliente nuovo; 1 = usalo e chiamalo per nome; più di 1 = chiedi il nome per capire chi è.",
    input_schema: { type: 'object', properties: {} } },
  { name: 'services_catalog',
    description: "Servizi prenotabili: id, nome, durata, operatori abilitati e note del titolare. Il prezzo solo con include_price=true, da usare SOLO se il cliente chiede quanto costa.",
    input_schema: { type: 'object', properties: {
      name: { type: 'string', description: 'Filtro parziale sul nome del servizio' },
      include_price: { type: 'boolean' } } } },
  { name: 'availability_search',
    description: "Orari realmente prenotabili (stesso motore dell'agenda: orari del salone, turni, assenze, tempi di posa). Passa i servizi nell'ordine in cui vanno eseguiti. Restituisce al massimo 5 proposte, le più vicine a quanto chiesto. Se il salone è chiuso nella finestra chiesta, restituisce gli orari di apertura.",
    input_schema: { type: 'object', required: ['service_ids', 'date_from'], properties: {
      service_ids: { type: 'array', items: { type: 'string' }, minItems: 1 },
      date_from: { type: 'string', description: 'YYYY-MM-DD, primo giorno utile' },
      date_to: { type: 'string', description: 'YYYY-MM-DD, ultimo giorno (max 7 giorni dopo date_from). Ometti per un giorno solo.' },
      time_from: { type: 'string', description: 'HH:MM ora del salone, inizio finestra (es. "sera" = 17:00)' },
      time_to: { type: 'string', description: 'HH:MM ora del salone, fine finestra' },
      near_time: { type: 'string', description: 'HH:MM ora del salone preferita; ordina per vicinanza' },
      staff_id: { type: 'string', description: 'Solo se il cliente ha chiesto un operatore' } } } },
  { name: 'create_customer',
    description: 'Crea la scheda di un cliente nuovo con il numero di questa conversazione. Chiedi prima nome e cognome.',
    input_schema: { type: 'object', required: ['first_name', 'last_name'], properties: {
      first_name: { type: 'string' }, last_name: { type: 'string' } } } },
  { name: 'update_customer',
    description: 'Aggiorna nome o email del cliente di questa conversazione.',
    input_schema: { type: 'object', required: ['customer_id'], properties: {
      customer_id: { type: 'string' }, full_name: { type: 'string' }, email: { type: 'string' } } } },
  { name: 'create_appointment',
    description: "Prenota una proposta restituita da availability_search, copiandone esattamente proposal_id. Solo dopo che il cliente ha confermato servizio, giorno, ora e operatore. La conferma al cliente la invia il sistema.",
    input_schema: { type: 'object', required: ['customer_id', 'proposal_id'], properties: {
      customer_id: { type: 'string' }, proposal_id: { type: 'string' } } } },
  { name: 'appointments_upcoming',
    description: 'Prossimi appuntamenti del cliente di questa conversazione.',
    input_schema: { type: 'object', properties: {} } },
  { name: 'reschedule_appointment',
    description: 'Sposta un appuntamento del cliente su una proposta di availability_search (proposal_id).',
    input_schema: { type: 'object', required: ['appointment_id', 'proposal_id'], properties: {
      appointment_id: { type: 'string' }, proposal_id: { type: 'string' } } } },
  { name: 'cancel_appointment',
    description: 'Annulla un appuntamento del cliente, dopo sua conferma esplicita.',
    input_schema: { type: 'object', required: ['appointment_id'], properties: { appointment_id: { type: 'string' } } } },
  { name: 'escalate_to_owner',
    description: 'Passa la conversazione al titolare (richiesta di una persona, caso non gestibile).',
    input_schema: { type: 'object', required: ['reason', 'customer_message'], properties: {
      reason: { type: 'string' }, customer_message: { type: 'string' }, callback_window: { type: 'string' } } } },
  { name: 'set_conversation_outcome',
    description: "Registra l'esito della conversazione prima di chiuderla.",
    input_schema: { type: 'object', required: ['outcome'], properties: {
      outcome: { type: 'string', enum: ['booked', 'rescheduled', 'cancelled', 'info_only', 'escalated', 'abandoned'] },
      summary: { type: 'string' } } } },
]
export const CUSTOMER_TOOL_NAMES = CUSTOMER_TOOL_SCHEMAS.map((t) => t.name)
```

Why `proposal_id` instead of copying `start_iso`/staff: the model cannot garble times or staff ids. The dispatch keeps the proposals it returned for this turn in a per-session `Map<proposal_id, assignments>`, and the writes resolve from that map. A proposal_id the dispatch never issued is refused as `unknown_proposal`. This is what "deterministic" means for writes.

Before finalising the enum, check the real outcome values accepted by voice-booking's `calls.outcome` CHECK (`booking_engine/db/sql/03_voice_agent_schema.sql`).

- Test: names equal the approved naming table, in order.
- Commit: `feat(customer-agents): schemas with the approved names`.

### Task A2.4: reads

**File:** `src/lib/customer-agents/reads.ts` + tests (mock `sql` the way `tests/lib/business-advisor-grounding*.test.ts` do)

- `customersIdentify(session)`:

```sql
SELECT id, full_name FROM business_app_core.customers
WHERE shop_id=$1 AND regexp_replace(coalesce(phone,''),'\D','','g') = regexp_replace($2,'\D','','g')
ORDER BY verified DESC, created_at LIMIT 5
```

  - Returns `{ok:true, data:{matches:[{customer_id, full_name}]}}`.
  - When exactly one matches, also POST voice-booking `/sessions/{call_id}/customer` (A3.1) to link the session. Best effort: a failure is logged, never thrown.
- `servicesCatalogForCustomer(session, {name, include_price})`: reuse `servicesCatalog` from grounding.
  - Add a per-service `owner_notes`: the non-empty `voice_agent.service_intake`/intake text. **Find the real table** by reading how voice-booking's `GET /voice/config/{shop_id}/intake` reads it (`booking_engine/db/*intake*`).
  - Strip `price_eur` unless `include_price === true`.
  - Drop `dead_offset_minutes`/`dead_minutes` (engine internals).
- `appointmentsUpcoming(session)`: appointments with `start_time > now()` and status not in (cancelled, no_show) for customers whose digits-phone equals the session phone.
  - Returns `appointment_id`, local `date` and `time` in `session.timezone`, and the services with staff names. Limit 5.
- Commit: `feat(customer-agents): in-process reads`.

### Task A2.5: availability wrapper

**File:** `src/lib/customer-agents/availability.ts` + test (mock `searchAvailability`)

Behaviour:
1. Validate the dates. The range is `[date_from, min(date_to ?? date_from, date_from+6)]`. A `date_from` in the past becomes today (local).
2. For each day, call `searchAvailability(shopId, {date, service_ids, pinned: staff_id ? Object.fromEntries(service_ids.map(id=>[id, staff_id])) : undefined, now_iso})`.
3. Keep candidates with `unplaced.length === 0` whose local start is within `[time_from, time_to]`, when those are given.
4. Rank:
   - by `|start − near_time|` on each day when `near_time` is given
   - otherwise chronologically
   - then take the best 5 across the range, preferring earlier days on ties
5. Enrich each result with service and staff names (one `servicesCatalog` read), and give it `proposal_id = 'P' + n`. Store `assignments` (from the candidate: `serviceId, staffId, start_iso`) in `session.proposals`.
6. Return `{ok:true, data:{proposals:[{proposal_id, date, weekday, start:'HH:MM', end:'HH:MM', services:[{service_name, staff_name, start, end}]}]}}` with local times.
7. With 0 proposals, return `{ok:true, data:{proposals:[], shop_hours:[{weekday, open, close}], closed_days:[...]}}` from `business_app_core.shop_hours` plus the shop-wide `time_off` in the range, so the model can say "siamo aperti dalle 9 alle 18:30". This is the owner's rule 1.1/1.2: out-of-hours requests get the opening hours, not a flat "no".

Tests:
- `near_time: '15:00'` on an empty day returns 14:00–16:00 slots, not 09:00
- a window after closing returns `proposals:[]` plus `shop_hours`
- `date_to` is capped at 7 days
- a partial candidate (`unplaced` non-empty) is excluded
- proposal ids resolve to their assignments

Commit: `feat(customer-agents): availability_search wrapper over the agenda engine`.

### Task A2.6: writes + dispatch + confirmation

**Files:** `writes.ts`, `dispatch.ts`, `confirmation.ts` + tests

- `writes.ts`:
  - `agentWrite(path, body)` → POST/PATCH `${WEBAPP_BASE_URL}/api/v1/hair-salon/agent/${path}` with `Authorization: Bearer ${MARKET_INTEL_SECRET}` (add it to `src/lib/webapp/client.ts` next to `searchAvailability`; reuse its `authHeaders()`).
  - A network error, 401, 404 on the route itself, or 5xx throws `TransportError`.
  - A 403/404/409/422 with a JSON `error` returns `{ok:false, error}`.
  - `sessionWrite(callId, path, body)` → voice-booking `${VOICE_AGENT_TOOLS_URL}/sessions/${callId}/${path}`, with `Authorization: Bearer ${VOICE_AGENT_TOOL_SECRET}` and `X-Shop-Id`. Same error split.
- `dispatch.ts`:

```ts
export function customerDispatch(session: CustomerSession & { proposals: Map<string, Assignment[]> }): Record<string, ToolFn>
```

  - One entry per schema name. `create_customer` sends `{full_name: first+' '+last, phone: session.phone, source: session.channel === 'whatsapp' ? 'whatsapp' : 'voice_agent', created_by_call_id: session.callId}`, then links the session customer (as in A2.4).
  - `create_appointment` / `reschedule_appointment` resolve `proposal_id` from `session.proposals`; an unknown id returns `{ok:false, error:'unknown_proposal'}`.
  - The phone is **never** taken from args.
- `confirmation.ts`: `confirmationText(data, session)` builds from the A1.5 response, in the shop timezone:

  `✅ Prenotazione confermata: {service_names joined " + "}, {weekday} {day} {month} alle {HH:MM} con {staff names joined " e "}. A presto da {shopName}!`

  Test the exact string for a two-service/two-staff appointment.
- Commit: `feat(customer-agents): dispatch, writes and deterministic confirmation`.

### Task A2.7: prompt + WhatsApp turn loop on the common layer

**Files:** `prompt.ts`, `src/lib/whatsapp/agent.ts`, `src/routes/whatsapp-agent.ts`, `tests/whatsapp/agent.test.ts`

`customerRules(channel)` is shared by both surfaces:

```
Sei l'assistente del salone "{shopName}". Rispondi ai clienti del salone.
{dateContext(now, tz)}
All'inizio chiama customers_identify per sapere chi ti scrive; se c'è un solo cliente, chiamalo per nome.
Per servizi, durate e operatori usa services_catalog; non elencare servizi che non ha restituito.
DISPONIBILITÀ: proponi, conferma o prometti SOLO orari restituiti da availability_search in questa conversazione.
  Traduci le richieste in finestre: "mattina" 09:00-12:00, "pomeriggio" 14:00-18:00, "sera" dalle 17:00, "verso le 15" near_time 15:00.
  Se non ci sono proposte e ti restituisce gli orari di apertura, dillo e proponi l'orario utile più vicino.
CLIENTE NUOVO: se customers_identify non trova nessuno, chiedi nome e cognome e crea la scheda con create_customer prima di prenotare.
Prima di create_appointment riepiloga servizio, giorno, ora e operatore e attendi il sì del cliente.
Dopo create_appointment la conferma la invia il sistema: non ripeterla.
Modifiche e annullamenti solo sugli appuntamenti restituiti da appointments_upcoming, dopo conferma del cliente.
Prezzi solo se chiesti (services_catalog con include_price=true); niente sconti.
Se il cliente chiede una persona o qualcosa non torna, usa escalate_to_owner.
Niente consigli medici, nessuna promessa di risultati estetici. Non inventare nulla che non venga dagli strumenti.
Rispondi nella lingua del cliente. Non presentarti e non definirti assistente; se chiede esplicitamente se sei una persona, rispondi con sincerità.
```

The channel suffix:
- whatsapp: `Stile WhatsApp: due o tre frasi, niente elenchi lunghi, niente formattazione pesante.`
- voice: `Stile parlato: frasi brevi, niente elenchi; prima di cercare dì una breve frase d'attesa. Il numero del chiamante può non essere il suo: conferma nome e numero prima di prenotare.`

`runTurn(input: {shopId, callId, messages, now})`:
1. `session = await loadSession(shopId, callId, 'whatsapp')`. If null, return `{text:'', escalate:true, reason:'no_session'}`.
2. Keep the existing loop mechanics: `MAX_AGENT_TOOL_CALLS`, `REFUSALS_BEFORE_ESCALATION`, `tool_ceiling`, `tool_refused`, and `tool_unreachable` on `TransportError`. Tools are `CUSTOMER_TOOL_SCHEMAS`, and execution is `executeCustomerTool(customerDispatch(session), shopId, name, args, {turnId: callId, surface: 'whatsapp_agent'})`.
3. When `create_appointment` returns ok, **end the turn immediately** with `text = confirmationText(result.data, session)`. There is no further model call. Same for `reschedule_appointment`, with the text `✅ Appuntamento spostato: …`, and `cancel_appointment`, with `Appuntamento annullato: {date} alle {time}.`
4. Delete from `agent.ts`: `catalogueBlock`, `blobsFrom`, `intakeBlock`, `ground`, `serviceIdsIn`, `namedInThread`, `SERVICE_LISTS/FIELDS`, `withDisclosure`, `buildSystem`, and the `services/intake/firstTurn/customerName/customerPhone/shopName` input fields.
5. `src/routes/whatsapp-agent.ts` accepts `{shop_id, call_id, messages, now}` and ignores unknown fields. voice-booking still sends the old ones until A3 lands, so ignoring them keeps the deploy order-independent.

Tests (`tests/whatsapp/agent.test.ts`): rewrite around the new loop, mocking `executeCustomerTool` results.
- a successful `create_appointment` returns the exact confirmation text and makes no second LLM call
- `TransportError` → `tool_unreachable`
- two `{ok:false}` → `tool_refused`
- the system prompt contains the shop name and the `lunedì 28 settembre` line, and does NOT contain `BLOCCO RUOLO`, `PRIVACY:` or `AMBITO:`
- no test references `/voice/tools/`

Delete `src/lib/whatsapp/tools.ts`, `src/lib/whatsapp/grounding.ts` (if only used by the removed code) and `tests/whatsapp/grounding.test.ts`.

Commit: `feat(whatsapp-agent): runs on the customer-agents common layer`.

### Task A2.8: `reschedule_visit` rename + full verify

- In marketing-engine, rename `reschedule_visit` → `reschedule_appointment` (`tools.ts` WRITE_TOOL_SCHEMAS, prompts `src/lib/i18n/prompts/*.ts`, tests).
- Run `npx tsc --noEmit -p . && npx jest`. Everything passes, apart from the known flaky `tests/lib/chat-repo.test.ts › claimTurn` timeout. Re-run it once; report if it fails twice.
- Update `CLAUDE.md` in `kairo-market-intel` with a short "Customer agents (WhatsApp + voice)" section: where the dispatch lives, the naming rule, and that writes go to the webapp `/agent/*` routes.
- Commit.

---

## A3 (voice-booking) — Session endpoints, slimmer payload, delete the old slot engine

### Task A3.1: session endpoints

**Files:** Create `booking_engine/api/routes/sessions.py`, mount it in `api/app.py` **without** the `/api/v1` prefix, like `/voice/tools`. Create `tests/booking_engine/test_sessions_routes.py`.

- Auth for all three: `require_tool_token` plus the `X-Shop-Id` header. The `voice_agent.calls` row `call_id` must have that `shop_id` (404 `unknown_session` otherwise).
- `POST /sessions/{call_id}/customer` `{customer_id}`: sets `calls.customer_id` and `customer_match='existing'`, but only if the customer's `shop_id` matches (404 otherwise).
- `POST /sessions/{call_id}/escalation` `{reason, customer_message, callback_window?}`: moves the body of today's `escalate_to_merchant` handler here unchanged (`api/routes/voice_tools_lifecycle.py:34`), including the memo insert and the session escalated flag.
- `POST /sessions/{call_id}/outcome` `{outcome, summary?}`: moves the `mark_outcome` handler body here unchanged.
- All return the existing `Envelope` shape `{ok, data|error}`.
- Tests: wrong shop → 404; happy path per route; a customer from another shop → 404.
- Docs: add `docs/knowledge/api/sessions.md`, link it from `api/README.md` and `_sidebar.md`.
- Commit: `feat(sessions): session-owned writes for the customer agents`.

### Task A3.2: slimmer engine payload + thread customer fallback

- `booking_engine/clients/marketing_agent.py` `turn(...)` and `services/messaging/wa_agent.py::handle`: send only `shop_id, call_id, messages, now`. Delete `_services`, `_cents`, `_shop_name`, `_customer_name`, the intake read and their tests. The customer name now comes from `customers_identify`.
- `booking_engine/db/whatsapp_thread_queries.py`: in `thread_list` and the thread-detail header, `customer_id` becomes `coalesce(last_in.customer_id, <latest open whatsapp calls row for that phone>.customer_id)`, so the Inbox shows the customer once the agent has identified or created them.
  - Add a test with a fake row, following the file's existing test style.
- Commit: `refactor(wa-agent): the engine loads its own context; threads show the identified customer`.

### Task A3.3: `sent_today` in shop-local time

- `db/whatsapp_queries.py::sent_today`: replace `date_trunc('day', now())` with `date_trunc('day', now() AT TIME ZONE s.timezone) AT TIME ZONE s.timezone`, joining `business_app_core.shops s` on shop_id.
- Test: the SQL contains `AT TIME ZONE` and binds nothing new. Verify it once against the QA DB with `psql` before committing (the plan's standing rule: execute non-trivial SQL against real rows).
- Commit: `fix(whatsapp): sent_today counts the salon's day`.

### Task A3.4: delete the old slot engine

- Delete the Phase A items listed in the file map. Before deleting, `grep -rn` each symbol and confirm no caller remains outside the deleted set.
- Keep `create_appointment`, `create_appointment_chain`, `SlotConflictError` and `insert_booking_locked` until Phase C, because the `/voice/tools/create_booking` route still uses them.
- Update `docs/knowledge/api/business.md`, `docs/knowledge/api/voice-tools.md` (check_availability row → "moved to marketing-engine `availability_search`") and `docs/knowledge/architecture.md` if it mentions the slot search.
- Run the full suite. The count drops by exactly the deleted tests; report before and after.
- Commit: `refactor: remove voice-booking's slot search; the webapp engine is the only one`.

### Task A3.5: AGENTS.md entry

Add a dated entry at the top covering:
- the common layer and why
- the single engine for search and writes, and what it fixed (absences, hours, reschedule checks)
- the naming table
- the deleted engine
- the session endpoints
- verification counts for all three repos

Update `docs/knowledge/decisions.md`. Commit.

---

# PHASE C — Voice on the common layer

**Precondition:** A2 is merged in the marketing-engine working tree.

### Task C1 (marketing-engine): voice MCP surface + instructions endpoint

**Files:** Create `src/routes/customer-agents-voice.ts`, mount it in the Express app next to `whatsappAgentRouter` (find where that is registered: `grep -rn whatsappAgentRouter src`). Create `tests/customer-agents/voice-mcp.test.ts`.

- Dependency: `@modelcontextprotocol/sdk`, using the stateless `StreamableHTTPServerTransport` (`sessionIdGenerator: undefined`). voice-booking's Python side uses the equivalent server today (`booking_engine/mcp_server.py`), which OpenAI Realtime already talks to.
- `POST /customer-agents/voice/mcp`:
  - Auth `Authorization: Bearer ${VOICE_AGENT_TOOL_SECRET}`, plus the headers `X-Shop-Id` and `X-Call-Id`, which the realtime session config sets on the MCP tool.
  - Per request:
    - `session = loadSession(shop, call, 'voice')`, or 401 if null
    - `tools/list` → `CUSTOMER_TOOL_SCHEMAS` mapped to MCP `{name, description, inputSchema}`
    - `tools/call` → `executeCustomerTool(customerDispatch(sessionWithProposals), …, {turnId: callId, surface: 'voice_agent'})`, returning `JSON.stringify(result)` as text content
  - `session.proposals` must survive between tool calls of one call, while each HTTP request is stateless. Keep a module-level `Map<callId, {proposals, expiresAt}>` with a 2h TTL. Note the ceiling in a `ponytail:` comment: a single process, so with several Fly machines a proposal made on one is unknown on another. The upgrade path is a `voice_agent.call_proposals` table.
  - Voice gets no deterministic confirmation text, because the model must speak it. The `create_appointment` result carries a `confirmation` field (the same `confirmationText`), and the voice rules say "leggi al cliente il campo confirmation".
- `GET /customer-agents/voice/instructions?shop_id=&call_id=`: same auth. Returns `{instructions: customerRules('voice') filled for the session}`.
- Tests:
  - `tools/list` returns the 11 approved names
  - `tools/call create_appointment` with an unknown `proposal_id` → `unknown_proposal`
  - no `X-Call-Id` → 401
  - the instructions contain the shop name and not `BLOCCO RUOLO`
- Commit: `feat(customer-agents): voice MCP surface on the common layer`.

### Task C2 (voice-booking): point the realtime session at marketing-engine

- `booking_engine/services/voice_openai.py`:
  - The MCP tool `server_url` becomes `${MARKET_INTEL_API_URL}/customer-agents/voice/mcp`, with the same bearer and the `X-Shop-Id`/`X-Call-Id` headers it sends today.
  - `instructions` are fetched from `GET ${MARKET_INTEL_API_URL}/customer-agents/voice/instructions` at accept time and concatenated after the persona/greeting that `prompt_assembler.py` still owns (voice preset, tone, greeting, overflow text: shop configuration, not agent rules).
  - If the fetch fails, the call is still accepted with persona-only instructions, and `logger.error` fires (GlitchTip picks it up).
- `booking_engine/services/call_supervisor.py`: the `response.create` nudge after each `mcp_call` stays. Tool names in its telemetry now come from marketing-engine.
- Tests: update `test_voice_openai*.py` for the new URL and the instructions fetch (mock httpx), including the fetch-failure path.
- Commit: `feat(voice): realtime tools and rules come from the customer-agents layer`.

### Task C3 (voice-booking): delete the voice tool layer

- Delete the Phase C list from the file map, after `grep -rn` confirms no caller.
  - `api/routes/appointments.py` may still use `create_appointment`. If so, keep that function, and only it.
  - Remove `SAFETY_PROMPT`, `DEFAULT_TOOL_ALLOWLIST`, `ATTESA_TOOLS` and `_TOOL_SCHEMAS` from `safety_layer.py`. Keep `booking_authz` only if something outside the deleted routes imports it; otherwise delete it too.
- Scripts `scripts/chat_agent.py`, `simulate_call.py`, `voice_test_server.py`: delete them if they only drove `/voice/tools`, otherwise repoint them.
- Docs:
  - `docs/knowledge/api/voice-tools.md` → a short page pointing at marketing-engine `customer-agents`
  - `voice-agent-logic.md` updated
  - `mcp` removed from both `requirements.txt` if nothing else imports it
- Full suite green. Report the counts. AGENTS.md entry: what moved, what was deleted.
- Commit: `refactor(voice): the voice tool layer lives in marketing-engine now`.

**Phase C cannot be fully verified without a live call.** It is done when the unit tests are green and one manual QA SIP call (owner) shows `tools/list` and one `availability_search` in `market_intel.agent_call` with `surface='voice_agent'`.

---

# PHASE B — Shop timezone everywhere else (after A and C)

One helper per repo, then mechanical replacement, grouped by risk. In each group: write one test that pins the behaviour for a non-Rome shop (`Europe/London`), replace, run the suite, commit.

### B0: helpers
- webapp: `getShopTimezone` exists (`shops.repo.ts:27`). Add `shopToday(tz)`, `shopYmd(date, tz)`, `shopZonedToUtc(ymd, hhmm, tz)` to `src/lib/time/` (rename `rome.ts` → `zone.ts`, keeping `rome*` wrappers deprecated until B finishes).
- marketing-engine: `customer-agents/time.ts` from A2.2 is the helper. Add `shopTimezone(shopId)` (cached per process for 5 minutes).
- voice-booking: `booking_engine/db/shop_tz.py::get_shop_timezone(shop_id) -> ZoneInfo`.

### B1: "today" computed in UTC (real off-by-one bugs, 00:00–02:00 Rome)
- webapp:
  - `components/bookings/NewApptPanel.tsx:74`
  - `app/api/v1/hair-salon/customers/[id]/whatsapp-offer/route.ts:97`
  - `campaigns/[id]/recipients/route.ts:110`
  - `campaigns/[id]/generate/route.ts:102`
  - `lib/db/repositories/customer-packages.repo.ts:294`
  - `components/business/StaffCostEditor.tsx:12`
  - `lib/business/overhead.ts:35`
  - `lib/business/commerce-setup.ts:21,28`
  - `lib/db/repositories/business-commerce.repo.ts:371,491`
- marketing-engine:
  - `grounding.ts:392` (`agendaDay` default)
  - `business-advisor/tools.ts:17-30` (`ymd`, `resolveRange`)
  - `scheduler/batch.ts:272`
- `CURRENT_DATE` → `(now() AT TIME ZONE tz)::date`:
  - webapp `action-center.repo.ts:522-523`
  - webapp `business-commerce.repo.ts:343,361,422,532`
  - webapp analytics `hire-scenario:132`, `idle-labour:145`, `menu-engineering:205`, `forward-book:140`, `cash-flow:231`
  - marketing-engine `grounding.ts:908-964`

### B2: month bucketing truncated in UTC
- `date_trunc('month', ts) AT TIME ZONE …` → `date_trunc('month', ts AT TIME ZONE tz)`:
  - webapp `action-center.repo.ts:49`
  - marketing-engine `grounding.ts:828-834`

### B3: hardcoded `'Europe/Rome'` in SQL (about 150 sites)
Replace with a bound `${tz}` from `getShopTimezone(shopId)`, one repository file per commit:
- `appointments.repo.ts` (remaining functions)
- `staff.repo.ts`
- `action-center.repo.ts`
- `daily-insights.repo.ts`
- `payments.repo.ts`
- `payments-analytics.repo.ts`
- `analytics/*.repo.ts` (forward-book, cash-flow, frequency-drift, forecast, idle-labour, cohort-ltv)
- marketing-engine `grounding.ts` (45, 200, 210, 392-408, 621-666, 690-691, 741-755)
- `00_baseline.sql:448-449` view: a new migration that joins `shops.timezone`

### B4: hardcoded Rome in JS formatting
- webapp:
  - `lib/utils.ts:17-38`
  - `lib/announcements/rome-today.ts`
  - `lib/receipt/receipt.ts:92`
  - `lib/action-center/staff-brief.ts:38`
  - `app/api/v1/action-center/staff-brief/route.ts:34,124`
  - `app/bookings/page.tsx:92,291`
  - `BusinessOperationalSetup.tsx:433-439,481`
  - `lib/booking/slip-ghost.ts`, `lib/booking/pending-busy.ts`
- Browser-local computations that should be shop-local:
  - `AnalyticsSection.tsx:24-25`
  - `lib/register/day-totals.ts:32`
  - `app/action-center/page.tsx:1525`
  - `BusinessDashboardView.tsx:135`
  - `lib/action-center/sources.ts:79-80`
- The shop timezone reaches the client via the existing shop context. Add `timezone` to it if it is not there.

### B5: voice-booking and the global cron
- voice-booking:
  - `whatsapp_send.py` `SALON_TZ` → per-shop tz in `spread()`
  - `whatsapp_automations.py:43,77,83,189`
  - `db/voice_queries.py:146,207,214,226-228`
  - `db/queries.py` `_ROME` (whatever survives Phase C)
- marketing-engine `scheduler/cron.ts`: stays global Rome (it is a platform schedule, not a shop one). Document that with a one-line comment rather than changing it.

### B6: close-out
- Delete the `rome*` wrappers once `grep -rn "Europe/Rome\|romeTo\|_ROME\|SALON_TZ" src booking_engine` returns only the `DEFAULT_TIMEZONE` constant and documented platform-level uses.
- AGENTS.md entry.

---

## Execution order and parallelism

1. **In parallel:** A1 (webapp) · A2 (marketing-engine) · A3.1–A3.3 (voice-booking). They agree on the contracts written above, so nothing blocks.
2. A3.4–A3.5 after A2.7 (the old engine is only deletable once WhatsApp no longer calls `check_availability`).
3. C1 after A2. C2 and C3 after C1.
4. Coordinator:
   - cross-repo review by a separate reviewer subagent
   - push all three repos together
   - reseed QA (`scripts/seed_test_sender.py` + templates ensure + agent opt-in)
   - a live WhatsApp conversation: new customer, evening request out of hours, booking, confirmation, reschedule, cancel
5. Phase B afterwards, one repo at a time.

## Self-review notes
- Every owner requirement maps to a task:
  - shop name from the session: A2.2
  - customer name via an agent: A2.3/A2.4 `customers_identify`
  - phone removed from the prompt: A2.7
  - shop timezone: A2.2, A1.7, Phase B
  - catalogue via an agent: A2.4
  - role/privacy/scope rules removed: A2.7
  - out-of-hours answer with the opening hours: A2.5 step 7
  - new customer in «Creati dall'assistente»: A1.3
  - deterministic confirmation: A2.6/A2.7
  - dedicated deterministic variant with no "from_call": A2.3/A2.6
  - runTool: A2.1
  - naming: the table, A1.8, A2.8
  - a single engine: A1.5–A1.7, A2.5, A3.4
  - a common layer for WhatsApp and voice: A2 + C
  - inbound customer matching (the open item from 2026-09-28): A2.4 link + A3.2 thread fallback
- Known residue, out of scope: when the routing menu fires first, the typed message before the tap stays outside the transcript (AGENTS.md 2026-09-28).
