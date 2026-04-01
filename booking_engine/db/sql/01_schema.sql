-- Virtual Assistant Booking Engine — PostgreSQL Schema
-- Target: Neon PostgreSQL
-- Run once to create schema. Idempotent (IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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

CREATE TABLE IF NOT EXISTS staff_services (
  staff_id    UUID    NOT NULL REFERENCES staff(id),
  service_id  UUID    NOT NULL REFERENCES services(id),
  PRIMARY KEY (staff_id, service_id)
);

CREATE TABLE IF NOT EXISTS staff_schedules (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  staff_id      UUID        NOT NULL REFERENCES staff(id),
  day_of_week   INTEGER     NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
  start_time    TEXT        NOT NULL,
  end_time      TEXT        NOT NULL,
  UNIQUE (staff_id, day_of_week)
);

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

CREATE TABLE IF NOT EXISTS phone_contacts (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_number  TEXT        NOT NULL,
  customer_id   UUID        NOT NULL REFERENCES customers(id),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (phone_number, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_phone_contacts_phone ON phone_contacts(phone_number);

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

CREATE TABLE IF NOT EXISTS appointment_services (
  appointment_id  UUID        NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
  service_id      UUID        NOT NULL REFERENCES services(id),
  duration_minutes INTEGER    NOT NULL,
  price_eur       NUMERIC(8,2),
  PRIMARY KEY (appointment_id, service_id)
);
