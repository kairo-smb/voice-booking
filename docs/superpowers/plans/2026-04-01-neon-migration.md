# Databricks SQL → Neon PostgreSQL Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Databricks SQL backend with Neon (PostgreSQL) while maintaining 100% API feature parity — every endpoint, query, and behavior must work identically.

**Architecture:** The booking_engine DB layer (`connection.py`, `queries.py`) is rewritten to use `asyncpg` (native async PostgreSQL driver). The schema gets proper PostgreSQL types (native UUID, TIMESTAMPTZ, NUMERIC), constraints (PK, FK, UNIQUE), and indexes. All API routes remain unchanged — only the DB layer beneath them changes. The voice_gateway is untouched (it calls booking_engine via HTTP).

**Tech Stack:** asyncpg (async PostgreSQL driver), Neon PostgreSQL (serverless), FastAPI (unchanged), pytest + pytest-asyncio (tests)

**What changes:**
| Layer | File | Change |
|-------|------|--------|
| Config | `booking_engine/config.py` | Neon DATABASE_URL instead of Databricks settings |
| Connection | `booking_engine/db/connection.py` | asyncpg pool instead of databricks-sql-connector |
| Queries | `booking_engine/db/queries.py` | PostgreSQL dialect ($1 params, native UUID, etc.) |
| Schema | `booking_engine/db/sql/01_schema.sql` | PostgreSQL DDL with constraints + indexes |
| Seed | `booking_engine/db/sql/02_seed_data.sql` | INSERT ON CONFLICT instead of MERGE |
| App | `booking_engine/api/app.py` | asyncpg pool lifespan |
| Deps | `requirements.txt` | asyncpg replaces databricks packages |
| Tests | `tests/` | Updated for asyncpg interface + Neon live tests |

**What does NOT change:**
- `booking_engine/api/models.py` — Pydantic models are DB-agnostic
- `booking_engine/api/routes/*` — Routes call query functions by name; signatures stay identical
- `voice_gateway/` — Entire module untouched (HTTP client to booking engine)

---

## File Structure

### Modified files
- `booking_engine/config.py` — Replace Databricks settings with `database_url: str`
- `booking_engine/db/connection.py` — Rewrite: asyncpg pool, `execute`/`execute_one`/`execute_void` with same signatures
- `booking_engine/db/queries.py` — Rewrite all SQL to PostgreSQL dialect, keep function signatures identical
- `booking_engine/db/sql/01_schema.sql` — PostgreSQL DDL with UUID type, PK/FK/UNIQUE constraints, indexes
- `booking_engine/db/sql/02_seed_data.sql` — INSERT ... ON CONFLICT DO NOTHING
- `booking_engine/api/app.py` — asyncpg pool init/close in lifespan
- `requirements.txt` — Remove databricks packages, add asyncpg
- `tests/conftest.py` — No change (fixtures are DB-agnostic)
- `tests/booking_engine/test_connection.py` — Rewrite for asyncpg pool interface
- `tests/booking_engine/test_queries.py` — Update mock targets for asyncpg
- `tests/booking_engine/test_routes/conftest.py` — No change (mocks query functions)
- `tests/live_db/conftest.py` — Neon connection instead of Databricks
- `tests/live_db/test_read_queries.py` — Minimal changes (query signatures unchanged)
- `tests/live_db/test_write_queries.py` — Minimal changes
- `tests/live_db/test_availability.py` — Minimal changes
- `tests/integration/conftest.py` — No change (FakeDB is in-memory)

---

## Key Migration Reference: Databricks SQL → PostgreSQL

| Concept | Databricks SQL | PostgreSQL (asyncpg) |
|---------|---------------|---------------------|
| Parameters | `%(name)s` dict | `$1, $2, ...` positional args |
| UUID type | STRING | UUID (native) |
| Timestamp | TIMESTAMP + `current_timestamp()` | TIMESTAMPTZ + `NOW()` |
| Price | DECIMAL(8,2) | NUMERIC(8,2) |
| String concat | `CONCAT(LOWER(x), '%%')` | `LOWER($1) \|\| '%'` |
| IN clause | f-string interpolation | `= ANY($1::uuid[])` |
| Upsert | MERGE INTO ... USING | INSERT ... ON CONFLICT |
| Generate UUID | `uuid()` | `gen_random_uuid()` |
| Table prefix | `catalog.schema.table` | `schema.table` or just `table` |
| Connection | `dbsql.connect(host, path, token)` | `asyncpg.create_pool(dsn)` |
| Fetch rows | sync cursor → `asyncio.to_thread` | native async `conn.fetch()` |
| Row type | tuple → dict conversion | `asyncpg.Record` → `dict(record)` |

---

### Task 1: PostgreSQL Schema (DDL)

**Files:**
- Modify: `booking_engine/db/sql/01_schema.sql`

- [ ] **Step 1: Rewrite the schema DDL for PostgreSQL**

Replace the entire file with PostgreSQL-native DDL. Key changes: native UUID type, TIMESTAMPTZ, PRIMARY KEY / FOREIGN KEY / UNIQUE constraints, indexes on lookup columns.

