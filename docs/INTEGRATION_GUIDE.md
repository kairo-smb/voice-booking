# Integration Guide — Execution Layer & Control Plane

> **Purpose:** This document is the interface contract between the two products that share a single Neon PostgreSQL database. It defines who owns what, the shared schema, boundaries, and patterns that both repos must respect.

## System Overview

```
                    ┌─────────────────────────────────────────────┐
                    │              Neon PostgreSQL                 │
                    │         (single source of truth)             │
                    └──────────┬───────────────┬──────────────────┘
                               │               │
                    ┌──────────▼───────┐ ┌─────▼──────────────────┐
                    │ EXECUTION LAYER  │ │    CONTROL PLANE       │
                    │ (this repo)      │ │    (other repo)        │
                    │                  │ │                        │
                    │ Voice Gateway    │ │ Shop Owner Dashboard   │
                    │ Booking Engine   │ │ (CRUD for all config)  │
                    │ (read-heavy)     │ │ (write-heavy for       │
                    │                  │ │  setup, read for       │
                    │                  │ │  analytics)            │
                    └──────────────────┘ └────────────────────────┘
                               │               │
                    ┌──────────▼───────┐ ┌─────▼──────────────────┐
                    │ Twilio (inbound  │ │ End-user browser       │
                    │ phone calls)     │ │ (shop owners/managers) │
                    └──────────────────┘ └────────────────────────┘
```

**Execution Layer** — Handles inbound phone calls via Twilio + OpenAI Realtime API. Reads shop config, checks availability, creates/manages appointments. Stateless REST API on AWS Lambda + voice gateway on Fly.io.

**Control Plane** — Web UI for shop owners to manage their business: staff, services, schedules, customers, appointments, and view analytics. This is the admin interface.

---

## Ownership Matrix

| Concern | Execution Layer | Control Plane |
|---------|:-:|:-:|
| **Schema DDL (migrations)** | Owns current schema | Must use same migrations or coordinate |
| **shops** table | Read-only | Full CRUD |
| **staff** table | Read-only | Full CRUD |
| **services** table | Read-only | Full CRUD |
| **staff_services** junction | Read-only | Full CRUD |
| **staff_schedules** table | Read-only | Full CRUD |
| **customers** table | Read + Create | Read + Update + Delete |
| **phone_contacts** table | Read + Create + Upsert | Read + Delete |
| **appointments** table | Read + Create + Cancel + Reschedule | Read + Update status + Delete |
| **appointment_services** junction | Read + Create (via appointment) | Read + Delete (via appointment) |
| **Availability calculation** | Owns the algorithm | Should call Booking Engine API or replicate logic |

### Key Principle

> The database is the integration layer. Both products connect directly to Neon PostgreSQL. There is no API-to-API dependency between the two products (except optionally for availability).

---

## Shared Database Schema

### Connection

Both products connect to the same Neon PostgreSQL database via the **pooler endpoint** (port 5432, transaction mode). This is critical for Lambda compatibility.

```
postgresql://<user>:<pass>@<endpoint>-pooler.<region>.aws.neon.tech/neondb?sslmode=require
```

### Tables

#### `shops`

The root entity. Every other table is scoped to a shop via `shop_id` foreign key. Multi-tenancy is row-level, not schema-level.

