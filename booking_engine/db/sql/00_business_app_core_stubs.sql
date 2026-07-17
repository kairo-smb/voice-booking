-- Minimal stubs for business_app_core (shared webapp schema)
-- Needed only for migration testing in isolation. In production (Neon),
-- the real business_app_core schema is created/managed by the webapp.
-- This file is idempotent (IF NOT EXISTS).

CREATE SCHEMA IF NOT EXISTS business_app_core;

-- Minimal shops table (real version has more columns, but these are sufficient for FKs)
CREATE TABLE IF NOT EXISTS business_app_core.shops (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Minimal customers table (real version has more columns)
CREATE TABLE IF NOT EXISTS business_app_core.customers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id UUID NOT NULL REFERENCES business_app_core.shops(id),
  phone TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Minimal appointments table (real version has more columns)
CREATE TABLE IF NOT EXISTS business_app_core.appointments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id UUID NOT NULL REFERENCES business_app_core.shops(id),
  customer_id UUID REFERENCES business_app_core.customers(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Minimal ai_token_log table (real version is managed by webapp)
CREATE TABLE IF NOT EXISTS business_app_core.ai_token_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id UUID NOT NULL REFERENCES business_app_core.shops(id),
  credits_used INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
