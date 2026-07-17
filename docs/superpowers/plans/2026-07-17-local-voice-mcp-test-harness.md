# Local Voice + MCP Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Task 4 is human-in-the-loop (real mic/speakers, real API calls, real cost) — do not dispatch it to a subagent.**

**Goal:** Let a developer have a real, spoken conversation with the actual voice agent (same prompt/tone/tools/DB as a real Twilio call) from a browser, with zero telephony involved — tool calls hit the real QA Fly app's MCP server.

**Architecture:** A tiny local FastAPI server mints an OpenAI Realtime ephemeral client secret using the exact `build_accept_payload()` function the real Twilio webhook uses, pointed at the QA Fly app's MCP endpoint. A single static HTML page then opens a WebRTC connection directly from the browser to OpenAI using that secret — mic in, speaker out, tool calls flow through OpenAI to the QA MCP server server-to-server.

**Tech Stack:** FastAPI, httpx (server-side ephemeral session minting), vanilla JS + WebRTC (browser, no build step, no external JS dependency).

---

### Task 1: `create_ephemeral_session()` OpenAI client function

**Files:**
- Modify: `booking_engine/clients/openai_realtime.py`

This repo's existing OpenAI Realtime client (`accept_sip_call`, in the same file) has no dedicated unit test of its own — it's a thin `httpx` passthrough, verified only through its caller's test (`tests/voice_gateway/test_voice_openai_incoming.py`, which mocks `accept_sip_call` at the import level). `create_ephemeral_session` is the same shape of thing (a thin `httpx` passthrough to a different OpenAI endpoint), so it follows the same precedent: no dedicated unit test here — Task 2's test mocks this function at the import level instead, which is where the actual branching logic (and thus the real test value) lives.

- [ ] **Step 1: Add the function**

Open `booking_engine/clients/openai_realtime.py`. It currently looks like this:

```python
"""Thin client for OpenAI Realtime SIP call control."""
from __future__ import annotations

from typing import Any

import httpx

_ACCEPT_URL = "https://api.openai.com/v1/realtime/calls/{call_id}/accept"


async def accept_sip_call(
    *, call_id: str, payload: dict[str, Any], api_key: str,
) -> bool:
    """Accept an incoming SIP call with the given session config. True on 2xx."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _ACCEPT_URL.format(call_id=call_id),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=payload,
        )
    return 200 <= resp.status_code < 300
```

Add this function to the end of the file (keep everything above unchanged):

```python
_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


async def create_ephemeral_session(
    *, session_config: dict[str, Any], api_key: str,
) -> dict[str, Any]:
    """Mint an ephemeral client secret for a browser WebRTC Realtime session.

    `session_config` is the same session-config dict `build_accept_payload()`
    produces (type/model/instructions/voice/tools). Raises
    `httpx.HTTPStatusError` on a non-2xx response.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _CLIENT_SECRETS_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"session": session_config},
        )
        resp.raise_for_status()
    return resp.json()
```

- [ ] **Step 2: Sanity-check it imports and the module still loads**

```bash
python3 -c "from booking_engine.clients.openai_realtime import create_ephemeral_session, accept_sip_call; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add booking_engine/clients/openai_realtime.py
git commit -m "feat(voice-test): add create_ephemeral_session for browser WebRTC test harness"
```

---

### Task 2: `scripts/voice_test_server.py` — session-minting server

**Files:**
- Create: `scripts/voice_test_server.py`
- Test: `tests/voice_gateway/test_voice_test_server.py`

- [ ] **Step 1: Write the failing test**

Create `tests/voice_gateway/test_voice_test_server.py`:

```python
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import scripts.voice_test_server as server
from booking_engine.services.identity_resolver import ResolutionResult


def _config():
    return {"welcome_message": "hi", "tone_instructions": "", "personality": "",
            "special_instructions": ""}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEMO_SHOP_ID", str(uuid4()))
    return TestClient(server.app)


def test_session_returns_client_secret_and_targets_qa_mcp(client):
    shop_id = uuid4()
    build_payload_mock = AsyncMock(
        return_value={"type": "realtime", "model": "gpt-realtime"}
    )
    create_session_mock = AsyncMock(
        return_value={"value": "ek_test123", "expires_at": 1}
    )

    with patch("scripts.voice_test_server.get_config",
               new=AsyncMock(return_value=_config())), \
         patch("scripts.voice_test_server.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "hi"})), \
         patch("scripts.voice_test_server.resolve_caller",
               new=AsyncMock(return_value=ResolutionResult(is_anonymous=True, matches=[]))), \
         patch("scripts.voice_test_server.insert_call",
               new=AsyncMock(return_value=uuid4())), \
         patch("scripts.voice_test_server.build_accept_payload", new=build_payload_mock), \
         patch("scripts.voice_test_server.create_ephemeral_session", new=create_session_mock):
        resp = client.post("/session", json={"shop_id": str(shop_id), "caller_phone": "+391234"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["client_secret"] == "ek_test123"
    assert "call_id" in body

    _, kwargs = build_payload_mock.call_args
    assert kwargs["mcp_server_url"] == "https://kairo-booking-engine-qa.fly.dev/mcp"
    assert kwargs["mcp_token"]


def test_session_defaults_shop_id_from_env(client, monkeypatch):
    demo_shop_id = str(uuid4())
    monkeypatch.setenv("DEMO_SHOP_ID", demo_shop_id)
    build_payload_mock = AsyncMock(return_value={"type": "realtime"})
    resolve_mock = AsyncMock(return_value=ResolutionResult(is_anonymous=True, matches=[]))

    with patch("scripts.voice_test_server.get_config", new=AsyncMock(return_value=_config())), \
         patch("scripts.voice_test_server.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "hi"})), \
         patch("scripts.voice_test_server.resolve_caller", new=resolve_mock), \
         patch("scripts.voice_test_server.insert_call", new=AsyncMock(return_value=uuid4())), \
         patch("scripts.voice_test_server.build_accept_payload", new=build_payload_mock), \
         patch("scripts.voice_test_server.create_ephemeral_session",
               new=AsyncMock(return_value={"value": "ek_x", "expires_at": 1})):
        resp = client.post("/session", json={})

    assert resp.status_code == 200
    resolve_mock.assert_awaited_once()
    _, kwargs = resolve_mock.call_args
    from uuid import UUID
    assert kwargs["shop_id"] == UUID(demo_shop_id)


def test_session_returns_400_when_shop_has_no_config(client):
    with patch("scripts.voice_test_server.get_config", new=AsyncMock(return_value=None)), \
         patch("scripts.voice_test_server.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "hi"})):
        resp = client.post("/session", json={"shop_id": str(uuid4())})

    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPATH=. && pytest tests/voice_gateway/test_voice_test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.voice_test_server'`

- [ ] **Step 3: Add `scripts/__init__.py` if it doesn't already exist**

```bash
test -f scripts/__init__.py || touch scripts/__init__.py
```

- [ ] **Step 4: Write the implementation**

Create `scripts/voice_test_server.py`:

```python
"""Local voice + MCP test harness — mint an OpenAI ephemeral Realtime session
for a browser WebRTC test call against the QA Fly app's MCP server.

The server itself never leaves your machine and executes no tools directly —
OpenAI calls the QA Fly app's MCP server server-to-server for every tool
invocation, exactly like a real Twilio call would. Write tools (create_booking,
etc.) execute for real, but only against the QA Neon branch, not production.

Usage:
    export PYTHONPATH=. ; set -a; source .env; set +a
    uvicorn scripts.voice_test_server:app --port 8765
    # then open http://localhost:8765/ in a browser
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from booking_engine.clients.openai_realtime import create_ephemeral_session
from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.realtime_session import build_accept_payload

logger = logging.getLogger(__name__)

_QA_MCP_URL = "https://kairo-booking-engine-qa.fly.dev/mcp"
_STATIC_DIR = Path(__file__).parent / "voice_test_static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_connection(Settings())
    yield
    await close_connection()


app = FastAPI(lifespan=lifespan)


class SessionRequest(BaseModel):
    shop_id: UUID | None = None
    caller_phone: str = ""


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/session")
async def create_session(body: SessionRequest) -> JSONResponse:
    settings = Settings()
    shop_id = body.shop_id or UUID(os.environ["DEMO_SHOP_ID"])

    config = await get_config(shop_id)
    policy = await get_policy()
    if not config or not policy:
        return JSONResponse({"error": "shop has no voice config/policy"}, status_code=400)

    resolution = await resolve_caller(shop_id=shop_id, caller_phone=body.caller_phone)
    call_id = await insert_call(
        shop_id=shop_id, caller_phone=body.caller_phone,
        matched_customer_id=(resolution.unique_match.customer_id
                              if resolution.unique_match else None),
    )
    mcp_token = mint_call_token(shop_id=shop_id, call_id=call_id,
                                secret=settings.openai_tool_secret)
    payload = await build_accept_payload(
        config=config, policy=policy, resolution=resolution,
        model=settings.openai_realtime_model,
        mcp_server_url=_QA_MCP_URL, mcp_token=mcp_token,
    )
    session = await create_ephemeral_session(
        session_config=payload, api_key=settings.openai_api_key,
    )
    return JSONResponse({"client_secret": session["value"], "call_id": str(call_id)})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `export PYTHONPATH=. && pytest tests/voice_gateway/test_voice_test_server.py -v`
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add scripts/voice_test_server.py scripts/__init__.py tests/voice_gateway/test_voice_test_server.py
git commit -m "feat(voice-test): add local session-minting server for the browser voice harness"
```