```sql
-- Virtual Assistant Booking Engine — PostgreSQL Schema
-- Target: Neon PostgreSQL
-- Run once to create schema. Idempotent (IF NOT EXISTS).

-- ============================================================
-- 0. Extensions
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- ============================================================
-- 1. Shops
-- ============================================================

CREATE TABLE IF NOT EXISTS shops (
  id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  name                TEXT            NOT NULL,
  phone_number        TEXT,
  address             TEXT,
  welcome_message     TEXT,
  tone_instructions   TEXT,
  personality         TEXT,
  special_instructions TEXT,
  is_active           BOOLEAN         NOT NULL DEFAULT true,
  created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2. Staff
-- ============================================================

CREATE TABLE IF NOT EXISTS staff (
  id            UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id       UUID            NOT NULL REFERENCES shops(id),
  full_name     TEXT            NOT NULL,
  role          TEXT,
  phone_number  TEXT,
  email         TEXT,
  bio           TEXT,
  is_active     BOOLEAN         NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_staff_shop ON staff(shop_id);

-- ============================================================
-- 3. Services
-- ============================================================

CREATE TABLE IF NOT EXISTS services (
  id                UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id           UUID            NOT NULL REFERENCES shops(id),
  service_name      TEXT            NOT NULL,
  description       TEXT,
  duration_minutes  INTEGER         NOT NULL,
  price_eur         NUMERIC(8,2),
  category          TEXT,
  is_active         BOOLEAN         NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_services_shop ON services(shop_id);

-- ============================================================
-- 4. Staff <-> Services (M2M)
-- ============================================================

CREATE TABLE IF NOT EXISTS staff_services (
  staff_id    UUID    NOT NULL REFERENCES staff(id),
  service_id  UUID    NOT NULL REFERENCES services(id),
  PRIMARY KEY (staff_id, service_id)
);

-- ============================================================
-- 5. Staff Schedules
-- ============================================================

CREATE TABLE IF NOT EXISTS staff_schedules (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  staff_id      UUID        NOT NULL REFERENCES staff(id),
  day_of_week   INTEGER     NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
  start_time    TEXT        NOT NULL,   -- HH:MM format
  end_time      TEXT        NOT NULL,   -- HH:MM format
  UNIQUE (staff_id, day_of_week)
);

-- ============================================================
-- 6. Customers
-- ============================================================

CREATE TABLE IF NOT EXISTS customers (
  id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id             UUID            NOT NULL REFERENCES shops(id),
  full_name           TEXT            NOT NULL,
  email               TEXT,
  preferred_staff_id  UUID            REFERENCES staff(id),
  notes               TEXT,
  created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id);

-- ============================================================
-- 7. Phone Contacts (caller ID linking)
-- ============================================================

CREATE TABLE IF NOT EXISTS phone_contacts (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_number  TEXT        NOT NULL,
  customer_id   UUID        NOT NULL REFERENCES customers(id),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (phone_number, customer_id)
);

CREATE INDEX IF NOT EXISTS idx_phone_contacts_phone ON phone_contacts(phone_number);

-- ============================================================
-- 8. Appointments
-- ============================================================

CREATE TABLE IF NOT EXISTS appointments (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id       UUID        NOT NULL REFERENCES shops(id),
  customer_id   UUID        NOT NULL REFERENCES customers(id),
  staff_id      UUID        NOT NULL REFERENCES staff(id),
  start_time    TIMESTAMPTZ NOT NULL,
  end_time      TIMESTAMPTZ NOT NULL,
  status        TEXT        NOT NULL DEFAULT 'scheduled',
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_appointments_staff_time ON appointments(staff_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_appointments_shop ON appointments(shop_id);
CREATE INDEX IF NOT EXISTS idx_appointments_customer ON appointments(customer_id);

-- ============================================================
-- 9. Appointment <-> Services (junction)
-- ============================================================

CREATE TABLE IF NOT EXISTS appointment_services (
  appointment_id  UUID        NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
  service_id      UUID        NOT NULL REFERENCES services(id),
  duration_minutes INTEGER    NOT NULL,
  price_eur       NUMERIC(8,2),
  PRIMARY KEY (appointment_id, service_id)
);
```

- [ ] **Step 2: Verify DDL is valid by reviewing constraints**

Check that all FK references are correct: `staff.shop_id → shops.id`, `services.shop_id → shops.id`, `staff_services.staff_id → staff.id`, `staff_services.service_id → services.id`, `staff_schedules.staff_id → staff.id`, `customers.shop_id → shops.id`, `customers.preferred_staff_id → staff.id`, `phone_contacts.customer_id → customers.id`, `appointments.shop_id → shops.id`, `appointments.customer_id → customers.id`, `appointments.staff_id → staff.id`, `appointment_services.appointment_id → appointments.id`, `appointment_services.service_id → services.id`.

- [ ] **Step 3: Commit**

```bash
git add booking_engine/db/sql/01_schema.sql
git commit -m "feat: rewrite schema DDL for PostgreSQL (Neon migration)"
```

---

### Task 2: Seed Data Migration

**Files:**
- Modify: `booking_engine/db/sql/02_seed_data.sql`

- [ ] **Step 1: Rewrite seed data using INSERT ON CONFLICT**

Replace all MERGE statements with PostgreSQL `INSERT ... ON CONFLICT DO NOTHING`. Keep all UUIDs and data values identical to the Databricks version for 1:1 parity.

