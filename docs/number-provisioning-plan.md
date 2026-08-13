# Number Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A salon on a paid plan requests an Estonian mobile number from Inbox, uploads the one document Estonia requires, and gets a number once Twilio approves — with a health semaphore that keeps saying whether the number is actually online.

**Architecture:** The regulatory bundle lifecycle lives in a new `voice_agent.number_requests` table (one row per shop). The webapp owns the gate, the form and the UI; voice-booking owns every Twilio call. Approval and purchase are asynchronous, driven by an hourly cron that also runs the health check. Twilio's own `Evaluations` endpoint is the validator — we never encode Estonian rules.

**Tech Stack:** Python 3 / FastAPI / asyncpg / `twilio` SDK; Next.js / TypeScript / Vitest; Postgres 17 on Neon.

**Branches:** voice-booking → new branch off `QA`. webapp → new branch off `QA`. (Phase 1 SMS work is in flight on `feat/messaging-sms-phase1` / `dev_messaging_sms`; do not build on top of unmerged work — branch from `QA`.)

**Reference:** `docs/number-provisioning-design.md`. Read §2, §3, §5, §6, §7 before starting.

---

## Ground truth established before planning

Verified against the real Twilio account, not assumed:

- Regulation `RN26dca8d0e541a6c8fce4abd46e518506` — "Estonia: Mobile - Business", `end_user_type: business` only.
- End-User needs exactly one field: `business_name`.
- One supporting document: type `commercial_registrar_excerpt`, field `business_name`, plus the file.
- No address, VAT or personal ID is requested. **Do not send fields the regulation does not ask for** — that is how bundles fail evaluation.

---

## File Structure

**voice-booking**

| File | Responsibility |
|---|---|
| `booking_engine/db/sql/12_number_requests.sql` | `number_requests` table + `shop_telephony` health columns |
| `booking_engine/clients/twilio_regulatory.py` | Regulations / EndUsers / SupportingDocuments / Bundles / Evaluations |
| `booking_engine/clients/twilio_numbers.py` | *(modify)* add `release_number`, `fetch_number` |
| `booking_engine/db/number_request_queries.py` | SQL for `number_requests` + health columns |
| `booking_engine/db/voice_telephony_queries.py` | *(modify)* add insert-only `insert_telephony` |
| `booking_engine/services/number_provisioning.py` | request → evaluate → submit; approval → purchase |
| `booking_engine/services/number_health.py` | the semaphore (pure decision + thin Twilio glue) |
| `booking_engine/api/routes/voice_telephony.py` | *(modify)* `/request`, `/request/{shop_id}`, fix `/provision` |
| `booking_engine/api/routes/messaging_tick.py` | `POST /messaging/tick` — drains approvals + health |

**webapp**

| File | Responsibility |
|---|---|
| `src/lib/billing/subscription-gate.ts` | `hasActiveSubscription` |
| `src/app/api/v1/hair-salon/voice/numbers/request/route.ts` | gated multipart passthrough |
| `src/app/api/v1/hair-salon/voice/numbers/status/route.ts` | request + number + health for the UI |
| `src/app/api/v1/hair-salon/voice/numbers/provision/submit/route.ts` | **delete** — 404s upstream |
| `src/components/inbox/channels/NumberPanel.tsx` | the four states |
| `src/components/inbox/channels/NumberRequestForm.tsx` | 2 fields + file |
| `src/components/inbox/tabs/ConfigurationTab.tsx` | *(modify)* mount a "Canali" section |

---

## Task 1: Schema

**Files:** Create `booking_engine/db/sql/12_number_requests.sql`

- [ ] **Step 1: Write it**

