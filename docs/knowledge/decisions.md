# Decisions

A short index into `CLAUDE.md`'s full history log — enough to find the relevant entry without reading the whole file. `CLAUDE.md` is the source of truth; this page is a lookup aid, not a duplicate, and is kept in sync by hand when `CLAUDE.md` gains a new entry worth indexing.

> **Maintenance rule:** a new `CLAUDE.md` entry that a future reader would plausibly search for gets a one-line pointer added here in the same change. See [README](README.md#maintenance-rule).

---

| Date | Decision | CLAUDE.md section |
|---|---|---|
| 2026-07-24 | CI: migration ownership for the shared Neon DB moved to the `webapp` repo | §"CI: migration ownership moved to the webapp repo" |
| 2026-07-24 | Repo cleanup: deleted dead docs/scripts, rewrote stale docs, closed a dependency drift | §"Repo cleanup: deleted dead docs/scripts..." |
| 2026-07-24 | In-process MCP tool dispatch (fixed self-proxying "dead air" over real HTTPS) + a tool-call timeout | §"Root-caused session 'dead air'..." |
| 2026-07-21 | `voice_tones`/`tone_id` migration gap found and closed during a WIP review | §"Reviewed voice-config WIP commit..." |
| 2026-07-21 | Server-side call-supervisor WebSocket built to fix mute-after-tool-result | §"SIP call supervisor: production fix..." |
| 2026-07-21 | Cost-gated pricing (`include_price`) + multi-service/multi-staff chain bookings | §"Cost-gated pricing + multi-service/multi-staff bookings" |
| 2026-07-21 | Diagnosed: hosted MCP does not auto-speak tool results (prod blocker at the time) | §"Realtime + hosted MCP does NOT auto-speak tool results" |
| 2026-07-21 | `/mcp` needs a trailing slash — OpenAI doesn't follow the 307 | §"MCP server_url must carry a trailing slash" |
| 2026-07-18 | CI/CD moved to ephemeral Neon branches; AWS Lambda deploy path removed | §"CI/CD: ephemeral Neon branches, seed-data bug fix, Lambda removal" |
| 2026-07-17 | Added live tool-dispatch + security test coverage; found/fixed an FK-crash bug | §"Live tool-dispatch + security test coverage" |
| 2026-07-16 | Telephony provider: Telnyx → Twilio (Estonia mobile numbers) | §"Telephony provider: Telnyx → Twilio" |