```sql
-- Virtual Assistant Booking Engine — Seed Data (PostgreSQL)
-- Target: Neon PostgreSQL
-- Run after 01_schema.sql. Safe to re-run (ON CONFLICT DO NOTHING).

-- ============================================================
-- Shops
-- ============================================================

INSERT INTO shops (id, name, phone_number, address, welcome_message, tone_instructions, personality, special_instructions, is_active)
VALUES
  ('a0000000-0000-0000-0000-000000000001', 'Salon Bella', '+39 02 1234567', 'Via Roma 42, Milano',
   'Ciao, benvenuto da Salon Bella! Come ti chiami?',
   'Amichevole e informale, dai del tu al cliente',
   'Sei Bella, l''assistente virtuale del Salon Bella. Sei solare, cordiale e sempre pronta ad aiutare.',
   'Se il cliente chiede di un servizio che non offriamo, suggerisci il servizio più simile disponibile.',
   true),
  ('b0000000-0000-0000-0000-000000000002', 'Studio Hair', '+39 06 7654321', 'Via del Corso 15, Roma',
   'Buongiorno, benvenuto allo Studio Hair. Come posso aiutarla?',
   'Professionale e formale, dia del lei al cliente',
   'Sei l''assistente dello Studio Hair. Sei professionale, preciso e attento ai dettagli.',
   NULL,
   true)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Staff
-- ============================================================

INSERT INTO staff (id, shop_id, full_name, role, bio, is_active)
VALUES
  ('11111111-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'Mirco Meazzo', 'stilista senior', 'Stilista con 15 anni di esperienza', true),
  ('11111111-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001', 'Giulia Verdi', 'colorista', 'Esperta di colorazioni e trattamenti', true),
  ('11111111-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000001', 'Marco Bianchi', 'stilista', 'Specializzato in tagli classici e barba', true),
  ('22222222-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000002', 'Anna Rossi', 'stilista senior', 'Stilista di fama internazionale', true),
  ('22222222-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000002', 'Luca Neri', 'colorista', 'Esperto di balayage e meches', true)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Services
-- ============================================================

INSERT INTO services (id, shop_id, service_name, description, duration_minutes, price_eur, category, is_active)
VALUES
  -- Salon Bella
  ('aaaa0001-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'Taglio donna', 'Taglio, shampoo e piega', 45, 35.00, 'taglio', true),
  ('aaaa0001-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001', 'Taglio uomo', 'Taglio maschile classico o moderno', 30, 25.00, 'taglio', true),
  ('aaaa0001-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000001', 'Colore', 'Colorazione completa', 60, 55.00, 'colore', true),
  ('aaaa0001-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000001', 'Meches', 'Meches o colpi di sole', 90, 70.00, 'colore', true),
  ('aaaa0001-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000001', 'Piega', 'Piega semplice', 30, 20.00, 'piega', true),
  ('aaaa0001-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000001', 'Trattamento cheratina', 'Trattamento lisciante alla cheratina', 120, 90.00, 'trattamento', true),
  -- Studio Hair
  ('bbbb0001-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000002', 'Taglio donna', 'Taglio con consulenza personalizzata', 50, 45.00, 'taglio', true),
  ('bbbb0001-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000002', 'Colore', 'Colorazione premium', 75, 80.00, 'colore', true),
  ('bbbb0001-0000-0000-0000-000000000003', 'b0000000-0000-0000-0000-000000000002', 'Balayage', 'Balayage naturale', 120, 120.00, 'colore', true),
  ('bbbb0001-0000-0000-0000-000000000004', 'b0000000-0000-0000-0000-000000000002', 'Piega', 'Piega professionale', 30, 30.00, 'piega', true)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Staff <-> Services
-- ============================================================

INSERT INTO staff_services (staff_id, service_id)
VALUES
  -- Mirco Meazzo: taglio donna, taglio uomo, piega
  ('11111111-0000-0000-0000-000000000001', 'aaaa0001-0000-0000-0000-000000000001'),
  ('11111111-0000-0000-0000-000000000001', 'aaaa0001-0000-0000-0000-000000000002'),
  ('11111111-0000-0000-0000-000000000001', 'aaaa0001-0000-0000-0000-000000000005'),
  -- Giulia Verdi: colore, meches, piega, cheratina
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000003'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000004'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000005'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000006'),
  -- Marco Bianchi: taglio donna, taglio uomo, piega
  ('11111111-0000-0000-0000-000000000003', 'aaaa0001-0000-0000-0000-000000000001'),
  ('11111111-0000-0000-0000-000000000003', 'aaaa0001-0000-0000-0000-000000000002'),
  ('11111111-0000-0000-0000-000000000003', 'aaaa0001-0000-0000-0000-000000000005'),
  -- Anna Rossi: taglio donna, colore, balayage, piega
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000001'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000002'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000003'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000004'),
  -- Luca Neri: colore, balayage
  ('22222222-0000-0000-0000-000000000002', 'bbbb0001-0000-0000-0000-000000000002'),
  ('22222222-0000-0000-0000-000000000002', 'bbbb0001-0000-0000-0000-000000000003')
ON CONFLICT (staff_id, service_id) DO NOTHING;

-- ============================================================
-- Staff Schedules (all staff: Mon-Sat 10:00-18:00)
-- ============================================================

INSERT INTO staff_schedules (id, staff_id, day_of_week, start_time, end_time)
SELECT gen_random_uuid(), s.id, d.day, '10:00', '18:00'
FROM staff s
CROSS JOIN (VALUES (0),(1),(2),(3),(4),(5)) AS d(day)
ON CONFLICT (staff_id, day_of_week) DO NOTHING;

-- ============================================================
-- Sample Customers
-- ============================================================

INSERT INTO customers (id, shop_id, full_name, preferred_staff_id)
VALUES
  ('cccc0001-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'Maria Rossi', '11111111-0000-0000-0000-000000000001'),
  ('cccc0001-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001', 'Luca Ferrari', NULL),
  ('cccc0002-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000002', 'Francesca Bianchi', NULL)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Sample Phone Contacts
-- ============================================================

INSERT INTO phone_contacts (id, phone_number, customer_id)
VALUES
  (gen_random_uuid(), '+39 333 1111111', 'cccc0001-0000-0000-0000-000000000001'),
  (gen_random_uuid(), '+39 333 2222222', 'cccc0001-0000-0000-0000-000000000002'),
  (gen_random_uuid(), '+39 333 3333333', 'cccc0002-0000-0000-0000-000000000001')
ON CONFLICT (phone_number, customer_id) DO NOTHING;
```

- [ ] **Step 2: Commit**

```bash
git add booking_engine/db/sql/02_seed_data.sql
git commit -m "feat: rewrite seed data for PostgreSQL (INSERT ON CONFLICT)"
```

---

### Task 3: Configuration — Neon Database URL

**Files:**
- Modify: `booking_engine/config.py`

- [ ] **Step 1: Write failing test for new Settings**

Create test to verify Settings loads `database_url` and no longer requires Databricks fields.

```python
# tests/booking_engine/test_config.py
import os
import pytest
from booking_engine.config import Settings


def test_settings_from_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    s = Settings()
    assert s.database_url == "postgresql://user:pass@host/db"


def test_settings_default_pool_sizes():
    s = Settings(database_url="postgresql://localhost/test")
    assert s.pool_min_size == 2
    assert s.pool_max_size == 10
```

- [ ] **Step 2: Run tests — expect FAIL (Settings still has Databricks fields)**

```bash
pytest tests/booking_engine/test_config.py -v
```

Expected: FAIL — `database_url` not found on Settings.

- [ ] **Step 3: Rewrite config.py for Neon**

```python
"""Booking Engine configuration from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""
    pool_min_size: int = 2
    pool_max_size: int = 10

    model_config = {"env_prefix": ""}
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/booking_engine/test_config.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/config.py tests/booking_engine/test_config.py
git commit -m "feat: replace Databricks config with Neon DATABASE_URL"
```

---

### Task 4: Connection Layer — asyncpg Pool

**Files:**
- Modify: `booking_engine/db/connection.py`
- Modify: `tests/booking_engine/test_connection.py`

- [ ] **Step 1: Write failing tests for new connection module**

Replace the existing test_connection.py with tests for the asyncpg-based interface. The public API stays the same (`execute`, `execute_one`, `execute_void`, `init_connection`, `close_connection`) but internals change.

```python
# tests/booking_engine/test_connection.py
"""Unit tests for asyncpg-based connection module."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from booking_engine.config import Settings


@pytest.fixture
def settings():
    return Settings(database_url="postgresql://user:pass@localhost:5432/testdb")


class TestInitConnection:
    @patch("booking_engine.db.connection.asyncpg")
    async def test_creates_pool(self, mock_asyncpg, settings):
        mock_pool = AsyncMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        from booking_engine.db.connection import init_connection
        await init_connection(settings)

        mock_asyncpg.create_pool.assert_called_once_with(
            dsn=settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )


class TestCloseConnection:
    @patch("booking_engine.db.connection._pool")
    async def test_closes_pool(self, mock_pool):
        mock_pool.close = AsyncMock()
        from booking_engine.db.connection import close_connection
        # Set the module-level _pool
        import booking_engine.db.connection as mod
        mod._pool = mock_pool
        await close_connection()
        mock_pool.close.assert_called_once()


class TestExecute:
    @patch("booking_engine.db.connection._get_pool")
    async def test_returns_list_of_dicts(self, mock_get_pool):
        mock_conn = AsyncMock()
        mock_record = MagicMock()
        mock_record.items.return_value = [("id", "abc"), ("name", "test")]
        mock_record.__iter__ = lambda self: iter(["abc", "test"])
        mock_record.keys.return_value = ["id", "name"]
        mock_record.__getitem__ = lambda self, key: {"id": "abc", "name": "test"}[key]
        mock_conn.fetch = AsyncMock(return_value=[mock_record])

        mock_pool = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        from booking_engine.db.connection import execute
        result = await execute("SELECT * FROM shops WHERE id = $1", "abc")

        assert isinstance(result, list)
        mock_conn.fetch.assert_called_once_with("SELECT * FROM shops WHERE id = $1", "abc")


class TestExecuteOne:
    @patch("booking_engine.db.connection._get_pool")
    async def test_returns_dict_or_none(self, mock_get_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        mock_pool = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        from booking_engine.db.connection import execute_one
        result = await execute_one("SELECT * FROM shops WHERE id = $1", "abc")

        assert result is None
        mock_conn.fetchrow.assert_called_once()


class TestExecuteVoid:
    @patch("booking_engine.db.connection._get_pool")
    async def test_executes_without_return(self, mock_get_pool):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 1")

        mock_pool = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        from booking_engine.db.connection import execute_void
        await execute_void("INSERT INTO shops (id) VALUES ($1)", "abc")

        mock_conn.execute.assert_called_once()
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
pytest tests/booking_engine/test_connection.py -v
```

