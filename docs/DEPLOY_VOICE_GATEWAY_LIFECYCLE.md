# Voice Gateway Call-Lifecycle — Deploy Guide

Manual deploy steps for activating call persistence on the Fly.io voice gateway. Read after Plan 3 has merged to `main`.

## What this deploys

Once live, every inbound call to the gateway will:
- Insert a row into `voice_agent.calls` on token issuance (with caller→customer reconciliation against `business_app_core.phone_contacts`).
- Stream each turn into `voice_agent.call_transcripts` when the browser/Twilio client POSTs to `/api/v1/realtime/transcript`.
- Log function calls into `voice_agent.call_events` from `/api/v1/realtime/action`.
- On `POST /api/v1/realtime/end`, the OpenAI Responses API classifies `{outcome, outcome_reason, summary}` and the call row is finalized.

The webapp's Inbox UI (Plan 2) becomes meaningfully populated from this point on.

## Prerequisites

- voice-booking Booking Engine already deployed (Plan 1).
- Fly.io app for the voice gateway already exists. `fly secrets list` should show `OPENAI_KEY` and `BOOKING_ENGINE_URL`.
- A Neon `DATABASE_URL` (pooler endpoint).

## 1. Set Fly secrets

```bash
fly secrets set \
  DATABASE_URL='postgresql://neondb_owner:...@...-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require' \
  OPENAI_CLASSIFIER_MODEL=gpt-4o-mini
```

`OPENAI_CLASSIFIER_MODEL` is optional (defaults to `gpt-4o-mini`). The classifier runs **once per call** at hangup — very small token cost.

## 2. Deploy

```bash
cd /Users/mirco/Documents/kairo/voice-booking
./scripts/deploy-voice.sh
```

The script builds the image and rolls it on Fly. Cold start is a few seconds; new asyncpg pool init adds ~100ms.

Verify health:
```bash
fly logs | grep -E 'Voice gateway DB pool ready|started'
curl -s https://<your-app>.fly.dev/health
```

## 3. End-to-end smoke test (browser/WebRTC)

1. Open the deployed gateway URL (`https://<your-app>.fly.dev`) in a browser — the static test UI loads.
2. Pick a shop, start a call, exchange a few turns.
3. End the call (close tab counts only if the client POSTs `/end` on unload — verify with your client logic).
4. In the webapp Inbox → **Conversazioni** tab, the call should appear with:
   - Caller number (or `+0000` if not provided)
   - Customer match badge (likely `unmatched` for first-time callers)
   - An outcome chip
   - Transcript visible when you open the drawer
5. If the call invoked `book_appointment` successfully, the **Go to appointment** link in the drawer should resolve.

## 4. Browser/Twilio client wiring (REQUIRED for transcripts/outcome to flow)

The gateway exposes new endpoints that the client must call:

- After `/api/v1/realtime/token` returns, store the `call_id` field — every subsequent POST to `/transcript`, `/end`, and `/action` should include it.
- Each completed turn (assistant or caller speech) → `POST /api/v1/realtime/transcript` with `{ call_id, role, text }`.
- Hangup → `POST /api/v1/realtime/end` with `{ call_id }` — this triggers classification.

The static test UI (`voice_gateway/static/index.html`) was NOT updated in this plan — you'll need to add these calls to it manually OR to whichever production client (Twilio Media Streams handler, custom WebRTC frontend) you use.

If you skip the client wiring, the calls table will still get a row at token issuance (with `customer_match`), but `summary`, `outcome`, and transcripts will be empty. The webapp UI handles that gracefully.

## 5. Optional headers for richer call rows

The `/token` endpoint reads two headers from the caller's request:
- `x-caller-number`: E.164 phone number (falls back to `+0000` if missing — bad for reconciliation).
- `x-twilio-call-sid`: Twilio call SID (used for UNIQUE deduplication).

When Twilio Media Streams hits the gateway via the WebSocket handshake, set these on the upgrade request.

## 6. Rollback

The gateway change is contained to:
- New endpoints `/transcript` and `/end`
- A new module `voice_gateway/call_lifecycle.py`
- An asyncpg pool that's optional (no `DATABASE_URL` ⇒ no pool, calls run as before without persistence)

To roll back:
```bash
fly secrets unset DATABASE_URL  # disables persistence
fly deploy --image <previous-image-tag>
```

The pool init is skipped when `DATABASE_URL` is unset — the gateway returns to its old behavior with zero code changes needed.

## Future enhancements (out of scope)

- Twilio Media Streams + audio recording (not currently wired).
- Live "call in progress" view (would require websocket push from gateway → webapp).
- Materialized analytics view (currently the booking engine recomputes on every request — fine for low volumes).
