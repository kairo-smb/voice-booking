#!/usr/bin/env bash
set -euo pipefail

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL must be set" >&2
  exit 1
fi

# 01_schema.sql and 02_seed_data.sql are a from-scratch LOCAL bootstrap pair
# (unqualified table names, fake demo data) — never meant to run against a
# real Neon branch, which already has the full schema under business_app_core.
# Only 03+ are real, schema-qualified migrations safe to re-apply there.
for f in booking_engine/db/sql/*.sql; do
  base=$(basename "$f")
  if [ "$base" = "01_schema.sql" ] || [ "$base" = "02_seed_data.sql" ]; then
    continue
  fi
  echo "Applying $f..."
  psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f"
done

echo "All migrations applied."
