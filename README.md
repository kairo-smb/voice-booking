# Kairo Voice Booking — Booking Engine

AI-powered real-time voice assistant for appointment booking, built with FastAPI, Neon PostgreSQL, and the OpenAI Realtime API. Single Fly.io service; no separate gateway.

## Architecture

```
Caller (phone) → Twilio (TwiML) → OpenAI Realtime API (native SIP, STT/LLM/TTS)
                                          ↕ MCP tool calls (in-process ASGI, /mcp)
                                    Booking Engine (Fly.io)
                                          ↕ SQL (asyncpg)
                                    Neon PostgreSQL (serverless)
```

**Booking Engine** (`booking_engine/`) — the only deployed service. A FastAPI app that:
- serves the plain REST API (shops, staff, services, customers, appointments) against the shared `business_app_core` Neon schema — read/write boundaries are documented in `docs/knowledge/database.md`;
- accepts inbound Twilio calls (`voice_twiml.py`), dials them via `<Dial><Sip>` straight into OpenAI's native SIP gateway, and handles the `realtime.call.incoming` webhook (`voice_openai.py`) to accept the call with the salon's persona, plus the agent rules and the MCP tool server of marketing-engine's customer agents (`/customer-agents/voice/*`) — this service no longer executes any agent tool;
- owns the agent session row (`voice_agent.calls`) and its writes (`/sessions/*`: customer link, escalation, outcome), which the customer agents call back into;
- persists calls, transcripts, and voice-specific config in its own `voice_agent` schema (DDL in `booking_engine/db/sql/`), while treating `business_app_core` as ground truth it never alters.

Deployed on Fly.io with auto-stop machines ($0 idle). QA and production are separate Fly apps (`fly.toml` / `fly.qa.toml`), both built from the same `booking_engine/Dockerfile.fly`.

## Project structure

```
booking_engine/
├── api/routes/       # REST + voice webhooks + session endpoints for the customer agents
├── services/         # prompt_assembler, realtime_session, call_supervisor, messaging/, ...
├── db/                # asyncpg pool + queries
│   └── sql/           # voice_agent schema migrations (03+; 01/02 are a local-only bootstrap pair)
├── clients/           # OpenAI Realtime, Twilio numbers, push notifications
├── config.py          # Settings (env vars)
└── Dockerfile.fly      # Fly.io image

scripts/                # local dev/test harnesses — see each script's docstring
tests/                  # pytest suite; tests/live_db/* needs a real DATABASE_URL
fly.toml / fly.qa.toml  # Fly.io app configuration (prod / QA)
docs/knowledge/         # human-oriented docs site (Docsify) — architecture, database,
                        # voice-agent logic, providers, operations, API reference
```

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up the database

```bash
export DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"
./scripts/setup_neon.sh
```

### 3. Run locally

```bash
uvicorn booking_engine.api.app:create_app --factory --port 8000
```

### 4. Test a call without a phone

See `docs/knowledge/operations.md` ("Testing a real call without a phone") for dialing the real OpenAI SIP path with a softphone, or use one of the local harnesses:

```bash
# Browser/WebRTC harness (mic + speakers, mints a real session). Tools and
# rules come from marketing-engine: point DATABASE_URL and MARKET_INTEL_API_URL
# at the same environment (QA).
./scripts/run_webrtc_harness.sh
```

(`chat_agent.py` and `simulate_call.py` drove this repo's own `/voice/tools/*`
and were deleted with them on 2026-09-28.)

### 5. Run tests

```bash
# Unit + integration tests (no database needed)
pytest tests/ --ignore=tests/live_db -v

# Live database tests (requires DATABASE_URL, ideally an ephemeral Neon branch)
DATABASE_URL=postgresql://... pytest tests/live_db/ -v
```

## Deployment

Deploys automatically to Fly.io via GitHub Actions on push to `main` (production, `deploy-fly-prod.yml`) or `QA` (`deploy-qa.yml`) — both run tests and migration checks against a throwaway Neon branch before touching the real one. Manual deploy:

```bash
fly auth login
flyctl deploy --config fly.toml      # production
flyctl deploy --config fly.qa.toml   # QA
```

See `docs/knowledge/operations.md` for env vars, secrets, and the post-deploy smoke test.

### Cost

$0 idle: Fly.io free tier (auto-stop machines) + Neon free tier. Usage-based costs: Twilio (per-minute + number rental), OpenAI Realtime (per-minute).

## Documentation

`docs/knowledge/` is a [Docsify](https://docsify.js.org/) site covering architecture, the database ownership contract, voice-agent domain logic, providers, operations, and the full API reference — `npx --yes serve docs/knowledge` to browse it. `AGENTS.md` is the running log of architectural decisions, incidents, and what's still open — read it before making non-trivial changes.
