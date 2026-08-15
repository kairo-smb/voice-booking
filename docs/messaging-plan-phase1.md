# Phase 1 — SMS marketing send: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing marketing-copy generator into something that actually sends — one personalised SMS to one consenting customer, from the shop's own Twilio DID, billed as AI credits at 2× the Twilio cost.

**Architecture:** A new `sms` schema in the shared Neon DB, owned by voice-booking. Sending lives entirely in voice-booking (`booking_engine/services/messaging/`), exposed as `POST /api/v1/sms/send` behind the control-plane token. The webapp keeps all salon-facing UI: `RetentionMessageModal` gains an "Invia SMS" action that calls a thin webapp route, which re-checks consent and forwards to voice-booking. Exactly one credit-debit path (voice-booking), exactly one consent source of truth (`business_app_core.customers`).

**Tech Stack:** Python 3 / FastAPI / asyncpg / `twilio` SDK (voice-booking); Next.js / TypeScript / postgres.js (webapp); Postgres 17 on Neon.

**Branches:** voice-booking → `QA` (current). webapp → `dev_messaging_sms` (created from `QA`).

**Reference:** `docs/messaging-design.md` §1–7, §12 Phase 1, §14.

---

## File Structure

**voice-booking** (branch `QA`)

| File | Responsibility |
|---|---|
| `booking_engine/db/sql/11_sms_schema.sql` | create schema `sms` + 3 tables |
| `booking_engine/services/messaging/__init__.py` | package marker |
| `booking_engine/services/messaging/gsm7.py` | encoding detection, lossless sanitisation, segment count — pure |
| `booking_engine/services/messaging/send_credits.py` | Twilio USD → credits at 2× — pure |
| `booking_engine/services/messaging/sms_send.py` | consent/opt-out gate, build, debit, Twilio call, persist |
| `booking_engine/services/messaging/sms_inbound.py` | STOP keyword parsing + consent withdrawal |
| `booking_engine/db/sms_queries.py` | all SQL for the `sms` schema |
| `booking_engine/api/routes/sms.py` | `POST /sms/send`, `/sms/webhook/inbound`, `/sms/webhook/status` |
| `booking_engine/db/token_basket_queries.py` | *(modify)* fail-closed debit + message FK columns |
| `booking_engine/api/app.py` | *(modify)* register the router |
| `tests/booking_engine/test_gsm7.py` | pure unit tests |
| `tests/booking_engine/test_send_credits.py` | pure unit tests |
| `tests/booking_engine/test_sms_send.py` | gate + orchestration, DB mocked |
| `tests/booking_engine/test_routes/test_sms_routes.py` | route auth + wiring |
| `tests/live_db/test_sms_live.py` | real schema, real consent rows |

**webapp** (branch `dev_messaging_sms`)

| File | Responsibility |
|---|---|
| `src/lib/db/sql/46_ai_token_log_message_fk.sql` | 2 nullable columns on `ai_token_log` |
| `src/lib/messaging/sms-preview.ts` | segment/credit preview — mirrors `gsm7.py`, preview only |
| `src/app/api/v1/hair-salon/customers/[id]/send-sms/route.ts` | consent re-check + forward to voice-booking |
| `src/components/marketing/customers/RetentionMessageModal.tsx` | *(modify)* Invia SMS + cost line |
| `src/i18n/it.ts`, `src/i18n/en.ts` | *(modify)* tab rename + new strings |
| `src/lib/messaging/__tests__/sms-preview.test.ts` | preview unit tests |
| `src/app/api/v1/hair-salon/customers/[id]/send-sms/route.test.ts` | route tests |

---

## Task 1: GSM-7 encoding and segment counting

Cost depends on encoding: GSM-7 fits 160 chars in one segment, UCS-2 only 70. Italian accented lowercase (`à è é ì ò ù`) **is** in GSM-7 and is free; curly quotes, dashes, ellipsis and uppercase accents are not, and silently triple the bill.

**Files:**
- Create: `booking_engine/services/messaging/__init__.py`
- Create: `booking_engine/services/messaging/gsm7.py`
- Test: `tests/booking_engine/test_gsm7.py`

- [ ] **Step 1: Create the package marker**

```bash
mkdir -p booking_engine/services/messaging
printf '"""SMS and WhatsApp sending. See docs/messaging-design.md."""\n' \
  > booking_engine/services/messaging/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/booking_engine/test_gsm7.py`:

```python
from booking_engine.services.messaging.gsm7 import sanitize, encode_info


def test_italian_accents_are_gsm7_and_free():
    # à è é ì ò ù are in the GSM 03.38 alphabet — no UCS-2 penalty.
    info = encode_info("Ciao Giulia, è passato un po'. Ti va un caffè però?")
    assert info.encoding == "gsm7"
    assert info.segments == 1


def test_curly_quote_is_transliterated_not_upgraded():
    # The LLM writes ’ ; left alone it would force UCS-2 and halve the segment.
    raw = "Ciao Giulia, com’è andata?"
    assert sanitize(raw) == "Ciao Giulia, com'è andata?"
    assert encode_info(sanitize(raw)).encoding == "gsm7"


def test_uppercase_e_grave_becomes_apostrophe_form():
    # È is NOT in GSM-7 (only É is). E' is the standard Italian typewriter form.
    assert sanitize("È ora!") == "E' ora!"


def test_emoji_forces_ucs2_and_is_not_stripped():
    # Content is never silently removed — the caller sees the real cost instead.
    info = encode_info(sanitize("Ciao Giulia 💇"))
    assert info.encoding == "ucs2"
    assert "💇" in info.text


def test_gsm7_segment_boundaries():
    assert encode_info("a" * 160).segments == 1
    # Over 160, concatenation headers cut each segment to 153.
    assert encode_info("a" * 161).segments == 2
    assert encode_info("a" * 306).segments == 2
    assert encode_info("a" * 307).segments == 3


def test_ucs2_segment_boundaries():
    assert encode_info("💇" * 35).segments == 1   # 70 UTF-16 units
    assert encode_info("💇" * 36).segments == 2   # 72 > 70


def test_extended_chars_count_double():
    # € { } [ ] ~ ^ | live in the GSM extension table: 2 septets each.
    info = encode_info("€" * 80)
    assert info.encoding == "gsm7"
    assert info.segments == 1     # 80 × 2 = 160 septets = exactly one segment
    assert encode_info("€" * 81).segments == 2
```

- [ ] **Step 3: Run it and watch it fail**

Run: `python -m pytest tests/booking_engine/test_gsm7.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'booking_engine.services.messaging.gsm7'`

- [ ] **Step 4: Implement**

Create `booking_engine/services/messaging/gsm7.py`:

```python
"""GSM 03.38 encoding detection and segment counting.

Segment count is the billing unit: an SMS is 160 chars in GSM-7 but only 70 in
UCS-2, so one stray curly quote from the LLM can triple the price of a campaign.
`sanitize` removes typographic noise losslessly; anything left that GSM-7 can't
represent (emoji) is kept and the caller is told the real cost instead.
"""
from __future__ import annotations

from dataclasses import dataclass

# GSM 03.38 default alphabet. Note it already contains the Italian lowercase
# accented vowels (à è é ì ò ù) — those are free, contrary to folklore.
_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Extension table: each costs two septets (ESC + char).
_EXTENDED = "^{}\\[~]|€"

# Lossless replacements for characters an LLM emits that GSM-7 lacks.
# Uppercase accented vowels other than É have no GSM-7 form; Italian typewriter
# convention writes them as letter + apostrophe.
_TRANSLITERATE = {
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ",
    "È": "E'", "À": "A'", "Ì": "I'", "Ò": "O'", "Ù": "U'",
}

GSM7_SINGLE, GSM7_MULTI = 160, 153
UCS2_SINGLE, UCS2_MULTI = 70, 67


@dataclass(frozen=True)
class EncodeInfo:
    text: str
    encoding: str   # 'gsm7' | 'ucs2'
    units: int      # septets (gsm7) or UTF-16 code units (ucs2)
    segments: int


def sanitize(text: str) -> str:
    """Replace non-GSM-7 typography with equivalent GSM-7 characters.

    Lossless by construction: only characters with an accepted plain-text
    equivalent are in the table. Emoji and other true non-GSM content are left
    alone — silently deleting words from a message addressed to a named
    customer is worse than charging for a second segment.
    """
    return "".join(_TRANSLITERATE.get(ch, ch) for ch in text)


def _septets(text: str) -> int | None:
    """Septet count, or None if the text can't be represented in GSM-7."""
    total = 0
    for ch in text:
        if ch in _BASIC:
            total += 1
        elif ch in _EXTENDED:
            total += 2
        else:
            return None
    return total


def encode_info(text: str) -> EncodeInfo:
    """Report the encoding, unit count and billable segment count for `text`."""
    septets = _septets(text)
    if septets is not None:
        single, multi, units, encoding = GSM7_SINGLE, GSM7_MULTI, septets, "gsm7"
    else:
        # UCS-2 bills per UTF-16 code unit, so astral chars (most emoji) cost 2.
        units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
        single, multi, encoding = UCS2_SINGLE, UCS2_MULTI, "ucs2"

    if units == 0:
        segments = 1
    elif units <= single:
        segments = 1
    else:
        segments = -(-units // multi)   # ceil
    return EncodeInfo(text=text, encoding=encoding, units=units, segments=segments)
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/booking_engine/test_gsm7.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add booking_engine/services/messaging/ tests/booking_engine/test_gsm7.py
git commit -m "feat(sms): GSM-7 sanitisation and segment counting"
```

