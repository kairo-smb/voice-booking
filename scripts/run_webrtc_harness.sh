#!/usr/bin/env bash
# Old test path: local WebRTC harness — mints an ephemeral OpenAI session,
# serves a browser page that talks to it directly (your mic/speakers).
# Usage: ./scripts/run_webrtc_harness.sh [port]

set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8765}"

set -a
source .env
set +a
export PYTHONPATH=.

echo "Open http://localhost:${PORT}/ once uvicorn starts, then click Start Call."
uvicorn scripts.voice_test_server:app --port "$PORT"