Expected: FAIL — connection.py still uses Databricks.

- [ ] **Step 3: Rewrite connection.py for asyncpg**

```python
"""PostgreSQL (Neon) connection management via asyncpg."""
from __future__ import annotations

import logging

import asyncpg

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Connection pool not initialized. Call init_connection first.")
    return _pool


async def execute(sql: str, *args) -> list[dict]:
    """Execute SQL and return all rows as list of dicts."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(row) for row in rows]


async def execute_one(sql: str, *args) -> dict | None:
    """Execute SQL and return one row as dict, or None."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None


async def execute_void(sql: str, *args) -> None:
    """Execute SQL that returns nothing (INSERT/UPDATE/DELETE)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *args)


async def init_connection(settings: Settings) -> None:
    """Create the asyncpg connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    logger.info("PostgreSQL connection pool initialized (min=%d, max=%d)",
                settings.pool_min_size, settings.pool_max_size)


async def close_connection() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")
```

Note: `get_table()` is removed — PostgreSQL doesn't need catalog.schema prefixes. If schema qualification is needed later, add it as a simple prefix in Settings, but for Neon the default `public` schema is fine.

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/booking_engine/test_connection.py -v
```

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/connection.py tests/booking_engine/test_connection.py
git commit -m "feat: rewrite connection layer for asyncpg (Neon)"
```

---

### Task 5: Query Functions — PostgreSQL Dialect

**Files:**
- Modify: `booking_engine/db/queries.py`

This is the largest task. Every SQL query must be rewritten from Databricks SQL dialect to PostgreSQL.

**Key changes per query:**
- `%(name)s` → `$1, $2, ...` positional params
- `get_table("x")` → just `"x"` (table name directly)
- `CONCAT(LOWER(%(name)s), '%%')` → `LOWER($3) || '%'`
- f-string IN clauses → `= ANY($1::uuid[])` with array params
- Pass `UUID` objects directly (asyncpg handles native UUID)
- `current_timestamp()` → `NOW()`
- Remove `str(uuid)` wrappers — pass UUID objects directly

- [ ] **Step 1: Write failing tests for PostgreSQL query signatures**

Update `tests/booking_engine/test_queries.py` to mock the new `execute`/`execute_one`/`execute_void` that accept `*args` (positional) instead of `dict` params.

```python
# tests/booking_engine/test_queries.py
"""Unit tests for query functions (mocked DB)."""
from __future__ import annotations

from datetime import datetime, date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from booking_engine.db.queries import (
    SlotConflictError,
    cancel_appointment,
    create_appointment,
    create_customer,
    find_customers_by_name_and_phone,
    find_customers_by_phone,
    get_available_slots,
    get_shop,
    get_staff_services,
    list_appointments,
    list_services,
    list_staff,
    reschedule_appointment,
)

SHOP = UUID("a0000000-0000-0000-0000-000000000001")
STAFF = UUID("11111111-0000-0000-0000-000000000001")
SVC = UUID("aaaa0001-0000-0000-0000-000000000001")
CUSTOMER = UUID("cccc0001-0000-0000-0000-000000000001")
APPT = UUID("dddddddd-0000-0000-0000-000000000001")
_ROME = ZoneInfo("Europe/Rome")


class TestGetShop:
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_found(self, mock_exec):
        mock_exec.return_value = {"id": SHOP, "name": "Salon Bella", "is_active": True}
        result = await get_shop(SHOP)
        assert result["name"] == "Salon Bella"
        mock_exec.assert_called_once()
        # Verify positional args: SQL string, then UUID
        call_args = mock_exec.call_args
        assert SHOP in call_args.args

    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_not_found(self, mock_exec):
        mock_exec.return_value = None
        result = await get_shop(SHOP)
        assert result is None


class TestListStaff:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_list(self, mock_exec):
        mock_exec.return_value = [
            {"id": STAFF, "full_name": "Mirco", "role": "stilista", "bio": "test"}
        ]
        result = await list_staff(SHOP)
        assert len(result) == 1
        assert result[0]["full_name"] == "Mirco"


class TestListServices:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_list(self, mock_exec):
        mock_exec.return_value = [
            {"id": SVC, "service_name": "Taglio donna", "duration_minutes": 45, "price_eur": Decimal("35.00"), "category": "taglio"}
        ]
        result = await list_services(SHOP)
        assert len(result) == 1


class TestFindCustomers:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_by_phone_found(self, mock_exec):
        mock_exec.return_value = [{"id": CUSTOMER, "full_name": "Maria Rossi"}]
        result = await find_customers_by_phone(SHOP, "+39 333 1111111")
        assert len(result) == 1

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_by_phone_empty(self, mock_exec):
        mock_exec.return_value = []
        result = await find_customers_by_phone(SHOP, "+39 000 0000000")
        assert result == []

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_by_name_and_phone(self, mock_exec):
        mock_exec.return_value = [{"id": CUSTOMER, "full_name": "Maria Rossi"}]
        result = await find_customers_by_name_and_phone(SHOP, "Maria", "+39 333 1111111")
        assert len(result) == 1


class TestCreateCustomer:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_without_phone(self, mock_one, mock_void):
        mock_one.return_value = {"id": CUSTOMER, "full_name": "Test"}
        result = await create_customer(SHOP, "Test")
        assert result["full_name"] == "Test"
        mock_void.assert_called_once()  # INSERT customer
        mock_one.assert_called_once()   # SELECT back

    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_with_phone(self, mock_one, mock_void):
        mock_one.side_effect = [
            {"id": CUSTOMER, "full_name": "Test"},  # SELECT customer
            None,  # no existing phone_contact
        ]
        result = await create_customer(SHOP, "Test", "+39 333 9999999")
        assert result["full_name"] == "Test"
        assert mock_void.call_count == 2  # INSERT customer + INSERT phone_contact


class TestCreateAppointment:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_success(self, mock_exec, mock_one, mock_void):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 45, "price_eur": Decimal("35.00")}],  # services
            [],  # no overlap
        ]
        mock_one.return_value = {"id": APPT, "status": "scheduled"}
        start = datetime(2026, 5, 5, 10, 0, tzinfo=_ROME)
        result = await create_appointment(SHOP, CUSTOMER, STAFF, [SVC], start)
        assert result["status"] == "scheduled"

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_conflict(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 45, "price_eur": Decimal("35.00")}],
            [{"id": "existing"}],  # overlap found
        ]
        start = datetime(2026, 5, 5, 10, 0, tzinfo=_ROME)
        with pytest.raises(SlotConflictError):
            await create_appointment(SHOP, CUSTOMER, STAFF, [SVC], start)


class TestCancelAppointment:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_success(self, mock_one, mock_void):
        mock_one.side_effect = [
            {"id": APPT, "status": "scheduled"},   # found
            {"id": APPT, "status": "cancelled"},    # after update
        ]
        result = await cancel_appointment(SHOP, APPT)
        assert result["status"] == "cancelled"

    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_not_cancellable(self, mock_one):
        mock_one.return_value = None
        result = await cancel_appointment(SHOP, APPT)
        assert result is None


class TestListAppointments:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_with_services(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": APPT, "staff_name": "Mirco", "status": "scheduled"}],  # appointments
            [{"service_id": SVC, "service_name": "Taglio", "duration_minutes": 45, "price_eur": Decimal("35.00")}],  # services
        ]
        result = await list_appointments(SHOP)
        assert len(result) == 1
        assert "services" in result[0]
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
pytest tests/booking_engine/test_queries.py -v
```