---

## Task 2: Twilio cost → credits at 2×

**Files:**
- Create: `booking_engine/services/messaging/send_credits.py`
- Test: `tests/booking_engine/test_send_credits.py`

- [ ] **Step 1: Write the failing test**

Create `tests/booking_engine/test_send_credits.py`:

```python
from booking_engine.services.messaging.send_credits import send_credits


def test_one_italian_sms_segment():
    # $0.093 × 2 × 1000 credits/USD = 186
    assert send_credits(0.093) == 186


def test_two_segments():
    assert send_credits(0.186) == 372


def test_free_stays_free():
    # A WhatsApp service message inside the 24h window costs Twilio nothing.
    # rawToUserCredits() in the webapp floors at 1; that would quietly invert
    # the economics of the whole free-form model, so this must return 0.
    assert send_credits(0.0) == 0


def test_negative_and_nonsense_are_free_not_charged():
    assert send_credits(-1.0) == 0
    assert send_credits(float("nan")) == 0


def test_rounds_up_so_a_send_is_never_free_by_rounding():
    assert send_credits(0.0001) == 1   # 0.0001 × 2 × 1000 = 0.2 → ceil 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/booking_engine/test_send_credits.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `booking_engine/services/messaging/send_credits.py`:

```python
"""Twilio pass-through cost → AI credits.

Deliberately NOT the webapp's rawToUserCredits(): that applies MARGIN = 10 (the
LLM margin) and floors at 1 credit. Both are wrong for sends — the margin here
is 2×, and a floor of 1 would charge for a free WhatsApp service message.
See docs/messaging-design.md §5.1.
"""
from __future__ import annotations

import math

MARGIN = 2
CREDITS_PER_USD = 1000


def send_credits(twilio_usd: float) -> int:
    """Credits to charge the shop for a send that cost us `twilio_usd`."""
    if not math.isfinite(twilio_usd) or twilio_usd <= 0:
        return 0
    return math.ceil(twilio_usd * MARGIN * CREDITS_PER_USD)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/booking_engine/test_send_credits.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/send_credits.py tests/booking_engine/test_send_credits.py
git commit -m "feat(sms): 2x Twilio cost to credits converter"
```

---

## Task 3: `sms` schema migration

Applied to real branches by webapp's `migrate-all.yml`, which checks this repo out read-only and runs `scripts/migrate.sh` — so the file lives here and nowhere else.

**Files:**
- Create: `booking_engine/db/sql/11_sms_schema.sql`

- [ ] **Step 1: Write the migration**

Create `booking_engine/db/sql/11_sms_schema.sql`:

```sql
-- SMS marketing sends. See docs/messaging-design.md §4.1.
-- Additive and idempotent: migrate.sh re-applies every file on every run.

CREATE SCHEMA IF NOT EXISTS sms;

CREATE TABLE IF NOT EXISTS sms.campaigns (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id     uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  name        text NOT NULL,
  status      text NOT NULL DEFAULT 'draft'
              CHECK (status IN ('draft','approved','sending','sent','cancelled')),
  approved_at timestamptz,
  approved_by uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sms.outbound_messages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id       uuid REFERENCES sms.campaigns(id) ON DELETE CASCADE,
  shop_id           uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  customer_id       uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  to_phone          text NOT NULL,
  from_number       text NOT NULL,
  body              text NOT NULL,
  segments          smallint NOT NULL,
  encoding          text NOT NULL CHECK (encoding IN ('gsm7','ucs2')),
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','sent','delivered','failed','suppressed')),
  suppressed_reason text,
  provider_sid      text,
  price_usd         numeric,
  credits_charged   integer,
  error_code        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  sent_at           timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Idempotency as a DB constraint: a campaign reaches each customer at most once
-- however many times the send job re-runs.
CREATE UNIQUE INDEX IF NOT EXISTS sms_outbound_campaign_customer_uniq
  ON sms.outbound_messages (campaign_id, customer_id)
  WHERE campaign_id IS NOT NULL AND customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS sms_outbound_provider_sid_idx
  ON sms.outbound_messages (provider_sid);

-- Suppression list of last resort: a STOP must be honoured from a phone that
-- matches no customers row (import, wrong number, deleted customer), and this
-- is also the legal evidence trail for the Garante.
CREATE TABLE IF NOT EXISTS sms.opt_outs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id          uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  phone_normalized text NOT NULL,
  keyword          text NOT NULL,
  raw_body         text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shop_id, phone_normalized)
);
```

- [ ] **Step 2: Apply it twice against a scratch DB to prove idempotency**

```bash
createdb sms_migration_check
psql -v ON_ERROR_STOP=1 sms_migration_check -c \
  'CREATE SCHEMA business_app_core;
   CREATE TABLE business_app_core.shops (id uuid PRIMARY KEY);
   CREATE TABLE business_app_core.customers (id uuid PRIMARY KEY);'
psql -v ON_ERROR_STOP=1 sms_migration_check -f booking_engine/db/sql/11_sms_schema.sql
psql -v ON_ERROR_STOP=1 sms_migration_check -f booking_engine/db/sql/11_sms_schema.sql
psql sms_migration_check -c '\dt sms.*'
dropdb sms_migration_check
```

Expected: both runs exit 0, `\dt` lists `campaigns`, `opt_outs`, `outbound_messages`.

- [ ] **Step 3: Commit**

```bash
git add booking_engine/db/sql/11_sms_schema.sql
git commit -m "feat(sms): sms schema migration"
```

---

## Task 4 (webapp): `ai_token_log` message columns

A messaging debit currently has nowhere to record what it paid for — `ai_token_log` has `voice_call_id` and nothing else. Without this, "was this send billed?" is unanswerable from the log, which is the exact bug the webapp's own `chargeActual` comment says the chat ledger row was added to fix.

**Files:**
- Create: `src/lib/db/sql/46_ai_token_log_message_fk.sql` *(webapp, branch `dev_messaging_sms`)*

- [ ] **Step 1: Write the migration**

```sql
-- Trace a credit debit back to the message that caused it, mirroring the
-- existing voice_call_id column. See voice-booking docs/messaging-design.md §5.2.
--
-- Nullable and deliberately WITHOUT foreign keys: sms.outbound_messages and
-- whatsapp.messages live in schemas owned by the voice-booking repo and are
-- migrated after this one, so a real FK would make business_app_core depend on
-- a child schema and invert migrate-all.yml's ordering.

ALTER TABLE business_app_core.ai_token_log
  ADD COLUMN IF NOT EXISTS sms_message_id      uuid,
  ADD COLUMN IF NOT EXISTS whatsapp_message_id uuid;
```

- [ ] **Step 2: Apply it against a scratch DB twice**

```bash
cd ~/Documents/kairo/webapp
createdb token_log_check
psql -v ON_ERROR_STOP=1 token_log_check -c \
  'CREATE SCHEMA business_app_core;
   CREATE TABLE business_app_core.ai_token_log (id uuid PRIMARY KEY, shop_id uuid);'
psql -v ON_ERROR_STOP=1 token_log_check -f src/lib/db/sql/46_ai_token_log_message_fk.sql
psql -v ON_ERROR_STOP=1 token_log_check -f src/lib/db/sql/46_ai_token_log_message_fk.sql
psql token_log_check -c '\d business_app_core.ai_token_log'
dropdb token_log_check
```

Expected: both runs exit 0; `\d` shows `sms_message_id` and `whatsapp_message_id`.

- [ ] **Step 3: Commit (webapp)**

```bash
cd ~/Documents/kairo/webapp
git add src/lib/db/sql/46_ai_token_log_message_fk.sql
git commit -m "feat(messaging): trace credit debits to sms/whatsapp messages"
```

---

## Task 5: Fail-closed credit debit

`insert_debit_event` drains the basket and proceeds when short — correct for voice (a live call can't be un-answered) and wrong for messaging (a message can simply wait).

**Files:**
- Modify: `booking_engine/db/token_basket_queries.py`
- Test: `tests/booking_engine/test_sms_send.py` (created here, extended in Task 7)

- [ ] **Step 1: Write the failing test**

Create `tests/booking_engine/test_sms_send.py`:

```python
import pytest
from uuid import uuid4

from booking_engine.db import token_basket_queries as tbq

SHOP = uuid4()


@pytest.mark.asyncio
async def test_debit_refused_when_balance_is_short(monkeypatch):
    calls = []
    async def fake_balance(shop_id):
        return 100
    async def fake_insert(**kw):
        calls.append(kw)
    monkeypatch.setattr(tbq, "get_balance", fake_balance)
    monkeypatch.setattr(tbq, "insert_debit_event", fake_insert)

    ok = await tbq.try_debit_for_message(shop_id=SHOP, credits=186)

    assert ok is False
    assert calls == []          # nothing was debited