```sql
-- Regulatory bundle lifecycle for self-service number provisioning.
-- See docs/number-provisioning-design.md §5. Idempotent: migrate.sh re-applies
-- every file on every run.

CREATE TABLE IF NOT EXISTS voice_agent.number_requests (
  shop_id           uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  status            text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','evaluating','pending_review',
                                      'approved','rejected','provisioned')),
  regulation_sid    text,
  bundle_sid        text,
  end_user_sid      text,
  document_sid      text,
  business_name     text,
  contact_email     text,
  evaluation_errors jsonb,
  rejection_reason  text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  submitted_at      timestamptz,
  reviewed_at       timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Only pending_review rows are polled; keep that scan cheap.
CREATE INDEX IF NOT EXISTS number_requests_pending_idx
  ON voice_agent.number_requests (status) WHERE status = 'pending_review';

-- Health semaphore. 'unknown' until the first check runs; a Twilio outage
-- leaves the previous value rather than painting every shop red.
ALTER TABLE voice_agent.shop_telephony
  ADD COLUMN IF NOT EXISTS health_status text NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS health_detail text,
  ADD COLUMN IF NOT EXISTS health_checked_at timestamptz;

DO $$ BEGIN
  ALTER TABLE voice_agent.shop_telephony
    ADD CONSTRAINT shop_telephony_health_status_check
    CHECK (health_status IN ('unknown','green','red'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
```

- [ ] **Step 2: Prove idempotency**

```bash
createdb nr_check
psql -v ON_ERROR_STOP=1 nr_check -c 'CREATE SCHEMA business_app_core; CREATE SCHEMA voice_agent;
  CREATE TABLE business_app_core.shops (id uuid PRIMARY KEY);
  CREATE TABLE voice_agent.shop_telephony (shop_id uuid PRIMARY KEY);'
psql -v ON_ERROR_STOP=1 nr_check -f booking_engine/db/sql/12_number_requests.sql
psql -v ON_ERROR_STOP=1 nr_check -f booking_engine/db/sql/12_number_requests.sql
psql nr_check -c '\d voice_agent.number_requests' -c '\d voice_agent.shop_telephony'
dropdb nr_check
```
Both runs must exit 0. The `DO $$` block is there because `ADD CONSTRAINT` has no `IF NOT EXISTS`; confirm the second run does not error on it.

- [ ] **Step 3: Commit** — `git add booking_engine/db/sql/12_number_requests.sql && git commit -m "feat(numbers): regulatory request table and health columns"`

---

## Task 2: Fix the orphaned-number bug

This is a money bug and it exists today. See design §3.1.

**Files:** Modify `booking_engine/db/voice_telephony_queries.py`, `booking_engine/clients/twilio_numbers.py`; Test `tests/booking_engine/test_number_provisioning.py`

- [ ] **Step 1: Failing test**

```python
import pytest
from uuid import uuid4
from booking_engine.db import voice_telephony_queries as q

@pytest.mark.asyncio
async def test_insert_telephony_does_not_overwrite_an_existing_number(monkeypatch):
    """The PK guarantees one row; nothing guaranteed one PURCHASE. A second
    insert must lose, not overwrite — the overwritten number stays billed by
    Twilio forever with nothing referencing it."""
    seen = {}
    async def fake_execute_one(sql, *args):
        assert "DO NOTHING" in sql, "must not overwrite an existing row"
        seen["sql"] = sql
        return None            # conflict: a row already existed
    monkeypatch.setattr(q, "execute_one", fake_execute_one)

    row = await q.insert_telephony(
        shop_id=uuid4(), provider="twilio", kairo_number="+37251234567",
        kairo_number_sid="PN1", salon_existing_number=None, setup_path="new",
    )
    assert row is None
```

- [ ] **Step 2: Run — expect `AttributeError: ... has no attribute 'insert_telephony'`**

- [ ] **Step 3: Add `insert_telephony`** to `voice_telephony_queries.py`, leaving `upsert_telephony` untouched for legitimate status updates:

```python
async def insert_telephony(
    *,
    shop_id: UUID,
    provider: str,
    kairo_number: str,
    kairo_number_sid: str,
    salon_existing_number: str | None,
    setup_path: str,
    activation_status: str = "active",
) -> dict | None:
    """Claim the shop's one telephony row. Returns None if it already existed.

    Deliberately NOT upsert_telephony: overwriting swaps kairo_number and
    orphans the previously purchased number, which stays billed by Twilio with
    nothing referencing it. A None here means the caller must release the
    number it just bought. See docs/number-provisioning-design.md §3.1.
    """
    return await execute_one(
        """
        INSERT INTO voice_agent.shop_telephony
            (shop_id, provider, kairo_number, kairo_number_sid,
             salon_existing_number, setup_path, activation_status)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (shop_id) DO NOTHING
        RETURNING *
        """,
        shop_id, provider, kairo_number, kairo_number_sid,
        salon_existing_number, setup_path, activation_status,
    )
```