Expected: FAIL — queries.py still uses Databricks SQL syntax.

- [ ] **Step 3: Rewrite queries.py for PostgreSQL**

```python
"""SQL query functions for all Booking Engine operations (PostgreSQL / Neon)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from booking_engine.db.connection import execute, execute_one, execute_void

_ROME = ZoneInfo("Europe/Rome")


class SlotConflictError(Exception):
    """Raised when a booking would overlap an existing appointment."""


async def get_shop(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM shops WHERE id = $1 AND is_active = true",
        shop_id,
    )


async def list_staff(shop_id: UUID) -> list[dict]:
    return await execute(
        "SELECT id, full_name, role, bio FROM staff "
        "WHERE shop_id = $1 AND is_active = true ORDER BY full_name",
        shop_id,
    )


async def get_staff_services(staff_id: UUID) -> list[dict]:
    return await execute(
        "SELECT s.id, s.service_name, s.duration_minutes, s.price_eur, s.category "
        "FROM services s JOIN staff_services ss ON s.id = ss.service_id "
        "WHERE ss.staff_id = $1 AND s.is_active = true ORDER BY s.service_name",
        staff_id,
    )


async def list_services(shop_id: UUID) -> list[dict]:
    return await execute(
        "SELECT id, service_name, description, duration_minutes, price_eur, category "
        "FROM services WHERE shop_id = $1 AND is_active = true "
        "ORDER BY category, service_name",
        shop_id,
    )


async def find_customers_by_phone(shop_id: UUID, phone: str) -> list[dict]:
    return await execute(
        "SELECT c.id, c.full_name, c.preferred_staff_id, c.notes "
        "FROM customers c JOIN phone_contacts pc ON c.id = pc.customer_id "
        "WHERE c.shop_id = $1 AND pc.phone_number = $2 "
        "ORDER BY pc.last_seen_at DESC",
        shop_id, phone,
    )


async def find_customers_by_name_and_phone(
    shop_id: UUID, name: str, phone: str
) -> list[dict]:
    return await execute(
        "SELECT c.id, c.full_name, c.preferred_staff_id, c.notes "
        "FROM customers c JOIN phone_contacts pc ON c.id = pc.customer_id "
        "WHERE c.shop_id = $1 AND pc.phone_number = $2 "
        "AND LOWER(c.full_name) LIKE LOWER($3) || '%' "
        "ORDER BY pc.last_seen_at DESC",
        shop_id, phone, name,
    )


async def create_customer(
    shop_id: UUID, full_name: str, phone_number: str | None = None,
) -> dict:
    cid = uuid4()
    await execute_void(
        "INSERT INTO customers (id, shop_id, full_name, created_at) "
        "VALUES ($1, $2, $3, NOW())",
        cid, shop_id, full_name,
    )
    customer = await execute_one("SELECT * FROM customers WHERE id = $1", cid)
    if phone_number and customer:
        existing = await execute_one(
            "SELECT id FROM phone_contacts WHERE phone_number = $1 AND customer_id = $2",
            phone_number, cid,
        )
        if existing:
            await execute_void(
                "UPDATE phone_contacts SET last_seen_at = NOW() "
                "WHERE phone_number = $1 AND customer_id = $2",
                phone_number, cid,
            )
        else:
            await execute_void(
                "INSERT INTO phone_contacts (id, phone_number, customer_id, last_seen_at) "
                "VALUES ($1, $2, $3, NOW())",
                uuid4(), phone_number, cid,
            )
    return customer


async def upsert_phone_contact(phone: str, customer_id: UUID) -> None:
    await execute_void(
        "INSERT INTO phone_contacts (id, phone_number, customer_id, last_seen_at) "
        "VALUES ($1, $2, $3, NOW()) "
        "ON CONFLICT (phone_number, customer_id) DO UPDATE SET last_seen_at = NOW()",
        uuid4(), phone, customer_id,
    )


async def get_available_slots(
    shop_id: UUID,
    service_ids: list[UUID],
    start_date: date,
    end_date: date,
    staff_id: UUID | None = None,
) -> list[dict]:
    """Compute available booking slots (Python logic + SQL lookups)."""

    # Get total duration for requested services
    svc_rows = await execute(
        "SELECT SUM(duration_minutes) AS total FROM services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        service_ids,
    )
    total_minutes = int(svc_rows[0]["total"]) if svc_rows and svc_rows[0]["total"] else 0
    if total_minutes == 0:
        return []

    # Find eligible staff (who can do ALL requested services)
    num_services = len(service_ids)
    if staff_id:
        eligible = await execute(
            "SELECT st.id AS staff_id, st.full_name AS staff_name "
            "FROM staff st "
            "WHERE st.shop_id = $1 AND st.is_active = true AND st.id = $2 "
            "AND (SELECT COUNT(DISTINCT ss.service_id) FROM staff_services ss "
            "     WHERE ss.staff_id = st.id AND ss.service_id = ANY($3::uuid[])) = $4",
            shop_id, staff_id, service_ids, num_services,
        )
    else:
        eligible = await execute(
            "SELECT st.id AS staff_id, st.full_name AS staff_name "
            "FROM staff st "
            "WHERE st.shop_id = $1 AND st.is_active = true "
            "AND (SELECT COUNT(DISTINCT ss.service_id) FROM staff_services ss "
            "     WHERE ss.staff_id = st.id AND ss.service_id = ANY($2::uuid[])) = $3",
            shop_id, service_ids, num_services,
        )
    if not eligible:
        return []

    # Generate candidate slots per staff per day
    slots = []
    current = start_date
    while current <= end_date:
        dow = current.weekday()
        for staff_row in eligible:
            sid = staff_row["staff_id"]
            sname = staff_row["staff_name"]
            scheds = await execute(
                "SELECT start_time, end_time FROM staff_schedules "
                "WHERE staff_id = $1 AND day_of_week = $2",
                sid, dow,
            )
            for sched in scheds:
                st_parts = str(sched["start_time"]).split(":")
                et_parts = str(sched["end_time"]).split(":")
                win_start = datetime.combine(current, time(int(st_parts[0]), int(st_parts[1])), tzinfo=_ROME)
                win_end = datetime.combine(current, time(int(et_parts[0]), int(et_parts[1])), tzinfo=_ROME)

                slot_start = win_start
                while slot_start + timedelta(minutes=total_minutes) <= win_end:
                    slot_end = slot_start + timedelta(minutes=total_minutes)
                    slots.append({
                        "staff_id": sid, "staff_name": sname,
                        "slot_start": slot_start, "slot_end": slot_end,
                    })
                    slot_start += timedelta(minutes=30)
        current += timedelta(days=1)

    if not slots:
        return []

    # Filter out slots that overlap existing appointments
    staff_ids = [s["staff_id"] for s in eligible]
    from_ts = datetime.combine(start_date, time(0, 0), tzinfo=_ROME)
    to_ts = datetime.combine(end_date, time(23, 59), tzinfo=_ROME)
    existing = await execute(
        "SELECT staff_id, start_time, end_time FROM appointments "
        "WHERE staff_id = ANY($1::uuid[]) "
        "AND status NOT IN ('cancelled', 'no_show') "
        "AND start_time < $2 AND end_time > $3",
        staff_ids, to_ts, from_ts,
    )

    def overlaps(slot, appt):
        a_start = appt["start_time"] if isinstance(appt["start_time"], datetime) else datetime.fromisoformat(str(appt["start_time"]))
        a_end = appt["end_time"] if isinstance(appt["end_time"], datetime) else datetime.fromisoformat(str(appt["end_time"]))
        s_start = slot["slot_start"]
        s_end = slot["slot_end"]
        if a_start.tzinfo is None:
            a_start = a_start.replace(tzinfo=_ROME)
            a_end = a_end.replace(tzinfo=_ROME)
        return s_start < a_end and s_end > a_start

    available = []
    for slot in slots:
        blocked = any(
            slot["staff_id"] == appt["staff_id"] and overlaps(slot, appt)
            for appt in existing
        )
        if not blocked:
            available.append(slot)

    available.sort(key=lambda s: (s["slot_start"], s["staff_name"]))
    return available


async def create_appointment(
    shop_id: UUID,
    customer_id: UUID,
    staff_id: UUID,
    service_ids: list[UUID],
    start_time: datetime,
    notes: str | None = None,
) -> dict:
    svc_rows = await execute(
        "SELECT id, duration_minutes, price_eur FROM services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        service_ids,
    )
    total_minutes = sum(r["duration_minutes"] for r in svc_rows)
    end_time = start_time + timedelta(minutes=total_minutes)

    # Check for overlapping appointments
    overlap = await execute(
        "SELECT id FROM appointments "
        "WHERE staff_id = $1 AND status NOT IN ('cancelled', 'no_show') "
        "AND start_time < $2 AND end_time > $3",
        staff_id, end_time, start_time,
    )
    if overlap:
        raise SlotConflictError("Time slot conflicts with existing appointment")

    appt_id = uuid4()
    await execute_void(
        "INSERT INTO appointments (id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        appt_id, shop_id, customer_id, staff_id, start_time, end_time, notes,
    )

    for svc in svc_rows:
        await execute_void(
            "INSERT INTO appointment_services (appointment_id, service_id, duration_minutes, price_eur) "
            "VALUES ($1, $2, $3, $4)",
            appt_id, svc["id"], svc["duration_minutes"],
            float(svc["price_eur"]) if svc["price_eur"] else None,
        )

    return await execute_one("SELECT * FROM appointments WHERE id = $1", appt_id)


async def list_appointments(
    shop_id: UUID,
    customer_id: UUID | None = None,
    status: str | None = None,
) -> list[dict]:
    # Build query dynamically based on filters
    conditions = ["a.shop_id = $1"]
    args: list = [shop_id]
    idx = 2

    if customer_id:
        conditions.append(f"a.customer_id = ${idx}")
        args.append(customer_id)
        idx += 1
    if status:
        conditions.append(f"a.status = ${idx}")
        args.append(status)
        idx += 1

    where = " AND ".join(conditions)
    rows = await execute(
        f"SELECT a.*, st.full_name AS staff_name "
        f"FROM appointments a JOIN staff st ON a.staff_id = st.id "
        f"WHERE {where} ORDER BY a.start_time",
        *args,
    )

    for row in rows:
        svcs = await execute(
            "SELECT aps.service_id, s.service_name, aps.duration_minutes, aps.price_eur "
            "FROM appointment_services aps JOIN services s ON aps.service_id = s.id "
            "WHERE aps.appointment_id = $1",
            row["id"],
        )
        row["services"] = svcs

    return rows


async def cancel_appointment(shop_id: UUID, appointment_id: UUID) -> dict | None:
    existing = await execute_one(
        "SELECT * FROM appointments WHERE id = $1 AND shop_id = $2 AND status IN ('scheduled', 'confirmed')",
        appointment_id, shop_id,
    )
    if not existing:
        return None
    await execute_void(
        "UPDATE appointments SET status = 'cancelled' WHERE id = $1",
        appointment_id,
    )
    return await execute_one("SELECT * FROM appointments WHERE id = $1", appointment_id)


async def reschedule_appointment(
    shop_id: UUID,
    appointment_id: UUID,
    new_start_time: datetime,
    new_staff_id: UUID | None = None,
) -> dict | None:
    current = await execute_one(
        "SELECT * FROM appointments WHERE id = $1 AND shop_id = $2 AND status IN ('scheduled', 'confirmed')",
        appointment_id, shop_id,
    )
    if not current:
        return None

    c_start = current["start_time"]
    c_end = current["end_time"]
    if isinstance(c_start, str):
        c_start = datetime.fromisoformat(c_start)
        c_end = datetime.fromisoformat(c_end)
    duration = c_end - c_start
    new_end = new_start_time + duration
    staff = new_staff_id if new_staff_id else current["staff_id"]

    # Cancel old
    await execute_void("UPDATE appointments SET status = 'cancelled' WHERE id = $1", appointment_id)

    # Create new
    new_id = uuid4()
    await execute_void(
        "INSERT INTO appointments (id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        new_id, shop_id, current["customer_id"], staff,
        new_start_time, new_end, current.get("notes"),
    )

    # Copy services
    old_svcs = await execute(
        "SELECT service_id, duration_minutes, price_eur FROM appointment_services WHERE appointment_id = $1",
        appointment_id,
    )
    for svc in old_svcs:
        await execute_void(
            "INSERT INTO appointment_services (appointment_id, service_id, duration_minutes, price_eur) "
            "VALUES ($1, $2, $3, $4)",
            new_id, svc["service_id"], svc["duration_minutes"],
            float(svc["price_eur"]) if svc["price_eur"] else None,
        )

    return await execute_one("SELECT * FROM appointments WHERE id = $1", new_id)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/booking_engine/test_queries.py -v
```

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/queries.py tests/booking_engine/test_queries.py
git commit -m "feat: rewrite all queries for PostgreSQL dialect"
```

---

### Task 6: App Lifespan and Dependencies

**Files:**
- Modify: `booking_engine/api/app.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Update app.py lifespan for asyncpg**

