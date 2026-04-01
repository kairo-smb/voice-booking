#!/usr/bin/env bash
#
# Deploy Voice Gateway to Fly.io.
#
# Prerequisites:
#   - flyctl installed (brew install flyctl)
#   - Authenticated (fly auth login)
#
# First run creates the app. Subsequent runs just deploy.
#
# Usage:
#   ./scripts/deploy-voice.sh
#
#   # Set secrets (first time):
#   fly secrets set OPENAI_KEY=sk-... BOOKING_ENGINE_URL=https://xxx.lambda-url.eu-central-1.on.aws/
#
set -euo pipefail

APP_NAME="virtual-assistant-voice"

echo "=== Voice Gateway Fly.io Deployment ==="
echo "App: ${APP_NAME}"
echo ""

# Check if app exists
if ! fly apps list 2>/dev/null | grep -q "${APP_NAME}"; then
    echo "Creating Fly.io app: ${APP_NAME}"
    fly apps create "${APP_NAME}"
fi

echo "Deploying..."
fly deploy

echo ""
echo "=== Deployment complete ==="
echo "URL: https://${APP_NAME}.fly.dev"
echo ""
echo "Check status:  fly status"
echo "View logs:     fly logs"
echo "Set secrets:   fly secrets set OPENAI_KEY=sk-... BOOKING_ENGINE_URL=https://..."