```sql
CREATE TABLE shops (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    phone_number    TEXT,               -- Twilio number (set by Control Plane)
    address         TEXT,
    welcome_message TEXT,               -- Voice assistant greeting
    tone_instructions TEXT,             -- "Use informal tu" / "Use formal lei"
    personality     TEXT,               -- Free-text personality description
    special_instructions TEXT,          -- Business rules for the voice assistant
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Integration notes:**
- `welcome_message`, `tone_instructions`, `personality`, `special_instructions` — these fields configure the voice assistant's behavior. The Control Plane writes them; the Execution Layer reads them to build the OpenAI session prompt.
- `phone_number` — the Twilio number assigned to this shop. Control Plane sets it during onboarding; Execution Layer uses it to route inbound calls to the correct `shop_id`.
- `is_active` — when `false`, the Execution Layer should reject calls. Control Plane toggles this.

#### `staff`

```sql
CREATE TABLE staff (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shop_id         UUID NOT NULL REFERENCES shops(id),
    full_name       TEXT NOT NULL,
    role            TEXT,               -- e.g., "senior stylist"
    phone_number    TEXT,               -- Staff personal phone (Control Plane only)
    email           TEXT,               -- Staff email (Control Plane only)
    bio             TEXT,               -- Read by voice assistant for recommendations
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Integration notes:**
- `phone_number` and `email` are private to the Control Plane (staff notifications, not exposed via Booking Engine API).
- `bio` is read by the Execution Layer to help the voice assistant describe staff to callers.
- `is_active = false` staff are excluded from availability calculations.

#### `services`

```sql
CREATE TABLE services (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shop_id          UUID NOT NULL REFERENCES shops(id),
    service_name     TEXT NOT NULL,
    description      TEXT,
    duration_minutes INTEGER NOT NULL,      -- Used for slot calculation
    price_eur        NUMERIC(8,2),
    category         TEXT,                  -- e.g., "Taglio", "Colore"
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Integration notes:**
- `duration_minutes` is load-bearing — the availability algorithm uses it to calculate end times and detect conflicts. Changing it affects future bookings.
- `price_eur` is informational during voice calls but may be used for invoicing in the Control Plane.

#### `staff_services` (junction)

```sql
CREATE TABLE staff_services (
    staff_id   UUID NOT NULL REFERENCES staff(id),
    service_id UUID NOT NULL REFERENCES services(id),
    PRIMARY KEY (staff_id, service_id)
);
```

Determines which staff can perform which services. The availability algorithm filters staff by this table — a staff member only appears as available if they can perform **all** requested services.

#### `staff_schedules`

```sql
CREATE TABLE staff_schedules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    staff_id     UUID NOT NULL REFERENCES staff(id),
    day_of_week  INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),  -- 0=Monday, 6=Sunday
    start_time   TEXT NOT NULL,     -- "HH:MM" format, e.g., "10:00"
    end_time     TEXT NOT NULL,     -- "HH:MM" format, e.g., "18:00"
    UNIQUE (staff_id, day_of_week)
);
```

**Integration notes:**
- `day_of_week` uses ISO convention: **0 = Monday, 6 = Sunday**. Both products must agree on this.
- Times are in **shop-local timezone** (currently hardcoded to `Europe/Rome` in the Execution Layer). This needs to become a `shops.timezone` column when supporting multiple timezones.
- One row per staff per day. Missing day = day off.

#### `customers`

```sql
CREATE TABLE customers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shop_id             UUID NOT NULL REFERENCES shops(id),
    full_name           TEXT NOT NULL,
    email               TEXT,
    preferred_staff_id  UUID REFERENCES staff(id),
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Integration notes:**
- The Execution Layer creates customers during phone calls (voice assistant asks for name). The Control Plane can enrich them later (add email, notes, preferred staff).
- `email` is not currently used by the Execution Layer — reserved for Control Plane features (confirmations, reminders).
- Customers are scoped to a shop. A person calling two different shops is two different customer records.

#### `phone_contacts`

```sql
CREATE TABLE phone_contacts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_number TEXT NOT NULL,
    customer_id  UUID NOT NULL REFERENCES customers(id),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (phone_number, customer_id)
);
```

**Integration notes:**
- The Execution Layer uses Twilio caller ID to look up customers by phone number. On each call, `last_seen_at` is upserted.
- One phone number can map to multiple customers (across shops), and one customer can have multiple phone numbers.
- The Control Plane should be careful when deleting customers — cascade to `phone_contacts` to avoid orphans.

#### `appointments`