Remove all Databricks SDK auto-detect logic. The new lifespan is simple: read DATABASE_URL from Settings, init pool, yield, close pool.

```python
"""Booking Engine FastAPI application."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from booking_engine.config import Settings
from booking_engine.db.connection import init_connection, close_connection

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    logger.info("Connecting to PostgreSQL...")
    await init_connection(settings)
    logger.info("PostgreSQL connection pool ready")
    yield
    await close_connection()


def create_app() -> FastAPI:
    app = FastAPI(title="Virtual Assistant Booking Engine", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    from booking_engine.api.routes import shops, customers, services, availability, appointments
    app.include_router(shops.router, prefix="/api/v1")
    app.include_router(customers.router, prefix="/api/v1")
    app.include_router(services.router, prefix="/api/v1")
    app.include_router(availability.router, prefix="/api/v1")
    app.include_router(appointments.router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        return {"status": "ok"}
    return app
```

- [ ] **Step 2: Update requirements.txt**

Replace Databricks packages with asyncpg:

```
# Core
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
pydantic>=2.0.0
pydantic-settings>=2.0.0

# HTTP client
httpx>=0.27.0

# PostgreSQL (Neon)
asyncpg>=0.30.0

# Utilities
python-multipart>=0.0.9

# Testing
pytest>=8.0.0
pytest-asyncio>=0.24.0
httpx>=0.27.0
```

