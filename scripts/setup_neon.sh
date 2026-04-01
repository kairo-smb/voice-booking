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
