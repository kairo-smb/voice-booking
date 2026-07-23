#!/usr/bin/env bash
# New test path: real SIP call against the deployed QA app, no local server.
# Prints the dial-string for a softphone (Linphone/Zoiper/pjsua), and offers
# to flip on ENABLE_CALL_SUPERVISOR + CALL_SUPERVISOR_VERBOSE_LOGGING on QA
# for the duration of the test (turned back off on exit).
# Usage: ./scripts/run_sip_test.sh <shop_id>

set -euo pipefail
cd "$(dirname "$0")/.."

SHOP_ID="${1:?usage: run_sip_test.sh <shop_id>}"
QA_APP="kairo-booking-engine-qa"

set -a
source .env
set +a
export PYTHONPATH=.

python scripts/print_sip_test_uri.py "$SHOP_ID"

read -rp "Flip on ENABLE_CALL_SUPERVISOR + CALL_SUPERVISOR_VERBOSE_LOGGING on QA for this test? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
  fly secrets set ENABLE_CALL_SUPERVISOR=true CALL_SUPERVISOR_VERBOSE_LOGGING=true --app "$QA_APP"
  trap 'fly secrets unset ENABLE_CALL_SUPERVISOR CALL_SUPERVISOR_VERBOSE_LOGGING --app "$QA_APP"' EXIT
fi

echo "Place the call now, then watch logs (Ctrl+C to stop watching and revert flags):"
fly logs -a "$QA_APP"