- [ ] **Step 3: Install asyncpg**

```bash
pip install asyncpg>=0.30.0
```

- [ ] **Step 4: Commit**

```bash
git add booking_engine/api/app.py requirements.txt
git commit -m "feat: update app lifespan and deps for asyncpg"
```

---

### Task 7: Update Route Tests (Mock Target Adjustment)

**Files:**
- Modify: `tests/booking_engine/test_routes/conftest.py` (if needed)
- Verify: `tests/booking_engine/test_routes/test_*.py`

The route tests mock `booking_engine.db.queries.*` functions, and since the query function **signatures are identical** (same names, same return types), the route tests should pass without changes. This task verifies that.

- [ ] **Step 1: Run all route tests**

```bash
pytest tests/booking_engine/test_routes/ -v
```

Expected: All pass (route tests mock at the query function level, not the DB level).

- [ ] **Step 2: Run availability helper tests**

```bash
pytest tests/booking_engine/test_availability_helper.py -v
```

Expected: All pass (`_add_working_days` is pure Python, no DB dependency).

- [ ] **Step 3: Run model tests**

```bash
pytest tests/booking_engine/test_models.py -v
```

Expected: All pass (Pydantic models are DB-agnostic).

- [ ] **Step 4: Run voice gateway tests**

```bash
pytest tests/voice_gateway/ -v
```

Expected: All pass (voice gateway calls booking engine via HTTP, no DB dependency).

- [ ] **Step 5: Run integration tests**

```bash
pytest tests/integration/ -v
```

Expected: All pass (FakeDB is in-memory, no real DB).

- [ ] **Step 6: Fix any failures, then commit if changes were needed**

If any test needed adjustment, commit:

```bash
git add tests/
git commit -m "fix: update tests for asyncpg migration"
```

---

### Task 8: Run Full Mocked Test Suite

**Files:** None (verification only)

- [ ] **Step 1: Run all tests except live_db**

```bash
pytest tests/ --ignore=tests/live_db -v
```

Expected: All tests pass. This confirms the migration preserves all mocked/unit/integration test behavior.

- [ ] **Step 2: If failures, fix and re-run until green**

---

### Task 9: Neon Database Setup and Seed

**Files:**
- Create: `scripts/setup_neon.sh` (convenience script)

- [ ] **Step 1: Create setup script**

```bash
#!/usr/bin/env bash
# Setup Neon database: create schema + seed data
# Usage: DATABASE_URL=postgresql://... ./scripts/setup_neon.sh

set -euo pipefail

DB_URL="${DATABASE_URL:?Set DATABASE_URL environment variable}"

echo "Creating schema..."
psql "$DB_URL" -f booking_engine/db/sql/01_schema.sql

echo "Seeding data..."
psql "$DB_URL" -f booking_engine/db/sql/02_seed_data.sql

echo "Done. Verifying..."
psql "$DB_URL" -c "SELECT name FROM shops;"
```

- [ ] **Step 2: Run the setup script against your Neon database**

```bash
chmod +x scripts/setup_neon.sh
DATABASE_URL="postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require" ./scripts/setup_neon.sh
```

Expected: Schema created, seed data inserted, shops listed.

- [ ] **Step 3: Commit**

```bash
git add scripts/setup_neon.sh
git commit -m "feat: add Neon database setup script"
```

---

### Task 10: Live DB Tests — Neon Connection

**Files:**
- Modify: `tests/live_db/conftest.py`

- [ ] **Step 1: Rewrite conftest for Neon connection**

Replace Databricks CLI token logic with `DATABASE_URL` environment variable. Keep all seed data UUIDs identical.

