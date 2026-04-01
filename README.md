# Virtual Assistant — Voice Booking System

AI-powered real-time voice assistant for appointment booking, built with FastAPI, Neon PostgreSQL, and OpenAI Realtime API.

## Architecture

```
Customer (browser/WebRTC) ←→ OpenAI Realtime API (voice + LLM)
                                    ↕ function calls
                              Voice Gateway (Fly.io)
                                    ↕ HTTPS
                              Booking Engine (AWS Lambda)
                                    ↕ SQL
                              Neon PostgreSQL (serverless)
```

**Booking Engine** — Stateless REST API for shops, staff, services, customers, availability, and appointments. Backed by Neon PostgreSQL via asyncpg. Deployed as an AWS Lambda container with Mangum ASGI adapter and Function URL (~300ms cold start, $0 idle).

**Voice Gateway** — Generates ephemeral OpenAI Realtime API tokens with session config (tools, voice, VAD) and proxies function calls from the browser to the Booking Engine. Deployed on Fly.io with auto-stop machines ($0 idle).

**OpenAI Realtime API** — Handles STT, LLM reasoning, TTS, and voice activity detection natively via WebRTC. The browser connects directly to OpenAI for audio streaming.

## Project Structure

```
booking_engine/          # REST API (FastAPI + Neon PostgreSQL)
├── api/routes/          # Shops, customers, services, availability, appointments
├── db/                  # asyncpg connection pool + queries
│   └── sql/             # Schema DDL + seed data
├── config.py            # Settings (DATABASE_URL, pool sizes)
├── requirements.txt     # Service dependencies
└── Dockerfile           # AWS Lambda container image

voice_gateway/           # Realtime API gateway (FastAPI)
├── api/routes/
│   └── realtime.py      # Token generation + function call proxy
├── clients/
│   └── booking_client.py # Booking Engine HTTP client
├── static/
│   └── index.html       # WebRTC phone-call UI
├── config.py            # Settings (BOOKING_ENGINE_URL, OPENAI_KEY)
├── requirements.txt     # Service dependencies
└── Dockerfile           # Fly.io container image

lambda_handler.py        # Mangum entry point for AWS Lambda
fly.toml                 # Fly.io app configuration
scripts/
├── setup_neon.sh        # Initialize Neon database (schema + seed)
├── deploy-booking.sh    # Deploy Booking Engine to AWS Lambda
└── deploy-voice.sh      # Deploy Voice Gateway to Fly.io
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
# Booking Engine
uvicorn booking_engine.api.app:create_app --factory --port 8000

# Voice Gateway (in a separate terminal)
BOOKING_ENGINE_URL=http://localhost:8000 OPENAI_KEY=sk-... \
  uvicorn voice_gateway.api.app:create_app --factory --port 8001
```

Open http://localhost:8001 to access the phone-call UI.

### 4. Run tests

```bash
# Unit + integration tests (no database needed)
pytest tests/ --ignore=tests/live_db -v

# Live database tests (requires DATABASE_URL)
DATABASE_URL=postgresql://... pytest tests/live_db/ -v
```

## Deployment

### Booking Engine → AWS Lambda

```bash
AWS_REGION=eu-central-1 DATABASE_URL=postgresql://... ./scripts/deploy-booking.sh
```

Creates ECR repo, IAM role, Lambda function, and public Function URL on first run. Subsequent runs just update the container image.

### Voice Gateway → Fly.io

```bash
fly auth login
./scripts/deploy-voice.sh
fly secrets set OPENAI_KEY=sk-... BOOKING_ENGINE_URL=https://xxx.lambda-url.eu-central-1.on.aws/
```

### Cost

| Component | Idle | Active |
|-----------|------|--------|
| Lambda (Booking Engine) | $0 | ~$0-1/mo |
| Fly.io (Voice Gateway) | $0 | $0 (free tier) |
| Neon PostgreSQL | $0 | $0 (free tier) |
| **Total infrastructure** | **$0** | **$0-1/mo** |

Plus usage-based: Twilio (per-minute), OpenAI Realtime (per-minute).

## Design Docs

- [Design Spec](docs/superpowers/specs/2026-03-25-hair-salon-voice-assistant-design.md)
- [Cloud Deployment Plan](docs/superpowers/plans/2026-04-01-cloud-deployment.md)
- [Neon Migration Plan](docs/superpowers/plans/2026-04-01-neon-migration.md)
