#!/usr/bin/env bash
set -euo pipefail

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL must be set" >&2
  exit 1
fi

for f in booking_engine/db/sql/*.sql; do
  echo "Applying $f..."
  psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f"
done

echo "All migrations applied."
