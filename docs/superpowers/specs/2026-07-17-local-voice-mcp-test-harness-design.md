# Local Voice + MCP Test Harness — Design

**Status:** Approved (verbally, across this conversation) — formalized here for
the record and to hand off to writing-plans.

## Motivation

Testing the real Twilio→OpenAI→MCP call chain requires a live phone call,
which is currently blocked on getting a working EU (Estonia) Twilio number
and clearing the same-account-call restriction discovered during live
testing (see project memory `project_telnyx_to_twilio_migration.md` and this
session's Twilio Dev Phone troubleshooting). Rather than wait on that, this
harness lets a developer have a real, spoken conversation with the actual
voice agent — same prompt, same tone config, same tools, same database — with
no telephony involved at all.

## Architecture

A small local server mints an OpenAI Realtime **ephemeral session** using the
exact same `build_accept_payload()` function the real Twilio webhook
(`booking_engine/api/routes/voice_openai.py`) uses — so prompt, tone, and
tools are byte-for-byte what a real call would get. Critically, the payload
is built **with** `mcp_server_url` set (unlike `scripts/chat_agent.py`, which
deliberately omits it to use a local, non-MCP fallback) — pointing at
`https://kairo-booking-engine-qa.fly.dev/mcp`, the QA Fly app's already-live
MCP server, using a `mint_call_token()`-signed bearer for that call.

A single static HTML page then opens a `RTCPeerConnection` directly from the
browser to OpenAI's Realtime WebRTC endpoint, using that ephemeral secret —
your mic and speakers become the "phone." When the agent invokes a tool,
OpenAI calls the QA MCP server directly (server-to-server), exactly as it
would for a real Twilio-originated call. The local server has **no
tool-execution responsibility at all** — it only mints the session and
serves the page.

Because tool execution hits the real, already-deployed QA MCP server, write
tools (`create_booking`, etc.) execute for real against the **QA** Neon
branch — not stubbed, and not touching production. This is exactly why the
QA Neon branch + Fly app were built in the preceding work.

## Components

### `scripts/voice_test_server.py` (new)

A small FastAPI app (`uvicorn scripts.voice_test_server:app`), run locally,
never deployed:

- `GET /` — serves `scripts/voice_test_static/index.html`.
- `POST /session` — body `{shop_id, caller_phone}` (both optional, default to
  `DEMO_SHOP_ID` from `.env` and an empty caller string). Runs
  `resolve_caller()` → `insert_call()` → `build_accept_payload(config, policy,
  resolution, model=settings.openai_realtime_model,
  mcp_server_url="https://kairo-booking-engine-qa.fly.dev/mcp",
  mcp_token=mint_call_token(shop_id=..., call_id=..., secret=settings.openai_tool_secret))`,
  then calls OpenAI's `POST https://api.openai.com/v1/realtime/sessions` REST
  endpoint server-side (using the real `OPENAI_API_KEY` from `.env`) to mint
  an ephemeral `client_secret`. Returns `{client_secret, call_id}` as JSON.

### `scripts/voice_test_static/index.html` (new)

A single self-contained page (inline `<script>`, no build step, no external
JS dependency — WebRTC session negotiation with OpenAI's Realtime endpoint is
a plain `fetch()` POST of an SDP offer, no SDK required):

- A form: shop_id (prefilled with `DEMO_SHOP_ID`), caller phone (free text).
- A "Start Call" button: calls `POST /session`, then:
  1. `navigator.mediaDevices.getUserMedia({audio: true})` for the mic.
  2. Creates an `RTCPeerConnection`, adds the mic track, creates a data
     channel (`oai-events`) for realtime events (transcripts, tool-call
     notifications — informational only, since the local page does **not**
     execute tools).
  3. Creates an SDP offer, POSTs it to
     `https://api.openai.com/v1/realtime?model=<model>` with
     `Authorization: Bearer <client_secret>` and
     `Content-Type: application/sdp`, sets the returned SDP answer as the
     remote description.
  4. Attaches the remote audio track to an `<audio autoplay>` element.
- A simple transcript log area (renders `response.audio_transcript.delta` /
  similar events from the data channel) so you can see what was said/done,
  not just hear it.
- A "Hang Up" button: closes the peer connection.

## Data Flow

1. Open `http://localhost:8001/`, fill in shop_id (defaulted) and a caller
   phone, click Start Call.
2. Browser → local server `POST /session` → local server calls OpenAI
   (server-side, real API key) → ephemeral `client_secret` returned to
   browser.
3. Browser → OpenAI (direct WebRTC, ephemeral secret) → mic/speaker
   conversation begins.
4. Agent needs a tool → OpenAI → QA Fly app's `/mcp` (server-to-server,
   `mcp_token` bearer) → real `/voice/tools/*` handlers → QA Neon branch.
5. Tool result flows back OpenAI → browser (audio + transcript), same as a
   real call.

## Safety / Scope

- Local-only: the small FastAPI server and the static page never leave your
  machine. Only the (already-existing, already-public) QA Fly MCP endpoint
  is remote.
- No stubbing: write tools execute for real, but only against the **QA**
  Neon branch (restored from production, not production itself).
- Out of scope: no changes to the real Twilio webhook, no SIP, no changes to
  `chat_agent.py` (kept as-is for text-only, non-MCP testing — this harness
  is a separate, voice-specific tool).
- Out of scope: production hardening, auth on the local server (it only
  binds to localhost and is never deployed), and any UI polish beyond a
  functional test page.

## Testing

Given this is a manual test tool (its whole purpose is a human listening and
speaking), there is no automated test suite for the "conversation" itself.
What's verifiable programmatically:
- `POST /session` returns a valid-shaped response (`client_secret` present,
  `call_id` is a UUID) — a quick unit test with a mocked OpenAI response.
- `build_accept_payload()` is called with `mcp_server_url` pointing at the
  QA Fly app (not production, not omitted) — a unit test asserting the
  constructed payload's `tools` list has `type: "mcp"` and the right
  `server_url`.