- [ ] **Step 4: Add `release_number` and `fetch_number`** to `booking_engine/clients/twilio_numbers.py`:

```python
def release_number(*, sid: str, account_sid: str, auth_token: str) -> None:
    """Give a number back. Called when we bought one we cannot store — without
    this, a lost race leaks a number billed at ~$3/mo forever."""
    Client(account_sid, auth_token).incoming_phone_numbers(sid).delete()


@dataclass
class NumberStatus:
    sid: str
    phone_number: str
    voice_url: str
    sms_url: str


def fetch_number(*, sid: str, account_sid: str, auth_token: str) -> NumberStatus:
    """Read a number back from Twilio. Raises TwilioRestException(404) if the
    number no longer belongs to this account."""
    n = Client(account_sid, auth_token).incoming_phone_numbers(sid).fetch()
    return NumberStatus(
        sid=n.sid, phone_number=n.phone_number,
        voice_url=n.voice_url or "", sms_url=n.sms_url or "",
    )
```

- [ ] **Step 5: Run the test — 1 passed. Then full suite: `python -m pytest tests/ --ignore=tests/live_db -q` — no regressions.**

- [ ] **Step 6: Commit** — `git commit -m "fix(numbers): insert-only telephony claim, plus release/fetch"`

---

## Task 3: Twilio regulatory client

**Files:** Create `booking_engine/clients/twilio_regulatory.py`; Test `tests/booking_engine/test_twilio_regulatory.py`

Thin wrappers over `numbers.twilio.com/v2/RegulatoryCompliance`. The Twilio Python SDK's coverage of this API is uneven, so use `httpx` against the REST endpoints directly — the repo already depends on `httpx`.

- [ ] **Step 1: Implement**

```python
"""Twilio Regulatory Compliance API — per-salon bundles.

Twilio's ISV rules require one bundle per end customer: "Do not reuse your
business information in customer bundles. Twilio audits this." So each salon
gets its own End-User + document + bundle. See design §2.

Requirements are queried, never hardcoded — Twilio's docs are explicit that
regulations change and must be read from the Regulations resource.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from httpx import AsyncClient, BasicAuth

_BASE = "https://numbers.twilio.com/v2/RegulatoryCompliance"


@dataclass(frozen=True)
class Violation:
    friendly_name: str
    description: str


async def _post(path: str, data: dict, *, account_sid: str, auth_token: str,
                files: dict | None = None) -> dict:
    async with AsyncClient(auth=BasicAuth(account_sid, auth_token), timeout=30.0) as c:
        r = await c.post(f"{_BASE}{path}", data=data, files=files)
        r.raise_for_status()
        return r.json()


async def get_regulation_sid(*, iso_country: str, number_type: str,
                             account_sid: str, auth_token: str) -> str | None:
    """The regulation for a country + number type, or None if none applies."""
    async with AsyncClient(auth=BasicAuth(account_sid, auth_token), timeout=30.0) as c:
        r = await c.get(f"{_BASE}/Regulations",
                        params={"IsoCountry": iso_country, "NumberType": number_type})
        r.raise_for_status()
        results = r.json().get("results", [])
    return results[0]["sid"] if results else None


async def create_end_user(*, business_name: str, account_sid: str, auth_token: str) -> str:
    """Estonia's regulation asks for exactly one field. Sending extras is how
    bundles fail evaluation, so only business_name goes in."""
    body = await _post("/EndUsers", {
        "FriendlyName": business_name,
        "Type": "business",
        "Attributes": json.dumps({"business_name": business_name}),
    }, account_sid=account_sid, auth_token=auth_token)
    return body["sid"]


async def upload_document(*, business_name: str, doc_type: str, filename: str,
                          content: bytes, content_type: str,
                          account_sid: str, auth_token: str) -> str:
    body = await _post("/SupportingDocuments", {
        "FriendlyName": f"{business_name} — {doc_type}",
        "Type": doc_type,
        "Attributes": json.dumps({"business_name": business_name}),
    }, files={"File": (filename, content, content_type)},
       account_sid=account_sid, auth_token=auth_token)
    return body["sid"]


async def create_bundle(*, regulation_sid: str, iso_country: str, email: str,
                        friendly_name: str, account_sid: str, auth_token: str) -> str:
    body = await _post("/Bundles", {
        "FriendlyName": friendly_name,
        "RegulationSid": regulation_sid,
        "IsoCountry": iso_country,
        "EndUserType": "business",
        "Email": email,
    }, account_sid=account_sid, auth_token=auth_token)
    return body["sid"]


async def assign_item(*, bundle_sid: str, object_sid: str,
                      account_sid: str, auth_token: str) -> None:
    await _post(f"/Bundles/{bundle_sid}/ItemAssignments", {"ObjectSid": object_sid},
                account_sid=account_sid, auth_token=auth_token)


async def evaluate(*, bundle_sid: str, account_sid: str,
                   auth_token: str) -> tuple[bool, list[Violation]]:
    """Synchronous. Returns (compliant, violations).

    Twilio is the validator — we surface its own wording rather than
    reimplementing Estonian rules that change without notice.
    """
    body = await _post(f"/Bundles/{bundle_sid}/Evaluations", {},
                       account_sid=account_sid, auth_token=auth_token)
    if body.get("status") == "compliant":
        return True, []
    return False, [
        Violation(friendly_name=v.get("friendly_name", ""),
                  description=v.get("description", ""))
        for v in body.get("results", [])
        if v.get("passed") is False
    ]


async def submit_for_review(*, bundle_sid: str, account_sid: str, auth_token: str) -> None:
    await _post(f"/Bundles/{bundle_sid}", {"Status": "pending-review"},
                account_sid=account_sid, auth_token=auth_token)


async def get_bundle_status(*, bundle_sid: str, account_sid: str, auth_token: str) -> str:
    async with AsyncClient(auth=BasicAuth(account_sid, auth_token), timeout=30.0) as c:
        r = await c.get(f"{_BASE}/Bundles/{bundle_sid}")
        r.raise_for_status()
        return r.json().get("status", "")
```