```python
"""Live DB test fixtures — real Neon PostgreSQL connection.

These tests run against the actual Neon database.
They are skipped automatically if DATABASE_URL is not set or connection fails.

Run with: DATABASE_URL=postgresql://... pytest tests/live_db/ -v
"""
from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

import pytest

from booking_engine.config import Settings
from booking_engine.db import connection

logger = logging.getLogger(__name__)

# ── Seed data IDs from 02_seed_data.sql (IDENTICAL to Databricks version) ──
SHOP_ID = UUID("a0000000-0000-0000-0000-000000000001")
SHOP_ID_2 = UUID("b0000000-0000-0000-0000-000000000002")

STAFF_MIRCO = UUID("11111111-0000-0000-0000-000000000001")
STAFF_GIULIA = UUID("11111111-0000-0000-0000-000000000002")
STAFF_MARCO = UUID("11111111-0000-0000-0000-000000000003")

SVC_TAGLIO_DONNA = UUID("aaaa0001-0000-0000-0000-000000000001")  # 45 min, €35
SVC_TAGLIO_UOMO = UUID("aaaa0001-0000-0000-0000-000000000002")   # 30 min, €25
SVC_COLORE = UUID("aaaa0001-0000-0000-0000-000000000003")        # 60 min, €55
SVC_PIEGA = UUID("aaaa0001-0000-0000-0000-000000000005")         # 30 min, €20

CUSTOMER_MARIA = UUID("cccc0001-0000-0000-0000-000000000001")
CUSTOMER_LUCA = UUID("cccc0001-0000-0000-0000-000000000002")

PHONE_MARIA = "+39 333 1111111"
PHONE_LUCA = "+39 333 2222222"


def _get_db_settings() -> Settings | None:
    """Build Settings from DATABASE_URL environment variable."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    return Settings(database_url=db_url)


def _try_connect() -> bool:
    """Attempt a real DB connection. Returns True if successful."""
    settings = _get_db_settings()
    if settings is None:
        return False
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(connection.init_connection(settings))
        result = loop.run_until_complete(
            connection.execute("SELECT 1 AS ping")
        )
        loop.close()
        return result is not None and len(result) > 0
    except Exception as e:
        logger.warning("Live DB connection failed: %s", e)
        return False


_db_available = _try_connect()

pytestmark = pytest.mark.skipif(
    not _db_available,
    reason="Neon PostgreSQL connection unavailable (set DATABASE_URL env var)",
)


@pytest.fixture(scope="session")
def db_connection():
    """Ensure we have an active DB connection for the test session."""
    if not _db_available:
        pytest.skip("No DB connection")
    yield connection


@pytest.fixture
def cleanup_customer_ids():
    """Collect customer IDs created during tests for cleanup."""
    ids: list[str] = []
    yield ids
    if ids:
        loop = asyncio.get_event_loop()
        for cid in ids:
            try:
                loop.run_until_complete(connection.execute_void(
                    "DELETE FROM phone_contacts WHERE customer_id = $1",
                    UUID(cid) if isinstance(cid, str) else cid,
                ))
                loop.run_until_complete(connection.execute_void(
                    "DELETE FROM customers WHERE id = $1",
                    UUID(cid) if isinstance(cid, str) else cid,
                ))
            except Exception as e:
                logger.warning("Cleanup failed for customer %s: %s", cid, e)


@pytest.fixture
def cleanup_appointment_ids():
    """Collect appointment IDs created during tests for cleanup."""
    ids: list[str] = []
    yield ids
    if ids:
        loop = asyncio.get_event_loop()
        for aid in ids:
            try:
                loop.run_until_complete(connection.execute_void(
                    "DELETE FROM appointment_services WHERE appointment_id = $1",
                    UUID(aid) if isinstance(aid, str) else aid,
                ))
                loop.run_until_complete(connection.execute_void(
                    "DELETE FROM appointments WHERE id = $1",
                    UUID(aid) if isinstance(aid, str) else aid,
                ))
            except Exception as e:
                logger.warning("Cleanup failed for appointment %s: %s", aid, e)
```

- [ ] **Step 2: Commit**

```bash
git add tests/live_db/conftest.py
git commit -m "feat: rewrite live DB test fixtures for Neon PostgreSQL"
```

---

### Task 11: Live DB Tests — Read Queries

**Files:**
- Modify: `tests/live_db/test_read_queries.py`

The query function signatures are unchanged, but since asyncpg returns native UUID/Decimal types instead of strings, some assertions may need minor adjustments.

- [ ] **Step 1: Run existing read query tests against Neon**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/test_read_queries.py -v
```

- [ ] **Step 2: Fix any type assertion failures**

Common fixes:
- `assert result["id"] == "a0000000-..."` → `assert result["id"] == UUID("a0000000-...")`
- `assert result["price_eur"] == 35.0` → `assert result["price_eur"] == Decimal("35.00")`
- `assert isinstance(result["id"], str)` → `assert isinstance(result["id"], UUID)`

Update assertions to work with native PostgreSQL types. The test logic stays the same — only type comparisons change.

- [ ] **Step 3: Run again — expect PASS**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/test_read_queries.py -v
```

Expected: 14 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/live_db/test_read_queries.py
git commit -m "fix: update read query test assertions for PostgreSQL native types"
```

---

### Task 12: Live DB Tests — Write Queries

**Files:**
- Modify: `tests/live_db/test_write_queries.py`

- [ ] **Step 1: Run existing write query tests against Neon**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/test_write_queries.py -v
```

- [ ] **Step 2: Fix type assertion and cleanup query issues**

The cleanup fixtures now use `$1` params instead of `%(name)s`. Ensure cleanup queries in conftest match. Fix any assertion type mismatches (UUID vs string).

- [ ] **Step 3: Run again — expect PASS**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/test_write_queries.py -v
```

Expected: 10 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/live_db/test_write_queries.py
git commit -m "fix: update write query tests for PostgreSQL"
```

---

### Task 13: Live DB Tests — Availability

**Files:**
- Modify: `tests/live_db/test_availability.py`

- [ ] **Step 1: Run existing availability tests against Neon**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/test_availability.py -v
```

- [ ] **Step 2: Fix any issues (timestamp timezone handling, type assertions)**

asyncpg returns timezone-aware datetimes natively. The slot generation code already uses `_ROME` timezone, so comparisons should work. Fix any edge cases.

- [ ] **Step 3: Run again — expect PASS**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/test_availability.py -v
```

Expected: 9 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/live_db/test_availability.py
git commit -m "fix: update availability tests for PostgreSQL"
```

---

### Task 14: Full Test Suite Verification

**Files:** None (verification only)

- [ ] **Step 1: Run all mocked tests**

```bash
pytest tests/ --ignore=tests/live_db -v
```

Expected: All pass (same count as before migration).

- [ ] **Step 2: Run all live DB tests**

```bash
DATABASE_URL="postgresql://..." pytest tests/live_db/ -v
```

Expected: 33 passed (same count as Databricks version).

- [ ] **Step 3: Run entire suite together**

```bash
DATABASE_URL="postgresql://..." pytest tests/ -v
```

Expected: All tests green. Total should match pre-migration count (96 mocked + 33 live = 129).

---

### Task 15: Cleanup and Final Commit

**Files:**
- Remove: any leftover Databricks-specific imports or references

- [ ] **Step 1: Search for leftover Databricks references**

```bash
grep -r "databricks" booking_engine/ --include="*.py"
grep -r "databricks" tests/ --include="*.py"
```

Expected: No matches. If found, remove them.

- [ ] **Step 2: Verify .env has DATABASE_URL**

Add `DATABASE_URL` to `.env` (or `.env.example`):

```
DATABASE_URL=postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require
OPENAI_KEY=sk-...
```

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "chore: clean up Databricks references, migration complete"
```