@pytest.mark.asyncio
async def test_debit_succeeds_and_records_the_message(monkeypatch):
    calls = []
    async def fake_balance(shop_id):
        return 1000
    async def fake_insert(**kw):
        calls.append(kw)
    monkeypatch.setattr(tbq, "get_balance", fake_balance)
    monkeypatch.setattr(tbq, "insert_debit_event", fake_insert)

    msg_id = uuid4()
    ok = await tbq.try_debit_for_message(shop_id=SHOP, credits=186, sms_message_id=msg_id)

    assert ok is True
    assert calls[0]["tokens"] == 186
    assert calls[0]["sms_message_id"] == msg_id


@pytest.mark.asyncio
async def test_zero_credits_is_a_no_op_success(monkeypatch):
    calls = []
    async def fake_insert(**kw):
        calls.append(kw)
    monkeypatch.setattr(tbq, "insert_debit_event", fake_insert)

    assert await tbq.try_debit_for_message(shop_id=SHOP, credits=0) is True
    assert calls == []          # a free message writes no ledger row
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/booking_engine/test_sms_send.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'try_debit_for_message'`

- [ ] **Step 3: Extend `insert_debit_event` with the message columns**

In `booking_engine/db/token_basket_queries.py`, change the signature and the final INSERT:

```python
async def insert_debit_event(
    *,
    shop_id: UUID,
    tokens: int,
    source: str,  # kept for signature compatibility; we map to 'granted'/'purchased'
    voice_call_id: UUID | None,
    sms_message_id: UUID | None = None,
    whatsapp_message_id: UUID | None = None,
) -> None:
```

and replace the trailing `INSERT` with:

```python
    await execute_void(
        """
        INSERT INTO business_app_core.ai_token_log
            (shop_id, credits_used, source, voice_call_id,
             sms_message_id, whatsapp_message_id, created_at)
        VALUES ($1, $2, $3::ai_credit_source, $4, $5, $6, now())
        """,
        shop_id, amount, debit_source, voice_call_id,
        sms_message_id, whatsapp_message_id,
    )
```

- [ ] **Step 4: Add the fail-closed wrapper**

Append to `booking_engine/db/token_basket_queries.py`:

```python
async def try_debit_for_message(
    *,
    shop_id: UUID,
    credits: int,
    sms_message_id: UUID | None = None,
    whatsapp_message_id: UUID | None = None,
) -> bool:
    """Debit for an outbound message, or refuse. Returns False without debiting.

    Unlike insert_debit_event (the voice path) this never overdraws: a live call
    can't be un-answered, but a message can simply not be sent. See
    docs/messaging-design.md §5.2.
    """
    if credits <= 0:
        return True   # a free message writes no ledger row
    # ponytail: check-then-debit, not one locked transaction. Two concurrent
    # sends could overdraw by one message; sends are owner-triggered and
    # effectively serial today. Wrap both in a single FOR UPDATE tx if bulk
    # campaigns ever run concurrently.
    if await get_balance(shop_id) < credits:
        return False
    await insert_debit_event(
        shop_id=shop_id,
        tokens=credits,
        source="granted",
        voice_call_id=None,
        sms_message_id=sms_message_id,
        whatsapp_message_id=whatsapp_message_id,
    )
    return True
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/booking_engine/test_sms_send.py -v`
Expected: 3 passed.

- [ ] **Step 6: Confirm the voice path still works**

Run: `python -m pytest tests/ -k "token_basket or balance" -v`
Expected: PASS — the two new parameters default to `None`, so existing callers are unaffected.

- [ ] **Step 7: Commit**

```bash
git add booking_engine/db/token_basket_queries.py tests/booking_engine/test_sms_send.py
git commit -m "feat(sms): fail-closed credit debit for outbound messages"
```

---

## Task 6: `sms` schema queries

**Files:**
- Create: `booking_engine/db/sms_queries.py`

- [ ] **Step 1: Implement**

No test of its own — these are thin SQL wrappers with no branching, exercised by Task 7's tests (mocked) and Task 11's (real DB). Writing unit tests that assert SQL strings tests nothing but the string.

Create `booking_engine/db/sms_queries.py`:

```python
"""SQL for the `sms` schema. See docs/messaging-design.md §4.1."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one, execute_void


async def get_shop_sender_number(shop_id: UUID) -> str | None:
    """The shop's own Twilio DID — the same number that answers voice calls."""
    row = await execute_one(
        "SELECT kairo_number FROM voice_agent.shop_telephony WHERE shop_id = $1",
        shop_id,
    )
    return row["kairo_number"] if row else None


async def get_customer_for_send(shop_id: UUID, customer_id: UUID) -> dict | None:
    """Consent + phone for one customer, scoped to the shop (never cross-shop)."""
    return await execute_one(
        """
        SELECT id, full_name, phone, phone_normalized,
               marketing_consent, marketing_consent_granted_at,
               marketing_consent_withdrawn_at
        FROM business_app_core.customers
        WHERE id = $1 AND shop_id = $2
        """,
        customer_id, shop_id,
    )


async def is_opted_out(shop_id: UUID, phone_normalized: str) -> bool:
    row = await execute_one(
        "SELECT 1 AS hit FROM sms.opt_outs WHERE shop_id = $1 AND phone_normalized = $2",
        shop_id, phone_normalized,
    )
    return row is not None


async def insert_outbound(
    *,
    shop_id: UUID,
    customer_id: UUID | None,
    to_phone: str,
    from_number: str,
    body: str,
    segments: int,
    encoding: str,
    status: str,
    suppressed_reason: str | None = None,
) -> UUID:
    row = await execute_one(
        """
        INSERT INTO sms.outbound_messages
            (shop_id, customer_id, to_phone, from_number, body,
             segments, encoding, status, suppressed_reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        RETURNING id
        """,
        shop_id, customer_id, to_phone, from_number, body,
        segments, encoding, status, suppressed_reason,
    )
    return row["id"]


async def mark_sent(
    *, message_id: UUID, provider_sid: str, price_usd: float | None, credits: int
) -> None:
    await execute_void(
        """
        UPDATE sms.outbound_messages
        SET status = 'sent', provider_sid = $2, price_usd = $3,
            credits_charged = $4, sent_at = now(), updated_at = now()
        WHERE id = $1
        """,
        message_id, provider_sid, price_usd, credits,
    )


async def mark_failed(*, message_id: UUID, error_code: str) -> None:
    await execute_void(
        """
        UPDATE sms.outbound_messages
        SET status = 'failed', error_code = $2, updated_at = now()
        WHERE id = $1
        """,
        message_id, error_code,
    )


async def update_status_by_sid(
    *, provider_sid: str, status: str, price_usd: float | None, error_code: str | None
) -> None:
    """Twilio status callback. Only advances to terminal states we recognise."""
    await execute_void(
        """
        UPDATE sms.outbound_messages
        SET status = $2,
            price_usd = COALESCE($3, price_usd),
            error_code = COALESCE($4, error_code),
            updated_at = now()
        WHERE provider_sid = $1
        """,
        provider_sid, status, price_usd, error_code,
    )


async def record_opt_out(
    *, shop_id: UUID, phone_normalized: str, keyword: str, raw_body: str
) -> None:
    """Suppression list entry. Idempotent — a second STOP is not an error."""
    await execute_void(
        """
        INSERT INTO sms.opt_outs (shop_id, phone_normalized, keyword, raw_body)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (shop_id, phone_normalized) DO NOTHING
        """,
        shop_id, phone_normalized, keyword, raw_body,
    )


async def withdraw_marketing_consent(*, shop_id: UUID, phone_normalized: str) -> int:
    """Reflect the STOP in the single source of truth.

    Without this the webapp keeps listing the customer as consenting while SMS
    silently suppresses them. Returns the number of rows updated (0 when the
    phone matches no customer — the opt_outs row still stands alone).
    """
    row = await execute_one(
        """
        WITH updated AS (
            UPDATE business_app_core.customers
            SET marketing_consent = false,
                marketing_consent_withdrawn_at = now(),
                marketing_consent_source = 'sms_stop',
                updated_at = now()
            WHERE shop_id = $1 AND phone_normalized = $2
              AND marketing_consent_withdrawn_at IS NULL
            RETURNING 1
        )
        SELECT count(*) AS n FROM updated
        """,
        shop_id, phone_normalized,
    )
    return int(row["n"]) if row else 0


async def get_shop_by_sender_number(number: str) -> UUID | None:
    """Inbound webhooks identify the shop by the number that was texted."""
    row = await execute_one(
        "SELECT shop_id FROM voice_agent.shop_telephony WHERE kairo_number = $1",
        number,
    )
    return row["shop_id"] if row else None
```

- [ ] **Step 2: Import-check**

Run: `python -c "import booking_engine.db.sms_queries"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add booking_engine/db/sms_queries.py
git commit -m "feat(sms): sms schema query layer"
```

---

## Task 7: The send service

**Files:**
- Create: `booking_engine/services/messaging/sms_send.py`
- Modify: `tests/booking_engine/test_sms_send.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/booking_engine/test_sms_send.py`:

```python
from datetime import datetime, timezone

from booking_engine.services.messaging import sms_send