- [ ] **Step 2: Tests with `respx`** (already a dependency — `tests/` uses it). Cover: `get_regulation_sid` returns None on an empty result list; `evaluate` maps `passed: false` entries to violations and ignores passed ones; `create_end_user` sends only `business_name` in `Attributes`.

- [ ] **Step 3: Verify against the REAL account, read-only.** This is the test that catches a regulation change:

```python
# tests/live_twilio/test_estonia_regulation.py — skipped unless TWILIO_ACCOUNT_SID is set
async def test_estonia_requirements_still_match_the_design():
    sid = await get_regulation_sid(iso_country="EE", number_type="mobile", ...)
    assert sid == "RN26dca8d0e541a6c8fce4abd46e518506"
    # If this fails, Estonia changed its rules — update design §2.1 before shipping.
```

Run it with credentials sourced from `.env` (never pasted into a file or a prompt):
`set -a; . ./.env; set +a; python -m pytest tests/live_twilio -v`

- [ ] **Step 4: Commit**

---

## Task 4: Request queries

**Files:** Create `booking_engine/db/number_request_queries.py`

Thin SQL, no branching, covered by Tasks 5 and 11. Functions: `get_request(shop_id)`, `upsert_request(...)`, `set_status(shop_id, status, **fields)`, `list_pending_review()`, `list_provisioned_numbers()`, `set_health(shop_id, status, detail)`.

`upsert_request` uses `ON CONFLICT (shop_id) DO UPDATE` — unlike telephony, re-submitting a *request* legitimately replaces the draft, and no money has been spent yet.

- [ ] Implement, `python -c "import booking_engine.db.number_request_queries"`, commit.

---

## Task 5: The request service

**Files:** Create `booking_engine/services/number_provisioning.py`; Test `tests/booking_engine/test_number_provisioning.py` (append)

