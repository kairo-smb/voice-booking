# Docsify knowledge base for voice-booking — design

## Purpose

Replicate the `webapp` repo's `docs/knowledge/` Docsify pattern in this repo: a
human-oriented documentation site covering both technical architecture and the
domain/logical rules the voice agent enforces, kept current by a stated
maintenance convention rather than tooling.

`CLAUDE.md` stays exactly what it is — an append-only chronological log of
decisions and incidents, the historical record. The knowledge base is a
different axis: the current-state reference (“what exists and how it behaves
right now”), not a log of how it got there. `decisions.md` is the one page
that deliberately bridges the two, as a short topic index into `CLAUDE.md`.

## Precedent

`~/Documents/kairo/webapp/docs/knowledge/` is the reference implementation:
plain markdown rendered client-side by Docsify (CDN script, no build step), a
`README.md` stating a maintenance rule, per-topic pages, and an `api/`
subfolder grouping REST contract docs by domain. `webapp/CLAUDE.md` (lines
~17–24) carries a short pointer section listing the pages and the rule; this
is the mechanism being mirrored, not any specific page content (the two
repos' domains differ).

## Structure

```
docs/knowledge/
  index.html              # Docsify loader — same config as webapp's, title changed
  README.md                # homepage: contents list + maintenance rule
  _sidebar.md
  architecture.md
  database.md
  voice-agent-logic.md
  providers.md
  operations.md
  decisions.md
  api/
    README.md
    business.md
    telephony-webhooks.md
    voice-tools.md
    voice-control-plane.md
```

No build step, no new dependency committed to `requirements.txt`. Viewed
locally via `npx --yes serve docs/knowledge` (same as webapp — Docsify fetches
`.md` files over HTTP, so `file://` doesn't work).

## Page contents (source of truth for each)

**`index.html`** — copy of webapp's docsify config (`loadSidebar: true`,
search plugin, `vue` theme), title `Kairo Voice Booking — Knowledge Base`.

**`README.md`** — mirrors webapp's homepage shape: one-paragraph purpose
statement making explicit that this is *not* an implementation reference (the
code is), a bulleted contents list linking each page with a one-line
description, and the maintenance-rule blockquote:

> Any change that adds, removes, or changes a REST/voice-tool endpoint, a
> database table (in either `business_app_core` or `voice_agent`), a
> provider integration, or a safety/authz/booking-constraint rule updates the
> matching file here in the same change — not as a follow-up. Enforced by
> whoever (human or agent) makes the change, not by tooling. If this rule
> stops being followed and the docs rot again, the next escalation is an
> automated staleness check — add that when manual discipline demonstrably
> fails, not before.

**`architecture.md`** — single-service topology (Twilio → OpenAI Realtime
native SIP → `booking_engine` on Fly.io → Neon), sourced from
`booking_engine/api/app.py` (route mounts), `voice_twiml.py`/`voice_openai.py`
(call acceptance flow), `mcp_server.py`/`services/mcp_tools.py` (in-process
MCP dispatch — the self-proxying/timeout history from `CLAUDE.md`'s
2026-07-24 "dead air" entry belongs here as the *current* behavior, without
repeating the incident narrative), and `services/call_supervisor.py` (the
flag-gated control-WS greet/nudge mechanism).

**`database.md`** — the ownership boundary between `business_app_core`
(Control Plane owns; this repo reads/writes narrowly and never alters DDL)
and `voice_agent` (this repo owns; DDL in `booking_engine/db/sql/`, applied in
order via `scripts/migrate.sh`, `01`/`02` explicitly excluded as a
local-only bootstrap pair). Documents `voice_agent` tables by reading the
actual `03`–`10` SQL files, not by copying old doc content — this is the
exact anti-pattern (`INTEGRATION_GUIDE.md`'s hand-copied, silently-stale
schema) the 2026-07-24 cleanup entry in `CLAUDE.md` just fixed. For
`business_app_core`, states plainly that this repo doesn't own that schema
and points at `booking_engine/db/queries.py` and `tests/live_db/*` as the
canonical mapping, same as the just-rewritten `INTEGRATION_GUIDE.md` does —
folds that content in rather than re-inventing it.

**`voice-agent-logic.md`** — the domain/"logical aspects" page. Sourced from:
`services/safety_layer.py` (non-negotiable rules, the 12 tool schemas),
`services/booking_authz.py` (phone-match authorization, the documented gap
around `update_customer_from_call` having no shop check), `services/
booking_constraints.py` (lead-time, past-slot, `MAX_GAP_MINUTES` chain
rules), `services/prompt_assembler.py` (3-layer prompt composition), the tone
system (`voice_tone_queries.py`, 8 seeded presets), `services/
identity_resolver.py` (phone-based caller matching), and the multi-service/
multi-staff chain booking behavior. This is business logic that "the code
can't say for itself" in the webapp README's phrasing — why a rule exists,
not just that it does.

**`providers.md`** — Twilio (numbers, TwiML webhook, signature verification,
Estonia-mobile-number rationale summarized with a link to the full `CLAUDE.md`
entry), OpenAI Realtime (native SIP acceptance, hosted MCP tool-result
auto-continue gap and the call-supervisor workaround, summarized not
re-narrated), Neon (pooler vs direct connection, ephemeral-branch CI usage).
Same per-provider shape as webapp's page: Purpose / Key files / Env vars /
hard rules or gotchas.

**`operations.md`** — absorbs `docs/DEPLOY_VOICE_AGENT.md` in full: Fly
deploy steps (prod + QA), `CONTROL_PLANE_SECRET`/`OPENAI_TOOL_SECRET`/Twilio
env vars, the GitHub Actions ephemeral-Neon-branch CI/CD flow (`ci.yml`,
`deploy-qa.yml`, `deploy-fly-prod.yml`), post-deploy smoke test, and the
live-SIP-call testing walkthrough (softphone dial, `ENABLE_CALL_SUPERVISOR`
debug flags). Rewritten into webapp's page style (maintenance-rule blockquote
at top), not a verbatim copy.