```sql
CREATE TABLE appointments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shop_id     UUID NOT NULL REFERENCES shops(id),
    customer_id UUID NOT NULL REFERENCES customers(id),
    staff_id    UUID NOT NULL REFERENCES staff(id),
    start_time  TIMESTAMPTZ NOT NULL,
    end_time    TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled',
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Status lifecycle:**

```
    ┌──────────┐    ┌───────────┐    ┌───────────┐
    │scheduled │───▶│ confirmed │───▶│ completed │
    └──────┬───┘    └─────┬─────┘    └───────────┘
           │              │
           ▼              ▼
    ┌──────────┐    ┌───────────┐
    │cancelled │    │  no_show  │
    └──────────┘    └───────────┘
```

| Status | Set by | Meaning |
|--------|--------|---------|
| `scheduled` | Execution Layer (default on creation) | Booked via phone, not yet confirmed |
| `confirmed` | Control Plane | Shop owner confirmed (e.g., after reminder) |
| `cancelled` | Both (Execution Layer via voice, Control Plane via UI) | Appointment won't happen |
| `no_show` | Control Plane | Customer didn't show up |
| `completed` | Control Plane (future) | Service was delivered |

**Integration notes:**
- The Execution Layer only transitions: `scheduled → cancelled` (via cancel/reschedule).
- The Control Plane manages all other transitions: `scheduled → confirmed`, `confirmed → completed`, `* → no_show`.
- Reschedule = cancel old + create new (the Execution Layer does this atomically).
- `end_time` is calculated from the sum of service durations. It is not user-editable.

#### `appointment_services` (junction)

```sql
CREATE TABLE appointment_services (
    appointment_id  UUID NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    service_id      UUID NOT NULL REFERENCES services(id),
    duration_minutes INTEGER NOT NULL,      -- Snapshot at booking time
    price_eur       NUMERIC(8,2),           -- Snapshot at booking time
    PRIMARY KEY (appointment_id, service_id)
);
```

**Integration notes:**
- `duration_minutes` and `price_eur` are **snapshots** — they capture the values at booking time. If a service's price changes later, existing appointments retain the original price.
- `ON DELETE CASCADE` from `appointments` — deleting an appointment cleans up its services automatically.

---

## Availability Algorithm

The Execution Layer owns the availability calculation. If the Control Plane needs to show availability (e.g., for manual booking via the dashboard), it has two options:

### Option A: Call the Booking Engine API (recommended)

```
GET /api/v1/shops/{shop_id}/availability?service_ids=...&start_date=...&end_date=...
```

This is the simplest integration. The Control Plane treats the Booking Engine as a service.

### Option B: Replicate the logic

The algorithm (in `booking_engine/db/queries.py → get_available_slots`):

1. Sum `duration_minutes` for all requested `service_ids`
2. Find staff who can do **all** requested services (`staff_services` junction, `is_active = true`)
3. For each eligible staff member, for each day in the date range:
   - Look up `staff_schedules` for that `day_of_week`
   - Generate 30-minute slot candidates from `start_time` to `end_time - total_duration`
   - Filter out slots that overlap any existing appointment with `status != 'cancelled'`
4. Return sorted list of `{staff_id, staff_name, slot_start, slot_end}`

**Slot granularity:** 30 minutes. A 45-minute service still starts on 30-minute boundaries (10:00, 10:30, 11:00...).

**Timezone:** All slot calculations use `Europe/Rome`. This is currently hardcoded.

---

## Booking Engine REST API

The full API is available for the Control Plane to consume if needed. Base URL is the Lambda Function URL.

### Endpoints

| Method | Path | Purpose | Used by |
|--------|------|---------|---------|
| `GET` | `/health` | Health check | Both |
| `GET` | `/api/v1/shops/{shop_id}` | Get shop details | Both |
| `GET` | `/api/v1/shops/{shop_id}/services` | List active services | Both |
| `GET` | `/api/v1/shops/{shop_id}/staff` | List active staff | Both |
| `GET` | `/api/v1/shops/{shop_id}/staff/{staff_id}/services` | Staff's services | Both |
| `GET` | `/api/v1/shops/{shop_id}/customers?phone=...&name=...` | Find customers | Both |
| `POST` | `/api/v1/shops/{shop_id}/customers` | Create customer | Execution Layer |
| `GET` | `/api/v1/shops/{shop_id}/availability?service_ids=...&start_date=...&end_date=...` | Check slots | Both |
| `POST` | `/api/v1/shops/{shop_id}/appointments` | Book appointment | Both |
| `GET` | `/api/v1/shops/{shop_id}/appointments?customer_id=...&status=...` | List appointments | Both |
| `PATCH` | `/api/v1/shops/{shop_id}/appointments/{id}/cancel` | Cancel | Both |
| `PATCH` | `/api/v1/shops/{shop_id}/appointments/{id}/reschedule` | Reschedule | Execution Layer |

### Request/Response Contracts

See the Booking Engine's Pydantic models in `booking_engine/api/models.py` for exact field types. Key conventions:

- All IDs are UUID v4 strings
- Datetimes are ISO 8601 with timezone (`2026-04-01T10:00:00+02:00`)
- Prices are decimal strings when in JSON (`"35.00"`)
- Error responses: `{"error": "error_code", "message": "human-readable"}`
- Slot conflict on booking: HTTP 409 with `{"error": "slot_taken", "message": "..."}`

---

## What the Control Plane Must Build

These are capabilities the Execution Layer does **not** provide and the Control Plane must own:

### 1. Shop Management (CRUD)

Full lifecycle for shops. The voice assistant reads `welcome_message`, `tone_instructions`, `personality`, and `special_instructions` to configure the OpenAI session. These fields directly control the voice assistant's behavior.

**Must support:**
- Create/edit shop profile
- Set/change Twilio phone number
- Toggle `is_active` (enables/disables the voice line)
- Configure voice personality fields

### 2. Staff Management (CRUD)

Add/remove staff, assign services, set schedules.

**Must support:**
- Create/edit/deactivate staff
- Manage `staff_services` assignments (which services each staff can do)
- Manage `staff_schedules` (working hours per day of week)
- `is_active` toggle — deactivated staff are excluded from availability immediately

### 3. Service Management (CRUD)

Add/remove services, set pricing and durations.

**Must support:**
- Create/edit/deactivate services
- Category management
- Price changes (note: existing appointment_services retain snapshot prices)

### 4. Customer Management

Enrich customers created by the voice assistant. Manage customer profiles.

**Must support:**
- View/search customers (by name, phone, email)
- Edit customer details (email, notes, preferred_staff_id)
- Merge duplicate customers (same person, different phone calls)
- Delete customer (must cascade phone_contacts and handle appointment FK)

### 5. Appointment Management

View and manage the appointment calendar. The primary view for day-to-day operations.

**Must support:**
- Calendar view (by staff, by day/week)
- Status transitions: `scheduled → confirmed`, `* → no_show`, `* → completed`
- Manual booking (via the dashboard, not voice)
- Cancel/reschedule from the dashboard
- Appointment notes editing

### 6. Analytics (future)

- Appointments per day/week/month
- Revenue per service/staff
- No-show rate
- Peak hours
- Customer frequency

---

## Patterns & Conventions

### Multi-tenancy

Everything is scoped by `shop_id`. The Control Plane must enforce this consistently:
- All queries include `WHERE shop_id = $1`
- Users (shop owners) must only see their own shop's data
- Authentication in the Control Plane maps a user to one or more `shop_id`s

### Soft Deletes

The Execution Layer uses `is_active = false` for soft deletes on `shops`, `staff`, and `services`. The Control Plane should:
- Default list views to `is_active = true`
- Provide an "archived" view for deactivated records
- Never hard-delete records that have dependent appointments

### Timezone Handling

**Current state:** `Europe/Rome` is hardcoded in the Execution Layer.

**Future:** Add `timezone TEXT NOT NULL DEFAULT 'Europe/Rome'` to the `shops` table. Both products should read this field and use it for all time calculations.

All `TIMESTAMPTZ` columns store UTC. Display and slot calculation use the shop's timezone.

### ID Generation

UUIDs are generated by PostgreSQL (`gen_random_uuid()`), not by application code. Both products should let the database generate IDs on INSERT and read them back.

### Concurrent Access

Both products write to `appointments` concurrently. The Execution Layer uses a SELECT-then-INSERT pattern with overlap checking in the same transaction. The Control Plane should either:
1. Use the Booking Engine API for creating appointments (recommended — conflict detection is built in)
2. Replicate the overlap check in its own transaction

**Never** insert into `appointments` without checking for time overlap on the same `staff_id`.

---

## Schema Evolution Rules

Since both products share the schema, migrations must be coordinated:

1. **Additive changes** (new columns with defaults, new tables, new indexes) — either product can apply them, but both must be aware
2. **Breaking changes** (column renames, type changes, dropped columns) — require coordinated release of both products
3. **Migration ownership** — the Execution Layer repo currently owns the DDL files (`booking_engine/db/sql/`). The Control Plane should either:
   - Import/reference the same DDL
   - Use a shared migrations repo/package
   - Or simply agree that one repo owns the schema and the other follows

### Planned Schema Additions

These columns/tables are expected and should be accounted for in Control Plane design:

| Change | Table | Purpose |
|--------|-------|---------|
| `timezone TEXT` | `shops` | Multi-timezone support |
| `email TEXT` | `customers` | Customer notifications |
| `twilio_sid TEXT` | `shops` | Link to Twilio phone number resource |
| `completed` status | `appointments` | Track completed appointments |
| `shop_users` table | new | Map login users to shops (Control Plane auth) |
| `audit_log` table | new | Track who changed what (both products write) |

---

## Deployment Topology

```
┌──────────────────────────────────────────────────────────────────┐
│                         Neon PostgreSQL                          │
│                     (Frankfurt, eu-central-1)                    │
│                                                                  │
│  Pooler: *-pooler.eu-central-1.aws.neon.tech:5432               │
│  Direct: *.eu-central-1.aws.neon.tech:5432                      │
└────────┬──────────────────────────────────────┬──────────────────┘
         │                                      │
         │  Connection via pooler endpoint      │  Connection via pooler endpoint
         │                                      │