- [ ] **Step 1: Failing tests** covering, with Twilio faked at the client-module boundary:
  - an existing `provisioned` request short-circuits and makes **no** Twilio calls
  - a noncompliant evaluation stores violations, sets `status='draft'`, and does **not** submit
  - a compliant evaluation submits and sets `status='pending_review'` with `submitted_at`
  - `get_regulation_sid` returning None yields `{"ok": False, "error": "no_regulation"}` rather than crashing

- [ ] **Step 2: Implement `submit_request(...)`** in the order End-User → document → bundle → 2 assignments → evaluate → submit, persisting each SID as it is created so a failure halfway leaves a resumable row rather than orphaned Twilio objects with no local reference.

- [ ] **Step 3: Implement `provision_approved(shop_id)`** — the piece the cron calls:

```python
async def provision_approved(shop_id, *, settings) -> str:
    """Buy the number for an approved bundle. Returns an outcome string.

    Purchase is the only irreversible step here, so it is bracketed: check for
    an existing row first (idempotent), and release the number if the insert
    loses a race. See design §3.1.
    """
    if await telephony_q.get_telephony(shop_id):
        await nr.set_status(shop_id, "provisioned")
        return "already_provisioned"

    req = await nr.get_request(shop_id)
    found = search_available_numbers(area_code=None, country=settings.twilio_default_country,
                                     limit=1, account_sid=..., auth_token=...)
    if not found:
        return "no_numbers_available"

    purchased = purchase_number(
        phone_number=found[0].phone_number,
        voice_url=f"{settings.public_base_url}/api/v1/voice/twiml/incoming",
        account_sid=..., auth_token=...,
        bundle_sid=req["bundle_sid"],      # the SALON's bundle, not Kairo's
        address_sid=settings.twilio_address_sid or None,
    )
    row = await telephony_q.insert_telephony(
        shop_id=shop_id, provider="twilio",
        kairo_number=purchased.phone_number, kairo_number_sid=purchased.sid,
        salon_existing_number=None, setup_path="new",
    )
    if row is None:
        # Lost the race. Give the number back or it bills forever.
        release_number(sid=purchased.sid, account_sid=..., auth_token=...)
        return "raced_released"
    await nr.set_status(shop_id, "provisioned")
    return "provisioned"
```

- [ ] **Step 4: Run tests, full suite, commit.**

---

## Task 6: The health semaphore

**Files:** Create `booking_engine/services/number_health.py`; Test `tests/booking_engine/test_number_health.py`

- [ ] **Step 1: Failing tests for the pure decision function**

```python
from booking_engine.services.number_health import decide_health, HealthProbe

BASE = "https://api.example.com"

def test_healthy_number_is_green():
    probe = HealthProbe(found=True, voice_url=f"{BASE}/api/v1/voice/twiml/incoming",
                        sms_url=f"{BASE}/api/v1/sms/webhook/inbound", reachable=True)
    assert decide_health(probe, base_url=BASE) == ("green", None)

def test_missing_number_is_red():
    probe = HealthProbe(found=False, voice_url="", sms_url="", reachable=True)
    assert decide_health(probe, base_url=BASE) == ("red", "number_missing")

def test_webhook_drift_is_red():
    probe = HealthProbe(found=True, voice_url="https://old.example.com/hook",
                        sms_url=f"{BASE}/api/v1/sms/webhook/inbound", reachable=True)
    assert decide_health(probe, base_url=BASE) == ("red", "webhook_drift")

def test_twilio_unreachable_does_not_flip_to_red():
    """A Twilio outage must not paint every salon red — only a definite
    negative answer changes the light."""
    probe = HealthProbe(found=False, voice_url="", sms_url="", reachable=False)
    assert decide_health(probe, base_url=BASE) == (None, "provider_unreachable")
```

- [ ] **Step 2: Implement.** `decide_health` returns `(status | None, detail)`; `None` means "leave the previous status, just record the attempt". The caller writes `health_checked_at` in every case and `health_status` only when the status is not None.

- [ ] **Step 3: `check_all()`** — one Twilio fetch per shop with a number, `asyncio.to_thread` around the blocking SDK call, exceptions classified as unreachable rather than propagating.

- [ ] **Step 4: Run, commit.**

---

## Task 7: Routes + the cron endpoint