---

### Task 3: `scripts/voice_test_static/index.html` — browser WebRTC page

**Files:**
- Create: `scripts/voice_test_static/index.html`

No automated test for this file — it's a manual test UI, exercised by a human in Task 4. Verification here is a structural HTML sanity check only.

- [ ] **Step 1: Write the page**

Create `scripts/voice_test_static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kairo Voice Test Harness</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }
  label { display: block; margin-top: 12px; font-weight: 600; }
  input { width: 100%; padding: 8px; font-size: 14px; box-sizing: border-box; }
  button { margin-top: 16px; padding: 10px 20px; font-size: 15px; cursor: pointer; }
  #log { margin-top: 24px; padding: 12px; background: #f5f5f5; border-radius: 6px;
         min-height: 200px; white-space: pre-wrap; font-family: monospace; font-size: 13px; }
  #status { font-weight: 600; }
</style>
</head>
<body>
  <h1>Kairo Voice Test Harness</h1>
  <p>Speaks to the real voice agent (same prompt/tools/DB as a real call) via OpenAI Realtime WebRTC. Tool calls hit the QA Fly app's MCP server — writes land on the QA Neon branch, not production.</p>

  <label for="shopId">Shop ID</label>
  <input id="shopId" value="">

  <label for="callerPhone">Caller phone (simulated)</label>
  <input id="callerPhone" value="+390000000000">

  <button id="startBtn">Start Call</button>
  <button id="hangupBtn" disabled>Hang Up</button>

  <p id="status">Idle</p>
  <div id="log"></div>

<script>
let pc = null;

function log(msg) {
  const el = document.getElementById('log');
  el.textContent += msg + "\n";
  el.scrollTop = el.scrollHeight;
}

function setStatus(msg) {
  document.getElementById('status').textContent = msg;
}

document.getElementById('startBtn').onclick = async () => {
  setStatus('Requesting session...');
  const shopId = document.getElementById('shopId').value.trim();
  const callerPhone = document.getElementById('callerPhone').value.trim();

  const sessionResp = await fetch('/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(shopId ? { shop_id: shopId, caller_phone: callerPhone }
                                 : { caller_phone: callerPhone }),
  });
  if (!sessionResp.ok) {
    const err = await sessionResp.json();
    setStatus('Failed to create session');
    log('ERROR: ' + JSON.stringify(err));
    return;
  }
  const { client_secret, call_id } = await sessionResp.json();
  log('Session created, call_id=' + call_id);
  setStatus('Connecting...');

  pc = new RTCPeerConnection();
  const audioEl = document.createElement('audio');
  audioEl.autoplay = true;
  pc.ontrack = (e) => { audioEl.srcObject = e.streams[0]; };
  document.body.appendChild(audioEl);

  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  pc.addTrack(mic.getTracks()[0]);

  const dc = pc.createDataChannel('oai-events');
  dc.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.type && ev.type.endsWith('transcript.delta') && ev.delta) {
        log(ev.delta);
      } else if (ev.type === 'response.function_call_arguments.done') {
        log('[tool call: ' + ev.name + '(' + ev.arguments + ')]');
      } else if (ev.type === 'error') {
        log('ERROR: ' + JSON.stringify(ev.error || ev));
      }
    } catch (err) {
      // non-JSON or unrecognized event, ignore
    }
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  const sdpResp = await fetch('https://api.openai.com/v1/realtime/calls', {
    method: 'POST',
    body: offer.sdp,
    headers: {
      'Authorization': 'Bearer ' + client_secret,
      'Content-Type': 'application/sdp',
    },
  });
  const answerSdp = await sdpResp.text();
  await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });

  setStatus('Connected — talk into your mic');
  document.getElementById('startBtn').disabled = true;
  document.getElementById('hangupBtn').disabled = false;
};

document.getElementById('hangupBtn').onclick = () => {
  if (pc) { pc.close(); pc = null; }
  setStatus('Idle');
  document.getElementById('startBtn').disabled = false;
  document.getElementById('hangupBtn').disabled = true;
};
</script>
</body>
</html>
```