┌────────▼─────────────┐              ┌─────────▼──────────────────┐
│ AWS Lambda           │              │ Control Plane (TBD)        │
│ (eu-central-1)       │              │                            │
│                      │              │ Options:                   │
│ Booking Engine API   │              │ - Vercel (Next.js)         │
│ Function URL (HTTPS) │              │ - AWS Amplify (React)      │
│ ~300ms cold start    │              │ - Fly.io (if SSR needed)   │
└────────▲─────────────┘              └────────────────────────────┘
         │
┌────────┴─────────────┐
│ Fly.io (fra)         │
│                      │
│ Voice Gateway        │
│ WebSocket + REST     │
│ ~3s cold start       │
└────────▲─────────────┘
         │
┌────────┴─────────────┐
│ Twilio               │
│ Inbound calls →      │
│ WebSocket to Fly.io  │
└──────────────────────┘
```

### Cost Implications for Control Plane

The current infrastructure runs at $0/month idle. The Control Plane should aim for the same pattern:
- **Vercel free tier** — good for Next.js with serverless functions
- **AWS Amplify free tier** — good for React SPA + Lambda backend
- Both support direct PostgreSQL connections via Neon's pooler

---

## Checklist for Control Plane Development

- [ ] Connect to Neon PostgreSQL via pooler endpoint
- [ ] Implement auth (map users → shop_ids)
- [ ] CRUD for shops, staff, services, staff_services, staff_schedules
- [ ] Customer management (list, search, edit, merge)
- [ ] Appointment calendar view (by staff, by day)
- [ ] Appointment status transitions (confirm, complete, no_show)
- [ ] Manual booking (use Booking Engine API or replicate overlap check)
- [ ] Configure voice assistant personality (welcome_message, tone, etc.)
- [ ] Respect `is_active` soft-delete pattern
- [ ] Scope all queries by `shop_id`
- [ ] Handle `TIMESTAMPTZ` correctly (store UTC, display in shop timezone)
- [ ] Test concurrent booking scenarios
