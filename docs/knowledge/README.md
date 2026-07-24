# Kairo Voice Booking — Knowledge Base

Human-oriented documentation for the voice-booking `booking_engine` service: what exists, how it behaves, and the nuances that matter for continuing the work. This is **not** an implementation reference — the code is the only ground truth for exactly how something works. This knowledge base exists to capture what the code can't say for itself: purpose, current behavior, external dependencies, and hard-won gotchas.

## Contents

- **[Architecture](architecture.md)** — single-service topology, the call flow from Twilio through OpenAI's native SIP into this service, in-process MCP tool dispatch
- **[Database](database.md)** — the `business_app_core` / `voice_agent` schema ownership boundary, and every `voice_agent` table
- **[Voice Agent Logic](voice-agent-logic.md)** — the domain rules: safety prompt, booking authorization, lead-time/gap constraints, prompt assembly, the tone system
- **[Providers](providers.md)** — every external service (Twilio, OpenAI Realtime, Neon, push notifications): purpose, auth, hard rules
- **[Operations](operations.md)** — deploy, migrations, CI, env vars, secrets, live-call testing
- **[Decisions](decisions.md)** — a short index into `CLAUDE.md`'s history log, organized for lookup rather than chronology
- **[API](api/README.md)** — REST/webhook/tool contract docs for every route in `booking_engine/api/routes/`, grouped by who calls them

## Maintenance rule

**Any change that adds, removes, or changes a REST/voice-tool endpoint, a database table (in either `business_app_core` or `voice_agent`), a provider integration, or a safety/authz/booking-constraint rule updates the matching file here in the same change — not as a follow-up.** This is enforced by whoever (human or agent) makes the change, not by tooling. `CLAUDE.md` points here.

If this rule stops being followed and the docs rot again, the next escalation is an automated staleness check (e.g. CI failing when a route exists with no matching `api/*.md` entry) — add that when manual discipline demonstrably fails, not before.

## Viewing as a site

This folder is a [Docsify](https://docsify.js.org/) site — plain markdown, rendered client-side, no build step. To view it locally:

```bash
npx --yes serve docs/knowledge
```

Then open the printed URL. (Opening `index.html` directly via `file://` won't work — Docsify fetches the `.md` files over HTTP.)