**Files:** Modify `booking_engine/api/routes/voice_telephony.py`; Create `booking_engine/api/routes/messaging_tick.py`; Modify `booking_engine/api/app.py`; Test `tests/booking_engine/test_routes/test_number_routes.py`

- [ ] **Step 1:** `POST /voice/numbers/request` — multipart (`shop_id`, `business_name`, `contact_email`, `file`), control-plane token. Returns `{ok, status, violations}`.
- [ ] **Step 2:** `GET /voice/numbers/request/{shop_id}` — request + telephony + health for the UI.
- [ ] **Step 3:** Fix `POST /voice/numbers/provision` to use the idempotent path from Task 2 (check-first, insert-only, release-on-race).
- [ ] **Step 4:** `POST /messaging/tick` behind `require_control_plane_token` — polls `pending_review` bundles, calls `provision_approved` on approval, then `check_all()`. Returns counts.
- [ ] **Step 5:** Register both routers in `app.py` with `prefix="/api/v1"`.
- [ ] **Step 6:** Tests: `/request` rejects without the token; tick is idempotent (run twice, one purchase); tick returns counts.
- [ ] **Step 7:** Full suite, commit.

---

## Task 8: The cron workflow

**Files:** Create `.github/workflows/messaging-cron.yml`

- [ ] Hourly `cron: '0 * * * *'`, POSTs `/api/v1/messaging/tick` with `CONTROL_PLANE_SECRET`, plus the orphaned `POST /api/v1/voice/heartbeat/forwarding` which has had no scheduler since the 2026-07-18 Lambda removal. Fail the job on non-2xx so a silent breakage is visible. Commit.

---

## Task 9 (webapp): The subscription gate

**Files:** Create `src/lib/billing/subscription-gate.ts` + test

- [ ] **Step 1: Failing test** — `plan_id` null → false; non-null → true; missing shop → false.
- [ ] **Step 2: Implement**

```typescript
/**
 * "Paid tier" is `shops.plan_id IS NOT NULL`. There is no 'gratis' plan row —
 * free is the ABSENCE of a plan. billing/webhook/process-event.ts sets plan_id
 * on checkout and nulls it on cancellation, so this column already means
 * "currently paying".
 *
 * The first subscription gate in this app. Keep it one predicate; gating is
 * otherwise by vertical bundle and role. Do not add a tier concept until
 * something actually needs base-vs-pro.
 */
export async function hasActiveSubscription(shopId: string): Promise<boolean> {
  const [row] = await sql<{ plan_id: string | null }[]>`
    SELECT plan_id FROM business_app_core.shops WHERE id = ${shopId}
  `
  return Boolean(row?.plan_id)
}
```

- [ ] **Step 3: Run, commit.**

---

## Task 10 (webapp): Routes

**Files:** Create `.../voice/numbers/request/route.ts`, `.../voice/numbers/status/route.ts`; **Delete** `.../voice/numbers/provision/submit/route.ts`

- [ ] **Step 1:** Delete the dead `provision/submit` route — it POSTs to `${VOICE_AGENT_API_URL}/voice/numbers/provision/submit`, which does not exist (Telnyx-era, removed 2026-07-16). Confirm nothing references it: `grep -rn "provision/submit" src/`.
- [ ] **Step 2:** `POST .../request` — `getShopId` → `hasActiveSubscription` → **402** if not → stream the multipart body to voice-booking preserving `Content-Type` (copy the pattern from the route being deleted; that part was correct).
- [ ] **Step 3:** `GET .../status` — same gate, returns request status, violations, number, health.
- [ ] **Step 4:** Tests: 402 without a plan and **no upstream call**; forwards with a plan; upstream failure maps cleanly.
- [ ] **Step 5:** `npx tsc --noEmit && npx vitest run`, commit.

---

## Task 11 (webapp): Inbox → Configurazione → Canali

**Files:** Create `src/components/inbox/channels/NumberPanel.tsx`, `NumberRequestForm.tsx`; Modify `ConfigurationTab.tsx`, `src/i18n/{it,en,es}.ts`

- [ ] **Step 1:** Implement the seven states from design §8.1 as a pure function first, so the copy logic is testable without React:

```typescript
// src/lib/numbers/request-state.ts
export const REVIEW_BUSINESS_DAYS = 3   // one place, correct it once real times are known

export type RequestView =
  | { kind: 'no_plan' }
  | { kind: 'form' }
  | { kind: 'violations'; violations: { friendly_name: string; description: string }[] }
  | { kind: 'pending'; expectedBy: Date }
  | { kind: 'overdue' }
  | { kind: 'rejected'; reason: string }
  | { kind: 'approved' }
  | { kind: 'active'; number: string; health: 'unknown' | 'green' | 'red'; detail: string | null }

export function addBusinessDays(from: Date, n: number): Date { /* skip Sat/Sun */ }
export function viewFor(input: {...}, now: Date): RequestView
```

Test it: `pending_review` submitted 1 business day ago → `pending` with the right `expectedBy`; submitted 5 business days ago → `overdue`, **not** a stale past date; a Friday submission skips the weekend.

- [ ] **Step 2:** The form: business name prefilled from `shops.name`, contact email prefilled from the owner, file input for the *visura camerale*. State plainly, before they start, that Twilio review takes a few days and what the document is — the friction is acceptable, being surprised by it is not.
- [ ] **Step 3:** i18n in all three locale files — all three carry this key set and a missing key is a runtime gap.
- [ ] **Step 4:** No new `role-visibility.ts` key: `inbox_configuration` is already blocked for `member`, so provisioning is owner-only for free. Confirm that is still true rather than assuming.
- [ ] **Step 5:** `npx tsc --noEmit && npx vitest run && npm run lint`, commit.

---

## Task 12: Live-DB tests

**Files:** Create `tests/live_db/test_number_requests_live.py`

- [ ] Two `upsert_request` calls leave one row · `insert_telephony` twice returns a row then `None` · health columns round-trip · everything cleaned up in fixture teardown, verified with a follow-up count. Run with `TEST_DATABASE_URL` sourced from webapp's `.env.local` (never pasted). Commit.

---

## Task 13: End-to-end verification against the real Twilio account

Automated tests fake the provider. This is the run that proves the flow.

- [ ] **Step 1 (safe, repeatable):** read-only regulation check from Task 3 Step 3.
- [ ] **Step 2 (safe):** create a real End-User, document and bundle for a test shop, and **evaluate** it. Evaluation is free and non-destructive. Confirm a compliant result with a real *visura camerale*, and that a deliberately empty `business_name` produces violations that render in the UI.
- [ ] **Step 3 (costs money — owner-triggered only, never in CI):** submit the bundle, wait for approval, then run the tick and confirm exactly one number is purchased and `shop_telephony` has one row. **A number is ~$3/mo and releasing it is manual.**
- [ ] **Step 4:** Break it on purpose — point `voice_url` at a wrong host in the Twilio console, run the tick, confirm the semaphore goes **red** with `webhook_drift`, then restore and confirm it returns green.
- [ ] **Step 5:** Record the outcome in `CLAUDE.md`.

---

## Task 14: Documentation

- [ ] **voice-booking:** `docs/knowledge/{architecture,database,providers,api}.md` + a `CLAUDE.md` entry recording that the 2026-07-16 one-bundle-for-all model is **superseded** — Twilio's ISV rules forbid reusing our business info in customer bundles — and that Estonia Mobile is business-only with a commercial-register extract required per salon.
- [ ] **webapp:** `docs/knowledge/{features,providers,architecture,decisions}.md`, including the first subscription gate and the deleted dead route.
- [ ] Commit in both repos.

---

## Definition of done

- [ ] voice-booking unit suite green, no regressions against the current baseline
- [ ] webapp `tsc` + `vitest` + `lint` clean
- [ ] live-DB tests pass and leave zero rows
- [ ] real-Twilio regulation check passes
- [ ] one real bundle evaluated compliant against the live API
- [ ] semaphore proven to flip red and back on a real webhook change
- [ ] docs updated in both repos, `CLAUDE.md` entry written

## Out of scope

WhatsApp sender registration · the voice/forwarding upgrade · number release when a subscription lapses (design §11 — a product decision, and it becomes real the first time someone churns) · bundle updates when a regulation changes (Twilio's Bundle Copies flow) · any tier distinction above "has a plan".