- [ ] **Step 2: Structural sanity check**

```bash
python3 -c "
import re
content = open('scripts/voice_test_static/index.html').read()
assert '<script>' in content and '</script>' in content
assert 'getUserMedia' in content
assert 'RTCPeerConnection' in content
assert '/session' in content
assert 'api.openai.com/v1/realtime/calls' in content
print('structural check OK')
"
```
Expected: `structural check OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/voice_test_static/index.html
git commit -m "feat(voice-test): add browser WebRTC page for the local voice test harness"
```

---

### Task 4: End-to-end manual verification (human-in-the-loop — real mic, real API cost)

**Files:** none (verification only)

**Known risk to watch for:** `build_accept_payload()` produces a flat `"voice": "..."` field, matching the older SIP `/accept` endpoint's shape. OpenAI's current `/v1/realtime/client_secrets` docs example nests voice under `"audio": {"output": {"voice": "..."}}` instead. Since Task 2's tests are fully mocked, this discrepancy can only surface here, at the first real network call. If Step 1 or Step 3 below fails with a 400 mentioning `voice` or an unrecognized field, patch `scripts/voice_test_server.py`'s `create_session` to convert before calling `create_ephemeral_session`:

```python
    voice = payload.pop("voice", None)
    if voice:
        payload["audio"] = {"output": {"voice": voice}}
```

Insert this right before the `create_ephemeral_session(...)` call, re-run Step 1/3, and commit the fix with a note referencing the actual error message you saw.

- [ ] **Step 1: Start the server**

```bash
export PYTHONPATH=. && set -a && source .env && set +a
uvicorn scripts.voice_test_server:app --port 8765
```
Expected: `Uvicorn running on http://0.0.0.0:8765`, no startup errors (confirms DB connection succeeds).

- [ ] **Step 2: Open the page**

Open `http://localhost:8765/` in a browser. Confirm the page loads with the Shop ID / Caller phone fields and a "Start Call" button.

- [ ] **Step 3: Place a test call**

Leave Shop ID blank (defaults to `DEMO_SHOP_ID` server-side), click "Start Call", grant microphone permission when prompted. Speak a simple booking request (e.g. "Ciao, vorrei prenotare un taglio").
Expected: you hear the agent's spoken response through your speakers; the transcript log shows matching text; status changes to "Connected — talk into your mic".

- [ ] **Step 4: Confirm a real tool call reaches the QA MCP server**

Continue the conversation until the agent needs to look up services/availability (a read tool). Confirm the transcript log shows a `[tool call: ...]` line, and that the conversation continues naturally afterward (proves OpenAI successfully reached `https://kairo-booking-engine-qa.fly.dev/mcp` and got a real result back).

- [ ] **Step 5: Confirm QA data was actually touched, not production**

If the call reached a write tool (e.g. `create_booking`), check the QA Neon branch (not production) for the new row:
```bash
# Using the Neon MCP tool or neonctl, query the QA branch (br-damp-recipe-agnys6xk)
# specifically — NOT the production DATABASE_URL — for the new appointment/customer row.
```
Expected: the new row exists on the QA branch; production is untouched.

- [ ] **Step 6: Hang up**

Click "Hang Up". Confirm status returns to "Idle" and the audio element stops.

---

## Self-Review Notes

- **Spec coverage:** Architecture (Task 2), Components — server (Task 2) and static page (Task 3), Data Flow (Task 4 verification steps 3-5), Safety/Scope (QA-only MCP URL asserted in Task 2's test), Testing section (Task 2's unit tests match the spec's stated testable surface: session response shape + mcp_server_url targeting) — all covered.
- **Placeholder scan:** no TBD/TODO; every step has literal code or commands.
- **Type/name consistency:** `create_ephemeral_session(session_config=..., api_key=...)` signature matches between Task 1's definition and Task 2's usage; `_QA_MCP_URL` string matches exactly between Task 2's code and its test's assertion; `SessionRequest` field names (`shop_id`, `caller_phone`) match between the FastAPI model and both the test's JSON bodies and the HTML page's `fetch` body.