**`decisions.md`** — a short (~1 line per entry) chronological or topical
index into `CLAUDE.md` sections, e.g. "Telnyx → Twilio (Estonia mobile
numbers) — see CLAUDE.md §2026-07-16" — enough for someone to find the
relevant history entry without reading the whole log, kept in sync manually
alongside `CLAUDE.md` edits (per the user's explicit choice over linking out
with no index).

**`api/README.md`** — response envelope conventions (`{"data": ...}` /
`{"error": "...", "message": "..."}`, 409 slot-conflict shape), and the
distinct auth schemes in use across the API surface: `CONTROL_PLANE_SECRET`
Bearer for control-plane endpoints, `OPENAI_TOOL_SECRET` Bearer for voice
tools, Twilio request-signature verification for the TwiML webhook, and —
worth calling out explicitly, it's a real known gap, not a doc error — the
OpenAI `realtime.call.incoming` webhook (`voice_openai.py`) only verifies a
signature when `OPENAI_WEBHOOK_SECRET` is set (see the `ponytail:` comment at
the top of that file); unset, the endpoint accepts unsigned requests.

**`api/business.md`** — the plain CRUD REST API (`shops`, `customers`,
`services`, `availability`, `appointments` routers) — folds in
`INTEGRATION_GUIDE.md`'s old endpoint table, re-verified against
`booking_engine/api/routes/{shops,customers,services,availability,
appointments}.py` rather than trusted as-is.

**`api/telephony-webhooks.md`** — `voice_twiml.py` (Twilio TwiML webhook,
signature verification) and `voice_openai.py` (`realtime.call.incoming`
acceptance, `SIP_TEST_FALLBACK_SHOP_ID` QA-only routing).

**`api/voice-tools.md`** — the 12 tools across `voice_tools_catalog.py`,
`voice_tools_booking.py`, `voice_tools_lifecycle.py`, `voice_tools_identity.py`,
plus `voice_events.py` and `voice_memos.py` — mounted via `/mcp`, dispatched
in-process (link to `architecture.md` for the dispatch mechanism rather than
repeating it).

**`api/voice-control-plane.md`** — `voice_config.py`, `voice_balance.py`,
`voice_heartbeat.py`, `voice_telephony.py`, `voice.py` (analytics/calls) —
the endpoints the webapp Control Plane calls.

## Consolidation

`docs/DEPLOY_VOICE_AGENT.md` and `docs/INTEGRATION_GUIDE.md` are deleted once
their content is folded into the pages above — no duplicate source of truth
left behind. `docs/DEPLOY_VOICE_AGENT.md`'s own env-var/endpoint tables were
already accurate as of the last cleanup pass; this is a move-and-reformat
into the new page shape, re-verifying anything that touches a route path or
schema detail rather than trusting the copy.

## CLAUDE.md change

A short new block (~8 lines, mirroring webapp's `CLAUDE.md` lines ~17–24) is
inserted after the existing header note, before the first dated entry. It
names each `docs/knowledge/*.md` page in one line and states the maintenance
rule in one sentence, pointing to the full rule in
`docs/knowledge/README.md`. It does not duplicate page content and does not
change `CLAUDE.md`'s existing role as the append-only history log — that
framing in the current header stays untouched.

## Out of scope (explicit non-goals)

- No CI job that generates or validates docs freshness. Webapp's own
  precedent explicitly defers this until manual discipline fails; no signal
  here suggests it's failing yet.
- No hosted/public deployment of the Docsify site (e.g. GitHub Pages) —
  local viewing via `npx serve`, matching webapp.
- No change to FastAPI's own auto-generated `/docs` (Swagger) — that remains
  the live request/response schema reference; the new `api/*.md` pages are
  the narrative/auth/gotcha layer on top of it, not a replacement.
- Not rewriting `CLAUDE.md`'s existing entries or its role as the history
  log.