CUSTOMER = uuid4()
NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _consenting_customer(**over):
    base = {
        "id": CUSTOMER,
        "full_name": "Giulia Rossi",
        "phone": "+393331234567",
        "phone_normalized": "+393331234567",
        "marketing_consent": True,
        "marketing_consent_granted_at": NOW,
        "marketing_consent_withdrawn_at": None,
    }
    base.update(over)
    return base


class _Recorder:
    """Collects what the service tried to do, so tests assert on effects."""
    def __init__(self):
        self.inserted = None
        self.sent = None
        self.failed = None
        self.debited = None


def _wire(monkeypatch, rec, *, customer, opted_out=False, sender="+37251234567",
          debit_ok=True, twilio=None):
    async def q_sender(shop_id): return sender
    async def q_customer(shop_id, customer_id): return customer
    async def q_opted(shop_id, phone): return opted_out
    async def q_insert(**kw):
        rec.inserted = kw
        return uuid4()
    async def q_sent(**kw): rec.sent = kw
    async def q_failed(**kw): rec.failed = kw
    async def q_debit(**kw):
        rec.debited = kw
        return debit_ok

    monkeypatch.setattr(sms_send.sms_queries, "get_shop_sender_number", q_sender)
    monkeypatch.setattr(sms_send.sms_queries, "get_customer_for_send", q_customer)
    monkeypatch.setattr(sms_send.sms_queries, "is_opted_out", q_opted)
    monkeypatch.setattr(sms_send.sms_queries, "insert_outbound", q_insert)
    monkeypatch.setattr(sms_send.sms_queries, "mark_sent", q_sent)
    monkeypatch.setattr(sms_send.sms_queries, "mark_failed", q_failed)
    monkeypatch.setattr(sms_send.tbq, "try_debit_for_message", q_debit)
    monkeypatch.setattr(
        sms_send, "_twilio_send",
        twilio or (lambda **kw: sms_send.TwilioResult(sid="SM123", price_usd=0.093)),
    )


@pytest.mark.asyncio
async def test_opt_out_footer_is_appended_server_side(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao Giulia, ti aspettiamo!"
    )

    assert rec.inserted["body"].endswith(sms_send.OPT_OUT_FOOTER)


@pytest.mark.asyncio
async def test_withdrawn_consent_is_suppressed_not_sent(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec,
          customer=_consenting_customer(marketing_consent_withdrawn_at=NOW))

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "no_consent"
    assert rec.inserted["status"] == "suppressed"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_opted_out_phone_is_suppressed(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), opted_out=True)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.reason == "opted_out"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_insufficient_credits_blocks_the_send(monkeypatch):
    # Never send something that can't be billed.
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), debit_ok=False)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "insufficient_credits"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_curly_quote_from_the_llm_does_not_double_the_bill(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao Giulia, com’è andata?"
    )

    assert rec.inserted["encoding"] == "gsm7"
    assert "’" not in rec.inserted["body"]


@pytest.mark.asyncio
async def test_successful_send_charges_two_times_twilio(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao Giulia!"
    )

    assert result.ok is True
    assert rec.debited["credits"] == 186      # 0.093 × 2 × 1000
    assert rec.sent["provider_sid"] == "SM123"


@pytest.mark.asyncio
async def test_twilio_failure_marks_the_row_failed(monkeypatch):
    rec = _Recorder()
    def boom(**kw):
        raise RuntimeError("21610 unsubscribed recipient")
    _wire(monkeypatch, rec, customer=_consenting_customer(), twilio=boom)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "provider_error"
    assert rec.failed is not None


@pytest.mark.asyncio
async def test_shop_without_a_number_cannot_send(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), sender=None)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.reason == "no_sender_number"
```

- [ ] **Step 2: Run and watch it fail**

Run: `python -m pytest tests/booking_engine/test_sms_send.py -v`
Expected: FAIL — `ImportError: cannot import name 'sms_send'`

- [ ] **Step 3: Implement**

Create `booking_engine/services/messaging/sms_send.py`:

```python
"""Send one marketing SMS to one customer.

Order matters and is a trust boundary, not a style choice: consent is re-checked
here even though the webapp checks it twice, because this is the last code that
runs before a named individual receives marketing. Credits are debited BEFORE
the provider call — a send we can't bill must not happen, and an unbilled send
is worse than a refunded one. See docs/messaging-design.md §6.3.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from twilio.rest import Client

from booking_engine.db import sms_queries
from booking_engine.db import token_basket_queries as tbq
from booking_engine.services.messaging.gsm7 import encode_info, sanitize
from booking_engine.services.messaging.send_credits import send_credits
from booking_engine.services.phone_normalize import normalize_e164

# Legally required in every marketing message (Garante). Appended server-side,
# never left to the LLM that wrote the body.
OPT_OUT_FOOTER = " Rispondi STOP per non ricevere piu'."

# Twilio list price for Italy, used only to pre-charge. The status callback
# writes the real price; this is never the billed figure of record.
_ESTIMATED_USD_PER_SEGMENT = 0.093


@dataclass(frozen=True)
class TwilioResult:
    sid: str
    price_usd: float | None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    reason: str            # 'sent' | why it didn't go
    message_id: UUID | None = None
    segments: int = 0
    credits: int = 0


def _has_active_consent(customer: dict) -> bool:
    """Mirrors the webapp's hasActiveMarketingConsent() exactly."""
    return (
        bool(customer.get("marketing_consent"))
        and customer.get("marketing_consent_granted_at") is not None
        and customer.get("marketing_consent_withdrawn_at") is None
    )


def _twilio_send(*, to: str, from_: str, body: str,
                 account_sid: str, auth_token: str) -> TwilioResult:
    """Blocking Twilio call. Wrapped in a thread by the caller."""
    client = Client(account_sid, auth_token)
    msg = client.messages.create(to=to, from_=from_, body=body)
    return TwilioResult(
        sid=msg.sid,
        price_usd=abs(float(msg.price)) if getattr(msg, "price", None) else None,
    )


