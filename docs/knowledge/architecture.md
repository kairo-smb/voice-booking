# Architecture

> **Maintenance rule:** a change to the service topology, the call flow, or the tool-dispatch mechanism updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Topology

```
Caller (phone) → Twilio (TwiML) → OpenAI Realtime API (native SIP, STT/LLM/TTS)
                                          ↕ MCP tool calls (in-process ASGI, /mcp)
                                    booking_engine (Fly.io, single service)
                                          ↕ SQL (asyncpg)
                                    Neon PostgreSQL (business_app_core + voice_agent)
```

One deployed service — `booking_engine`, a FastAPI app (`booking_engine/api/app.py`). There is no separate "voice gateway" process; an earlier two-service split was unified here (see `CLAUDE.md` §2026-07-21 "Realtime + hosted MCP..." and the architecture-divergence note it superseded).

## Call flow

1. A call reaches a Twilio number. Twilio POSTs to `POST /api/v1/voice/twiml/incoming` (`voice_twiml.py`), signature-checked against `TWILIO_AUTH_TOKEN`. The handler looks up the shop by the dialed number and returns TwiML that `<Dial><Sip>`s straight into OpenAI's SIP gateway, passing the shop id as a custom SIP header (`X-Shop-Id`, via Twilio's `<Dial><Sip>` query-string-after-host convention — see `services/realtime_session.py::build_sip_uri`).
2. OpenAI fires `realtime.call.incoming` to `POST /voice/openai/incoming` (`voice_openai.py`, top-level path, not under `/api/v1`). The handler reads `X-Shop-Id` back out of the SIP headers (or, QA-only, falls back to `SIP_TEST_FALLBACK_SHOP_ID` for a raw softphone test call with no Twilio in the path), resolves the caller by phone (`services/identity_resolver.py`), assembles the session prompt (`services/prompt_assembler.py` — see [Voice Agent Logic](voice-agent-logic.md)), and calls `accept_sip_call()` (`clients/openai_realtime.py`) with that prompt + the 12 tool schemas.
3. During the call, OpenAI calls tools over MCP against `/mcp` (mounted directly on this app in `app.py`, via `booking_engine/mcp_server.py`). Tool dispatch is **in-process** — `execute_tool()` uses an `ASGITransport(app=app)` call into the exact same running process rather than a real HTTP hop, wrapped in a 10s `asyncio.wait_for` (`TOOL_CALL_TIMEOUT_SECONDS`, `services/mcp_tools.py`) that returns a clean `{"ok": false, "error": "tool_timeout"}` on a stuck downstream call rather than hanging. This was a deliberate fix for real "dead air" latency caused by an earlier version that made a genuine outbound HTTPS request to the app's own public URL on every tool call — full incident in `CLAUDE.md` §2026-07-24.
4. If `ENABLE_CALL_SUPERVISOR` is set, a per-call background task (`services/call_supervisor.py`) opens its own control WebSocket to the accepted call and sends `response.create` on connect (greeting) and after each tool result (`response.output_item.done` for an `mcp_call`) — working around OpenAI's hosted MCP not auto-continuing after a tool result on its own. Off by default; see `CLAUDE.md` §2026-07-21 for why it exists and its current live-test status.
5. On hangup, the call is finalized via `voice_events.py`'s `session.*` webhooks (started/turn/ended), persisting to `voice_agent.calls`/`call_transcripts`/`call_events`.

## Auth boundaries

Four distinct auth schemes across the surface — see [API overview](api/README.md) for the full breakdown:
- `CONTROL_PLANE_SECRET` — the separate `webapp` Control Plane repo, reading/writing voice config, calls, analytics, telephony provisioning.
- `OPENAI_TOOL_SECRET` — OpenAI's Realtime tool/event calls (`/voice/tools/*`, `/voice/events/*`), and the MCP mount.
- Twilio request-signature verification (`TWILIO_AUTH_TOKEN`) — the TwiML webhook only.
- The OpenAI `realtime.call.incoming` webhook (`/voice/openai/incoming`) currently verifies a signature **only if `OPENAI_WEBHOOK_SECRET` is set** — unset, it accepts unsigned requests (a known, flagged gap; see the `ponytail:` comment at the top of `voice_openai.py`).
- The plain REST API (`shops`, `customers`, `services`, `availability`, `appointments`) has **no auth dependency at all** today.

## Self-service number provisioning (Path 2 onboarding)

A second, independent flow alongside the SIP call path above — no caller involved, just the webapp and Twilio's Regulatory Compliance API:

1. Webapp's "Richiedi numero" panel (Inbox → Configurazione → Canali) POSTs `business_name`/`contact_email`/a commercial-register document to `POST /api/v1/voice/numbers/request` (`voice_telephony.py`, control-plane bearer). `services/number_provisioning.py::submit_request` builds **that salon's own** Twilio regulatory bundle — regulation lookup → End-User → document upload → Bundle → 2× ItemAssignment → synchronous Evaluate → submit-if-compliant — persisting each Twilio SID to `voice_agent.number_requests` as soon as it exists, not batched at the end.
2. `POST /api/v1/messaging/tick` (`messaging_tick.py`), hit hourly by `.github/workflows/messaging-cron.yml`, polls every `pending_review` request's bundle status, calls `services/number_provisioning.py::provision_approved` to purchase the number once Twilio approves, and refreshes the green/red health semaphore (`services/number_health.py`) for every already-provisioned number.
3. `GET /api/v1/voice/numbers/request/{shop_id}` is the webapp's poll target — returns the request row and the telephony row (if any) so the UI can pick which state to render.

**This coexists with, and does not replace, the older manual `/voice/numbers/search` + `/voice/numbers/provision` pair** — those still purchase against the one shared Kairo-entity bundle (`TWILIO_BUNDLE_SID`) from the 2026-07-16 decision, used for Path 1 (forwarding) and ops-triggered onboarding. Only the new `/request` path builds a bundle per salon. Full rationale for why a shared bundle is no longer viable for self-service: `CLAUDE.md` §2026-08-14. Design detail (regulatory model, the orphaned-number bug and its fix, the health-semaphore contract): see the git history of the now-deleted `docs/number-provisioning-design.md`, or `CLAUDE.md` §2026-08-14.

## Alternate entrypoint (testing only)

`clients/openai_realtime.py::create_ephemeral_session` mints a browser/WebRTC session for local testing (`scripts/voice_test_server.py`, `scripts/run_webrtc_harness.sh`) — a different transport than production SIP calls. See [Operations](operations.md) for the live-SIP softphone test path, which exercises the real call flow above end-to-end without needing a funded Twilio number.