async def send_marketing_sms(
    *,
    shop_id: UUID,
    customer_id: UUID,
    body: str,
    account_sid: str = "",
    auth_token: str = "",
) -> SendResult:
    """Gate, build, bill, send. Every refusal is persisted, never silent."""
    sender = await sms_queries.get_shop_sender_number(shop_id)
    if not sender:
        return SendResult(ok=False, reason="no_sender_number")

    customer = await sms_queries.get_customer_for_send(shop_id, customer_id)
    if not customer:
        return SendResult(ok=False, reason="customer_not_found")

    phone = customer.get("phone_normalized") or normalize_e164(customer.get("phone"))
    text = sanitize(body.strip()) + OPT_OUT_FOOTER
    info = encode_info(text)

    async def _suppress(reason: str) -> SendResult:
        # Recorded, not dropped: "why did Giulia not get it?" must be answerable.
        mid = await sms_queries.insert_outbound(
            shop_id=shop_id, customer_id=customer_id, to_phone=phone or "",
            from_number=sender, body=text, segments=info.segments,
            encoding=info.encoding, status="suppressed", suppressed_reason=reason,
        )
        return SendResult(ok=False, reason=reason, message_id=mid)

    if not phone:
        return await _suppress("no_phone")
    if not _has_active_consent(customer):
        return await _suppress("no_consent")
    if await sms_queries.is_opted_out(shop_id, phone):
        return await _suppress("opted_out")

    message_id = await sms_queries.insert_outbound(
        shop_id=shop_id, customer_id=customer_id, to_phone=phone,
        from_number=sender, body=text, segments=info.segments,
        encoding=info.encoding, status="queued",
    )

    # Estimated from list price; the status webhook reconciles with the real one.
    credits = send_credits(_ESTIMATED_USD_PER_SEGMENT * info.segments)
    if not await tbq.try_debit_for_message(
        shop_id=shop_id, credits=credits, sms_message_id=message_id
    ):
        await sms_queries.mark_failed(message_id=message_id, error_code="insufficient_credits")
        return SendResult(ok=False, reason="insufficient_credits", message_id=message_id)

    try:
        result = await asyncio.to_thread(
            _twilio_send, to=phone, from_=sender, body=text,
            account_sid=account_sid, auth_token=auth_token,
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are data, not crashes
        await sms_queries.mark_failed(message_id=message_id, error_code=str(exc)[:200])
        return SendResult(ok=False, reason="provider_error", message_id=message_id)

    await sms_queries.mark_sent(
        message_id=message_id, provider_sid=result.sid,
        price_usd=result.price_usd, credits=credits,
    )
    return SendResult(
        ok=True, reason="sent", message_id=message_id,
        segments=info.segments, credits=credits,
    )
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/booking_engine/test_sms_send.py -v`
Expected: 11 passed (3 from Task 5 + 8 here).

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/sms_send.py tests/booking_engine/test_sms_send.py
git commit -m "feat(sms): marketing send service with consent gate and fail-closed billing"
```

---

## Task 8: STOP handling

**Files:**
- Create: `booking_engine/services/messaging/sms_inbound.py`
- Test: `tests/booking_engine/test_sms_inbound.py`

- [ ] **Step 1: Write the failing test**

Create `tests/booking_engine/test_sms_inbound.py`:

```python
import pytest
from uuid import uuid4

from booking_engine.services.messaging import sms_inbound

SHOP = uuid4()


def test_stop_keywords_are_recognised_case_and_space_insensitively():
    for raw in ["STOP", "stop", "  Stop  ", "CANCELLA", "ALT", "unsubscribe"]:
        assert sms_inbound.parse_stop_keyword(raw) is not None


def test_ordinary_replies_are_not_opt_outs():
    # "Stop" must match the word, not appear anywhere in a sentence — a customer
    # replying "non fermatevi, stop mai!" has not opted out.
    for raw in ["Grazie mille!", "Va bene per giovedi", "non fermatevi, stop mai!"]:
        assert sms_inbound.parse_stop_keyword(raw) is None


def test_empty_body_is_not_an_opt_out():
    assert sms_inbound.parse_stop_keyword("") is None
    assert sms_inbound.parse_stop_keyword(None) is None


@pytest.mark.asyncio
async def test_stop_writes_both_the_list_and_the_consent_row(monkeypatch):
    recorded, withdrawn = {}, {}

    async def q_shop(number): return SHOP
    async def q_record(**kw): recorded.update(kw)
    async def q_withdraw(**kw):
        withdrawn.update(kw)
        return 1

    monkeypatch.setattr(sms_inbound.sms_queries, "get_shop_by_sender_number", q_shop)
    monkeypatch.setattr(sms_inbound.sms_queries, "record_opt_out", q_record)
    monkeypatch.setattr(sms_inbound.sms_queries, "withdraw_marketing_consent", q_withdraw)

    handled = await sms_inbound.handle_inbound(
        to_number="+37251234567", from_number="+393331234567", body="STOP"
    )

    assert handled is True
    assert recorded["keyword"] == "STOP"
    assert recorded["phone_normalized"] == "+393331234567"
    # Both writes: the list alone would leave the webapp showing consent.
    assert withdrawn["phone_normalized"] == "+393331234567"


@pytest.mark.asyncio
async def test_stop_from_an_unknown_phone_still_suppresses(monkeypatch):
    # No customers row to update — the opt_outs entry must stand on its own.
    recorded = {}

    async def q_shop(number): return SHOP
    async def q_record(**kw): recorded.update(kw)
    async def q_withdraw(**kw): return 0

    monkeypatch.setattr(sms_inbound.sms_queries, "get_shop_by_sender_number", q_shop)
    monkeypatch.setattr(sms_inbound.sms_queries, "record_opt_out", q_record)
    monkeypatch.setattr(sms_inbound.sms_queries, "withdraw_marketing_consent", q_withdraw)

    assert await sms_inbound.handle_inbound(
        to_number="+37251234567", from_number="+393339999999", body="stop"
    ) is True
    assert recorded["phone_normalized"] == "+393339999999"


@pytest.mark.asyncio
async def test_unroutable_number_is_ignored(monkeypatch):
    async def q_shop(number): return None
    monkeypatch.setattr(sms_inbound.sms_queries, "get_shop_by_sender_number", q_shop)

    assert await sms_inbound.handle_inbound(
        to_number="+37259999999", from_number="+393331234567", body="STOP"
    ) is False
```

- [ ] **Step 2: Run and watch it fail**

Run: `python -m pytest tests/booking_engine/test_sms_inbound.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

Create `booking_engine/services/messaging/sms_inbound.py`:

```python
"""Inbound SMS — opt-out handling.

Twilio's automatic STOP handling covers US/Canada long codes only; an Estonian
number gets none of it, so honouring STOP is entirely our job and is a legal
requirement, not a nicety. See docs/messaging-design.md §6.3.
"""
from __future__ import annotations

import re

from booking_engine.db import sms_queries
from booking_engine.services.phone_normalize import normalize_e164

# Italian and English opt-out words. Matched as the WHOLE message (modulo
# whitespace and punctuation), never as a substring: "non fermatevi, stop mai!"
# is not an opt-out and unsubscribing someone who didn't ask is its own harm.
_STOP_WORDS = {"stop", "alt", "cancella", "cancellami", "unsubscribe", "stopall"}
_STRIP = re.compile(r"^[\s\W_]+|[\s\W_]+$", re.UNICODE)


def parse_stop_keyword(body: str | None) -> str | None:
    """Return the matched keyword as the customer typed it, or None."""
    if not body:
        return None
    cleaned = _STRIP.sub("", body)
    if cleaned.lower() in _STOP_WORDS:
        return cleaned
    return None


async def handle_inbound(*, to_number: str, from_number: str, body: str | None) -> bool:
    """Process one inbound SMS. Returns True when it was an opt-out we acted on."""
    keyword = parse_stop_keyword(body)
    if not keyword:
        return False

    shop_id = await sms_queries.get_shop_by_sender_number(to_number)
    if not shop_id:
        return False

    phone = normalize_e164(from_number) or from_number

    # Two writes, both required. The opt_outs row suppresses even when no
    # customer matches; the consent update keeps the webapp honest.
    await sms_queries.record_opt_out(
        shop_id=shop_id, phone_normalized=phone,
        keyword=keyword.upper(), raw_body=(body or "")[:500],
    )
    await sms_queries.withdraw_marketing_consent(shop_id=shop_id, phone_normalized=phone)
    return True
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/booking_engine/test_sms_inbound.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/sms_inbound.py tests/booking_engine/test_sms_inbound.py
git commit -m "feat(sms): STOP keyword opt-out handling"
```

---

## Task 9: Routes

**Files:**
- Create: `booking_engine/api/routes/sms.py`
- Modify: `booking_engine/api/app.py`
- Test: `tests/booking_engine/test_routes/test_sms_routes.py`

- [ ] **Step 1: Read how Twilio signatures are already verified**

Run: `grep -rn "X-Twilio-Signature\|RequestValidator" booking_engine/ --include="*.py"`

Reuse whatever `voice_twiml.py` uses. Do not write a second verifier — the 2026-07-16 entry added this one specifically to close a gap, and two implementations will drift.

- [ ] **Step 2: Write the failing test**

Create `tests/booking_engine/test_routes/test_sms_routes.py`:

```python
import pytest
from uuid import uuid4
from fastapi.testclient import TestClient

from booking_engine.api.app import create_app

SHOP, CUSTOMER = str(uuid4()), str(uuid4())


@pytest.fixture
def client():
    return TestClient(create_app())


def test_send_requires_the_control_plane_token(client):
    r = client.post("/api/v1/sms/send",
                    json={"shop_id": SHOP, "customer_id": CUSTOMER, "body": "Ciao"})
    assert r.status_code in (401, 503)


def test_send_rejects_an_empty_body(client, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "s3cret")
    r = client.post("/api/v1/sms/send",
                    headers={"Authorization": "Bearer s3cret"},
                    json={"shop_id": SHOP, "customer_id": CUSTOMER, "body": "   "})
    assert r.status_code == 422


def test_suppressed_send_returns_409_with_the_reason(client, monkeypatch):
    from booking_engine.api.routes import sms as sms_routes
    from booking_engine.services.messaging.sms_send import SendResult

    monkeypatch.setenv("CONTROL_PLANE_SECRET", "s3cret")

    async def fake_send(**kw):
        return SendResult(ok=False, reason="no_consent")
    monkeypatch.setattr(sms_routes, "send_marketing_sms", fake_send)

    r = client.post("/api/v1/sms/send",
                    headers={"Authorization": "Bearer s3cret"},
                    json={"shop_id": SHOP, "customer_id": CUSTOMER, "body": "Ciao"})
    assert r.status_code == 409
    assert r.json()["detail"] == "no_consent"
```

- [ ] **Step 3: Run and watch it fail**

Run: `python -m pytest tests/booking_engine/test_routes/test_sms_routes.py -v`
Expected: FAIL — 404 on every route.

- [ ] **Step 4: Implement**

Create `booking_engine/api/routes/sms.py`:

```python
"""SMS send + Twilio webhooks. See docs/messaging-design.md §6.1."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from booking_engine.api.deps import require_control_plane_token, _get_settings
from booking_engine.config import Settings
from booking_engine.db import sms_queries
from booking_engine.services.messaging.sms_inbound import handle_inbound
from booking_engine.services.messaging.sms_send import send_marketing_sms

router = APIRouter(prefix="/sms", tags=["sms"])

# Twilio expects TwiML or an empty 200; anything else shows up as an error in
# the console and triggers retries.
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


class SendRequest(BaseModel):
    shop_id: UUID
    customer_id: UUID
    body: str = Field(min_length=1, max_length=1600)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v


@router.post("/send")
async def send(
    payload: SendRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Synchronous single send. The owner is watching a modal — they need the
    outcome now, not within the hour."""
    result = await send_marketing_sms(
        shop_id=payload.shop_id,
        customer_id=payload.customer_id,
        body=payload.body,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )
    if not result.ok:
        # 409, not 400: the request was valid, the current state refuses it.
        raise HTTPException(status_code=409, detail=result.reason)
    return {"data": {
        "message_id": str(result.message_id),
        "segments": result.segments,
        "credits": result.credits,
    }}


@router.post("/webhook/inbound")
async def inbound(
    request: Request,
    From: Annotated[str, Form()],
    To: Annotated[str, Form()],
    Body: Annotated[str, Form()] = "",
) -> Response:
    await _verify_twilio(request)
    await handle_inbound(to_number=To, from_number=From, body=Body)
    # Always 200, even for a non-STOP reply: a retry storm helps nobody.
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


@router.post("/webhook/status")
async def status(
    request: Request,
    MessageSid: Annotated[str, Form()],
    MessageStatus: Annotated[str, Form()],
    ErrorCode: Annotated[str | None, Form()] = None,
    Price: Annotated[str | None, Form()] = None,
) -> Response:
    await _verify_twilio(request)
    if MessageStatus in {"delivered", "failed", "undelivered", "sent"}:
        await sms_queries.update_status_by_sid(
            provider_sid=MessageSid,
            status="delivered" if MessageStatus == "delivered"
                   else "failed" if MessageStatus in {"failed", "undelivered"}
                   else "sent",
            price_usd=abs(float(Price)) if Price else None,
            error_code=ErrorCode,
        )
    return Response(content=_EMPTY_TWIML, media_type="application/xml")
```

For `_verify_twilio`, import and reuse the helper found in Step 1 rather than
writing a new one. If `voice_twiml.py` defines it privately, lift it into
`booking_engine/services/twilio_signature.py` in this task and update both
call sites — one verifier, two routes.

- [ ] **Step 5: Register the router**

In `booking_engine/api/app.py`, after the `voice_heartbeat` block:

```python
    from booking_engine.api.routes import sms
    app.include_router(sms.router, prefix="/api/v1")
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/booking_engine/test_routes/test_sms_routes.py -v`
Expected: 3 passed.

- [ ] **Step 7: Run the whole suite for regressions**

Run: `python -m pytest tests/ --ignore=tests/live_db -q`
Expected: all pass except the 5 known pre-existing `test_voice_twiml_webhook.py`
failures recorded in `CLAUDE.md` (2026-07-24). If any *other* test fails, stop
and fix before continuing.

- [ ] **Step 8: Commit**

```bash
git add booking_engine/api/routes/sms.py booking_engine/api/app.py \
        tests/booking_engine/test_routes/test_sms_routes.py
git commit -m "feat(sms): send and Twilio webhook routes"
```

---

## Task 10 (webapp): Rename to Touchpoint Clienti

**Files:**
- Modify: `src/i18n/it.ts:623`, `src/i18n/en.ts` *(webapp, `dev_messaging_sms`)*

- [ ] **Step 1: Find both keys**

```bash
cd ~/Documents/kairo/webapp
grep -rn "tab_customers" src/i18n/ src/components/marketing/
```

- [ ] **Step 2: Change the labels only**

`src/i18n/it.ts`: `tab_customers: 'Salute Clienti'` → `tab_customers: 'Touchpoint Clienti'`
`src/i18n/en.ts`: the matching English value → `'Customer Touchpoints'`

Do **not** rename `CustomersTab.tsx`, the `customers/` directory, or the
`tab_customers` key. A file rename here changes no reader's understanding and
churns every import.

- [ ] **Step 3: Verify nothing else referenced the old string**

Run: `grep -rn "Salute Clienti" src/ ; echo "exit=$?"`
Expected: no matches (`exit=1`).

- [ ] **Step 4: Commit**

```bash
git add src/i18n/it.ts src/i18n/en.ts
git commit -m "feat(marketing): rename Salute Clienti to Touchpoint Clienti"
```

---

## Task 11 (webapp): Segment/credit preview

The owner must see the price before the click. This mirrors `gsm7.py` across a
language boundary — a duplication accepted deliberately, because the alternative
is a network round-trip on every keystroke. voice-booking's count is
authoritative at send time; this one is a preview and says so.

**Files:**
- Create: `src/lib/messaging/sms-preview.ts`
- Test: `src/lib/messaging/__tests__/sms-preview.test.ts`

- [ ] **Step 1: Write the failing test**

Create `src/lib/messaging/__tests__/sms-preview.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { previewSms } from '../sms-preview'

describe('previewSms', () => {
  it('treats Italian accents as free GSM-7', () => {
    const p = previewSms("Ciao Giulia, è passato un po'. Un caffè?")
    expect(p.encoding).toBe('gsm7')
    expect(p.segments).toBe(1)
  })

  it('transliterates the curly quote an LLM writes', () => {
    const p = previewSms('Ciao Giulia, com’è andata?')
    expect(p.encoding).toBe('gsm7')
  })

  it('reports UCS-2 and the higher cost for emoji instead of hiding it', () => {
    const p = previewSms('Ciao Giulia 💇')
    expect(p.encoding).toBe('ucs2')
    expect(p.segments).toBe(1)
  })

  // The footer is 37 GSM-7 chars, so a one-segment message has room for 123.
  it('counts segments at the GSM-7 boundaries, footer included', () => {
    expect(previewSms('a'.repeat(123)).segments).toBe(1)   // 123 + 37 = 160
    expect(previewSms('a'.repeat(124)).segments).toBe(2)   // 161
  })

  it('charges 186 credits per Italian segment', () => {
    expect(previewSms('a'.repeat(123)).credits).toBe(186)
    expect(previewSms('a'.repeat(124)).credits).toBe(372)
  })

  it('counts the opt-out footer, since the sender appends it either way', () => {
    // 130 chars of body looks like one segment but is two once sent — the owner
    // must see the real price, not the one the draft implies.
    expect(previewSms('a'.repeat(130)).segments).toBe(2)
    expect(previewSms('a'.repeat(130)).text).toContain('STOP')
  })
})
```

- [ ] **Step 2: Run and watch it fail**

Run: `npx vitest run src/lib/messaging/__tests__/sms-preview.test.ts`
Expected: FAIL — cannot resolve `../sms-preview`.

- [ ] **Step 3: Implement**

Create `src/lib/messaging/sms-preview.ts`:

```typescript
/**
 * Cost preview for a marketing SMS, shown before the owner clicks Invia.
 *
 * Mirrors booking_engine/services/messaging/gsm7.py + send_credits.py. That
 * duplication is deliberate: the alternative is a network round-trip per
 * keystroke. voice-booking recomputes at send time and its number is the one
 * that gets billed — this is a preview, and small drift is acceptable where a
 * spinner is not.
 */

const BASIC =
  '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ' +
  ' !"#¤%&\'()*+,-./0123456789:;<=>?' +
  '¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§' +
  '¿abcdefghijklmnopqrstuvwxyzäöñüà'
const EXTENDED = '^{}\\[~]|€'

const TRANSLITERATE: Record<string, string> = {
  '‘': "'", '’': "'", '‚': "'", '′': "'",
  '“': '"', '”': '"', '„': '"',
  '–': '-', '—': '-', '−': '-',
  '…': '...',
  ' ': ' ', ' ': ' ', ' ': ' ',
  'È': "E'", 'À': "A'", 'Ì': "I'", 'Ò': "O'", 'Ù': "U'",
}

/** Must stay byte-identical to OPT_OUT_FOOTER in sms_send.py. */
export const OPT_OUT_FOOTER = " Rispondi STOP per non ricevere piu'."

const USD_PER_SEGMENT = 0.093
const MARGIN = 2
const CREDITS_PER_USD = 1000

export interface SmsPreview {
  encoding: 'gsm7' | 'ucs2'
  segments: number
  credits: number
  /** The exact text that will go on the wire, footer included. */
  text: string
}

function sanitize(text: string): string {
  return [...text].map((ch) => TRANSLITERATE[ch] ?? ch).join('')
}

export function previewSms(body: string): SmsPreview {
  const text = sanitize(body.trim()) + OPT_OUT_FOOTER

  // for..of iterates by code point, so an astral emoji is one `ch` here and
  // correctly falls through to the UCS-2 branch.
  let septets = 0
  let gsm7 = true
  for (const ch of text) {
    if (BASIC.includes(ch)) septets += 1
    else if (EXTENDED.includes(ch)) septets += 2
    else { gsm7 = false; break }
  }

  const encoding: 'gsm7' | 'ucs2' = gsm7 ? 'gsm7' : 'ucs2'
  // UCS-2 bills per UTF-16 code unit, which is exactly what .length counts.
  const units = gsm7 ? septets : text.length
  const single = gsm7 ? 160 : 70
  const multi = gsm7 ? 153 : 67

  const segments = units <= single ? 1 : Math.ceil(units / multi)
  const credits = Math.ceil(USD_PER_SEGMENT * segments * MARGIN * CREDITS_PER_USD)
  return { encoding, segments, credits, text }
}
```

- [ ] **Step 4: Run the tests**

Run: `npx vitest run src/lib/messaging/__tests__/sms-preview.test.ts`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/lib/messaging/
git commit -m "feat(marketing): SMS segment and credit preview"
```

---

## Task 12 (webapp): The send route

**Files:**
- Create: `src/app/api/v1/hair-salon/customers/[id]/send-sms/route.ts`
- Test: `src/app/api/v1/hair-salon/customers/[id]/send-sms/route.test.ts`

- [ ] **Step 1: Read the route it mirrors**

Run: `cat "src/app/api/v1/hair-salon/customers/[id]/retention-message/route.ts"`
and its `route.test.ts`. Follow the same `getShopId` / `ok` / `fail` shape and
the same fail-closed consent posture.

- [ ] **Step 2: Write the failing test**

Create `src/app/api/v1/hair-salon/customers/[id]/send-sms/route.test.ts`, mirroring
the mock setup in `retention-message/route.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../../../../_lib/getShopId', () => ({ getShopId: vi.fn(async () => 'shop-1') }))
vi.mock('@/lib/db/repositories/customers.repo', () => ({ getCustomerById: vi.fn() }))

import { POST } from './route'
import { getCustomerById } from '@/lib/db/repositories/customers.repo'

const consenting = {
  id: 'cust-1',
  full_name: 'Giulia Rossi',
  marketing_consent: true,
  marketing_consent_granted_at: '2026-01-01T00:00:00Z',
  marketing_consent_withdrawn_at: null,
}

function req(body: unknown) {
  return new Request('http://x/send-sms', { method: 'POST', body: JSON.stringify(body) }) as never
}
const params = { params: Promise.resolve({ id: 'cust-1' }) }

beforeEach(() => {
  vi.clearAllMocks()
  process.env.VOICE_BOOKING_API_URL = 'http://vb'
  process.env.CONTROL_PLANE_SECRET = 's3cret'
})

describe('POST send-sms', () => {
  it('refuses a customer whose consent was withdrawn', async () => {
    vi.mocked(getCustomerById).mockResolvedValue({
      ...consenting, marketing_consent_withdrawn_at: '2026-06-01T00:00:00Z',
    } as never)
    const res = await POST(req({ body: 'Ciao' }), params)
    expect(res.status).toBe(403)
  })

  it('refuses an empty body without calling voice-booking', async () => {
    vi.mocked(getCustomerById).mockResolvedValue(consenting as never)
    const spy = vi.spyOn(globalThis, 'fetch')
    const res = await POST(req({ body: '   ' }), params)
    expect(res.status).toBe(422)
    expect(spy).not.toHaveBeenCalled()
  })

  it('forwards to voice-booking and returns the credits charged', async () => {
    vi.mocked(getCustomerById).mockResolvedValue(consenting as never)
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { message_id: 'm1', segments: 2, credits: 372 } }),
                   { status: 200 }),
    )
    const res = await POST(req({ body: 'Ciao Giulia' }), params)
    expect(res.status).toBe(200)
    expect((await res.json()).data.credits).toBe(372)
  })

  it('surfaces an insufficient-credit refusal as 402', async () => {
    vi.mocked(getCustomerById).mockResolvedValue(consenting as never)
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'insufficient_credits' }), { status: 409 }),
    )
    const res = await POST(req({ body: 'Ciao' }), params)
    expect(res.status).toBe(402)
  })

  it('fails cleanly when voice-booking is not configured', async () => {
    delete process.env.VOICE_BOOKING_API_URL
    vi.mocked(getCustomerById).mockResolvedValue(consenting as never)
    const res = await POST(req({ body: 'Ciao' }), params)
    expect(res.status).toBe(503)
  })
})
```

- [ ] **Step 3: Run and watch it fail**

Run: `npx vitest run "src/app/api/v1/hair-salon/customers/[id]/send-sms/route.test.ts"`
Expected: FAIL — cannot resolve `./route`.

- [ ] **Step 4: Implement**

Create `src/app/api/v1/hair-salon/customers/[id]/send-sms/route.ts`:

```typescript
import { NextRequest } from 'next/server'
import { ok, fail } from '@/lib/api-response'
import { getShopId } from '../../../_lib/getShopId'
import { getCustomerById } from '@/lib/db/repositories/customers.repo'
import { hasActiveMarketingConsent } from '@/lib/privacy/marketing-consent'

// Sends the generated win-back copy as an SMS through voice-booking, which owns
// the Twilio credentials, the shop's sender number and the credit debit.
//
// This route deliberately does NOT deduct credits: one debit path, in the
// service that actually knows what Twilio charged. It re-checks consent anyway
// — the same reasoning as the generation route, one step closer to the send.

export async function POST(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const shopId = await getShopId(req)
    const { id } = await params

    const customer = await getCustomerById(shopId, id)
    if (!customer) return fail('Customer not found', 404)
    if (!hasActiveMarketingConsent(customer)) {
      return fail('Customer has no active marketing consent', 403)
    }

    const parsed = (await req.json().catch(() => null)) as { body?: unknown } | null
    const body = typeof parsed?.body === 'string' ? parsed.body.trim() : ''
    if (!body) return fail('Message body is required', 422)

    const BASE = process.env.VOICE_BOOKING_API_URL ?? ''
    const SECRET = process.env.CONTROL_PLANE_SECRET ?? ''
    if (!BASE || !SECRET) return fail('SMS sending is not configured', 503)

    const resp = await fetch(`${BASE}/api/v1/sms/send`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ shop_id: shopId, customer_id: id, body }),
    }).catch(() => null)

    if (!resp) return fail('Could not reach the SMS service', 502)

    if (!resp.ok) {
      const detail = (await resp.json().catch(() => null))?.detail as string | undefined
      // 402 so the modal can show the top-up prompt, matching the generation route.
      if (detail === 'insufficient_credits') return fail('Not enough AI credits', 402)
      if (detail === 'no_consent' || detail === 'opted_out') {
        return fail('Customer has opted out of marketing', 403)
      }
      if (detail === 'no_phone') return fail('Customer has no phone number', 422)
      if (detail === 'no_sender_number') return fail('This shop has no SMS number yet', 409)
      return fail('Could not send the message right now', 502)
    }

    const data = (await resp.json().catch(() => null))?.data
    return ok({
      message_id: data?.message_id ?? null,
      segments: data?.segments ?? null,
      credits: data?.credits ?? null,
    })
  } catch (err) {
    if (err instanceof Response) return err
    console.error('[customers/send-sms POST]', err)
    return fail('Internal server error', 500)
  }
}
```

- [ ] **Step 5: Run the tests**

Run: `npx vitest run "src/app/api/v1/hair-salon/customers/[id]/send-sms/route.test.ts"`
Expected: 5 passed.

- [ ] **Step 6: Document the new env vars**

Add `VOICE_BOOKING_API_URL` to `.env.example` (and to `docs/knowledge/operations.md`
in Task 14). `CONTROL_PLANE_SECRET` may already be present — check before adding
a duplicate.

- [ ] **Step 7: Commit**

```bash
git add "src/app/api/v1/hair-salon/customers/[id]/send-sms/" .env.example
git commit -m "feat(marketing): send-sms route forwarding to voice-booking"
```

---

## Task 13 (webapp): Invia SMS in the modal

**Files:**
- Modify: `src/components/marketing/customers/RetentionMessageModal.tsx`
- Modify: `src/i18n/it.ts`, `src/i18n/en.ts`

- [ ] **Step 1: Add the i18n strings**

In `src/i18n/it.ts`, in the `marketing` block next to the existing
`retention_msg_*` keys:

```typescript
    retention_msg_send:          'Invia SMS',
    retention_msg_sending:       'Invio…',
    retention_msg_sent:          'Inviato',
    retention_msg_cost:          'SMS · {credits} crediti',
    retention_msg_send_error:    'Invio non riuscito. Riprova.',
    retention_msg_no_consent:    'Il cliente ha revocato il consenso.',
    retention_msg_no_phone:      'Il cliente non ha un numero di telefono.',
    retention_msg_no_number:     'Questo salone non ha ancora un numero SMS.',
```

Add the English equivalents in `src/i18n/en.ts`.

- [ ] **Step 2: Add the send state and handler**

In `RetentionMessageModal.tsx`, after the existing `copied` state:

```typescript
  const [sendState, setSendState] = useState<'idle' | 'sending' | 'sent'>('idle')
  const [sendError, setSendError] = useState<string | null>(null)

  const preview = message ? previewSms(message) : null

  const send = async () => {
    if (!customerId || !message) return
    setSendState('sending')
    setSendError(null)
    const token = localStorage.getItem('auth_token')
    const resp = await fetch(`/api/v1/hair-salon/customers/${customerId}/send-sms`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ body: message }),
    }).catch(() => null)

    if (resp?.ok) { setSendState('sent'); return }
    setSendState('idle')
    setSendError(
      resp?.status === 402 ? tr('marketing.retention_msg_no_credit')
      : resp?.status === 403 ? tr('marketing.retention_msg_no_consent')
      : resp?.status === 422 ? tr('marketing.retention_msg_no_phone')
      : resp?.status === 409 ? tr('marketing.retention_msg_no_number')
      : tr('marketing.retention_msg_send_error'),
    )
  }
```

Add the import: `import { previewSms } from '@/lib/messaging/sms-preview'`

- [ ] **Step 3: Reset send state per customer and per regeneration**

`sendState` must not survive a new draft — otherwise the button reads "Inviato"
for a message that was never sent. Add `setSendState('idle')` and
`setSendError(null)` to **both** the `generate` callback (next to
`setCopied(false)`) and the per-customer reset effect.

- [ ] **Step 4: Render the button and the cost line**

Replace the button row so Invia is the primary action and Copia demotes:

```tsx
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => void send()}
            disabled={loading || !message || sendState !== 'idle'}
            className="kairo-btn kairo-btn-primary text-sm"
          >
            {sendState === 'sending' ? tr('marketing.retention_msg_sending')
             : sendState === 'sent' ? tr('marketing.retention_msg_sent')
             : tr('marketing.retention_msg_send')}
          </button>
          <button onClick={copy} disabled={loading || !message} className="kairo-btn text-sm">
            {copied ? tr('marketing.retention_msg_copied') : tr('marketing.retention_msg_copy')}
          </button>
          <button onClick={() => void generate()} disabled={loading} className="kairo-btn text-sm">
            {tr('marketing.retention_msg_regenerate')}
          </button>
          <button onClick={onClose} className="ml-auto py-2 text-sm" style={{ color: 'var(--color-muted)' }}>
            {tr('marketing.retention_msg_close')}
          </button>
        </div>

        {preview && (
          <p className="mt-2 text-[11px]" style={{ color: 'var(--color-muted)' }}>
            {preview.segments} {tr('marketing.retention_msg_cost')
              .replace('{credits}', String(preview.credits))}
          </p>
        )}
        {sendError && (
          <p role="alert" className="mt-2 text-[11px]" style={{ color: 'var(--color-danger)' }}>
            {sendError}
          </p>
        )}
```

- [ ] **Step 5: Update the docstring**

The file's header comment still says *"no sending... copies it into WhatsApp"* —
now false, and marketing goes by SMS per the channel decision. Replace with:

```typescript
/**
 * Win-back copy for one churn-risk customer: generated, previewed with its
 * real cost, and sent as an SMS from the shop's own number.
 *
 * Two separate charges, deliberately. Generating bills the LLM cost at the
 * marketing margin; sending bills Twilio at 2×. Regenerate costs only the
 * former, Invia only the latter.
 *
 * Every generation costs an AI credit, so the drafts already shown are sent back
 * as `previous`: the engine uses them to pick a different angle, which is what
 * makes a second click worth its charge instead of a paraphrase.
 */
```

- [ ] **Step 6: Verify in the browser**

```bash
cd ~/Documents/kairo/webapp && npm run dev
```

Open Marketing → Touchpoint Clienti, click a customer, confirm: the tab reads
"Touchpoint Clienti", the modal shows "2 SMS · 372 crediti" style cost, Invia is
primary, and Rigenera resets the button from "Inviato" back to "Invia SMS".

- [ ] **Step 7: Run lint, types and the full webapp suite**

```bash
npm run lint && npx tsc --noEmit && npx vitest run
```
Expected: clean. Fix anything this surfaces before committing.

- [ ] **Step 8: Commit**

```bash
git add src/components/marketing/customers/RetentionMessageModal.tsx src/i18n/
git commit -m "feat(marketing): send generated win-back copy as SMS"
```

---

## Task 14: Documentation

Required by both repos' `CLAUDE.md` **in the same change**, not as a follow-up.

**Files:**
- Modify (voice-booking): `docs/knowledge/architecture.md`, `database.md`, `providers.md`, `api/`, `CLAUDE.md`
- Modify (webapp): `docs/knowledge/features.md`, `providers.md`, `operations.md`, `decisions.md`

- [ ] **Step 1: voice-booking `docs/knowledge/`**

- `database.md` — the `sms` schema: three tables, what each column is for, and
  why `opt_outs` exists alongside `customers.marketing_consent` (a STOP from a
  phone that matches no customer must still suppress).
- `api/` — `POST /api/v1/sms/send` (control-plane token, 409 + reason on
  refusal), `/sms/webhook/inbound`, `/sms/webhook/status` (both Twilio-signed).
- `providers.md` — Twilio Messaging: the shop's own DID is the sender; Twilio's
  automatic STOP handling does not cover non-US numbers, so we implement it.
- `architecture.md` — the send path and the single-debit-path rule.

- [ ] **Step 2: voice-booking `CLAUDE.md`**

Add a dated entry at the top (newest first, never rewrite older ones) recording:
SMS chosen for marketing because WhatsApp forbids free-form business-initiated
messages; 2× Twilio via credits and why `rawToUserCredits` was not reused (10×
margin, floor of 1 would charge for free messages); consent stays in
`business_app_core.customers` with `sms.opt_outs` as the last-resort list; the
send is synchronous because the owner is watching a modal.

- [ ] **Step 3: webapp `docs/knowledge/`**

- `features.md` — a Touchpoint Clienti entry: purpose, status, the two-charge
  model, and the gotcha that segment counting is duplicated between repos.
- `providers.md` — voice-booking now receives outbound calls from the webapp
  (`VOICE_BOOKING_API_URL` + `CONTROL_PLANE_SECRET`), reversing the previously
  documented direction.
- `operations.md` — the new env var.
- `decisions.md` — why the webapp does not deduct credits for a send.

- [ ] **Step 4: Commit both**

```bash
cd ~/Documents/kairo/voice-booking
git add docs/ CLAUDE.md && git commit -m "docs: SMS marketing send"
cd ~/Documents/kairo/webapp
git add docs/ && git commit -m "docs: Touchpoint Clienti SMS send"
```

---

## Task 15: Live-DB verification

Mocked tests never touch the real schema. `CLAUDE.md`'s 2026-07-21 entry records
a migration that would have crashed on first use precisely because its only tests
were live-DB ones that had been silently skipping.

**Files:**
- Create: `tests/live_db/test_sms_live.py`

- [ ] **Step 1: Write the tests**

```python
"""Real schema, real consent rows. Requires TEST_DATABASE_URL (QA Neon branch)."""
import os
import pytest
from uuid import uuid4

from booking_engine.db import sms_queries

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"
)


@pytest.mark.asyncio
async def test_opt_out_is_idempotent(live_shop_id):
    phone = f"+3933300{uuid4().int % 100000:05d}"
    await sms_queries.record_opt_out(
        shop_id=live_shop_id, phone_normalized=phone, keyword="STOP", raw_body="STOP")
    # A customer who texts STOP twice must not blow up the second time.
    await sms_queries.record_opt_out(
        shop_id=live_shop_id, phone_normalized=phone, keyword="STOP", raw_body="STOP")
    assert await sms_queries.is_opted_out(live_shop_id, phone) is True


@pytest.mark.asyncio
async def test_opt_out_from_unknown_phone_updates_no_customer(live_shop_id):
    phone = f"+3933399{uuid4().int % 100000:05d}"
    updated = await sms_queries.withdraw_marketing_consent(
        shop_id=live_shop_id, phone_normalized=phone)
    assert updated == 0   # and must not raise


@pytest.mark.asyncio
async def test_customer_lookup_is_shop_scoped(live_shop_id, live_customer_id):
    assert await sms_queries.get_customer_for_send(live_shop_id, live_customer_id) is not None
    # A different shop's id must not reach this customer.
    assert await sms_queries.get_customer_for_send(uuid4(), live_customer_id) is None


@pytest.mark.asyncio
async def test_outbound_insert_round_trips(live_shop_id, live_customer_id):
    mid = await sms_queries.insert_outbound(
        shop_id=live_shop_id, customer_id=live_customer_id,
        to_phone="+393331234567", from_number="+37251234567",
        body="test", segments=1, encoding="gsm7", status="suppressed",
        suppressed_reason="no_consent",
    )
    assert mid is not None
```

Reuse the existing `live_shop_id` / `live_customer_id` fixtures from
`tests/live_db/conftest.py`. If they aren't named that, run
`grep -n "def live_\|@pytest.fixture" tests/live_db/conftest.py` and use the
real names — do not add duplicates.

- [ ] **Step 2: Apply the migration to the QA branch and run**

```bash
export TEST_DATABASE_URL='<QA branch connection string>'
DATABASE_URL="$TEST_DATABASE_URL" psql -v ON_ERROR_STOP=1 "$TEST_DATABASE_URL" \
  -f booking_engine/db/sql/11_sms_schema.sql
python -m pytest tests/live_db/test_sms_live.py -v
```

Expected: 4 passed. If they skip, `TEST_DATABASE_URL` isn't exported — that is a
skip, not a pass, and this task is not done.

- [ ] **Step 3: Commit**

```bash
git add tests/live_db/test_sms_live.py
git commit -m "test(sms): live-DB coverage for opt-out and shop scoping"
```

---

## Definition of done

- [ ] `python -m pytest tests/ --ignore=tests/live_db -q` — green except the 5
      known `test_voice_twiml_webhook.py` failures.
- [ ] `python -m pytest tests/live_db/test_sms_live.py -v` — 4 passed, not skipped.
- [ ] webapp: `npm run lint && npx tsc --noEmit && npx vitest run` — clean.
- [ ] `11_sms_schema.sql` applied twice to a scratch DB with no error.
- [ ] One real SMS sent from a QA number to a real handset, arriving with the
      opt-out footer; replying STOP flips `marketing_consent` to `false` and
      creates the `sms.opt_outs` row. **This is the only check that proves the
      Twilio path**; every test above fakes the provider.
- [ ] Both repos' `docs/knowledge/` updated, voice-booking `CLAUDE.md` entry added.

## Out of scope for Phase 1

Campaign batches (`sms.campaigns` exists but nothing writes it), the
`/messaging/tick` cron, WhatsApp anything, and number provisioning. Phase 1 is
one message to one customer from a number the shop already has.
