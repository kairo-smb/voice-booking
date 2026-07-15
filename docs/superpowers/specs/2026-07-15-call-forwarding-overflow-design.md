# Call Forwarding & Overflow Handover — Design

**Date:** 2026-07-15
**Branch:** `feat/voice-forwarding-overflow`
**Status:** approved, ready for implementation plan

## Context

Salons route their phone traffic to the Kairo voice agent in one of two product modes:

1. **Full** — the salon forwards *every* call to us; the AI is the receptionist.
2. **Overflow** — the salon's staff answers first; only calls nobody picks up
   (no-answer after ~10s, or busy) reach us, and the AI catches them.

The routing brain (`voice_texml.py`), per-shop Telnyx DIDs, provisioning, and
the `answer_mode`/`overflow_ring_count` config columns already exist. But the two
modes are **inert**: nothing reads `answer_mode`, onboarding gives salons no
concrete "type this code" instructions, the greeting is a single static string,
and the silence heartbeat false-alarms on overflow shops. This spec wires the
existing pieces together with minimal new code.

## Core premise (why this is small)

For single-line salons (mobile SIM or copper landline — no PBX), overflow
filtering **must** happen at the carrier. We cannot "ring the salon, then fall
back to the AI" because the salon's only line is the one forwarding to us —
dialing it back is an infinite loop (the exact hazard guarded at
`voice_texml.py:89`). So the carrier does the 10s no-answer / busy filtering via
conditional call forwarding, and **every call that reaches our DID is already one
the AI should take.** Routing does not need to branch on mode.

Overflow is **best-effort**: use the frictionless no-answer timer where the line
supports it (mobile GSM codes), otherwise just catch missed calls. Both outcomes
are acceptable.

## Decisions (locked)

- **Number model:** keep the per-shop Telnyx DID (~€1/mo). Robust shop lookup by
  dialed number, already built. No shared trunk, no porting.
- **Line type:** derived from the Italian number prefix of `salon_existing_number`
  (`3…` = mobile, `0…` = landline). No new column.
- **Landline overflow:** salon chooses — we return both Full and best-effort
  Overflow instruction blocks; they pick which codes to enter.
- **Greeting:** shop-personalized per mode (not a hardcoded string). Backend
  stores it; the editor UI is webapp work on a separate branch.

## Components

### 1. Mode wiring — reuse `answer_mode`

`shop_config.answer_mode ('overflow' | 'always_on')` is the single source of
truth. `always_on` = Full, `overflow` = catch-misses. Already exists and is
PATCHable via `voice_config.py`. No routing change in `voice_texml.py`.

### 2. Setup-instruction generator (the low-friction lever)

New pure function `services/setup_instructions.py::build_instructions(...)` and
route `GET /voice/setup-instructions/{shop_id}`.

Inputs: `kairo_number` (DID), `salon_existing_number`, `answer_mode`,
`overflow_ring_count`. Output: a dict with a `full` block and an `overflow`
block, each carrying human text + the literal codes.

- **Mobile + Full:** activate `**21*<DID>#`, deactivate `##21#`.
- **Mobile + Overflow:** no-answer `**61*<DID>*11*<sec>#`
  (`sec = clamp(overflow_ring_count × 5, 5, 30)`), busy `**67*<DID>#`,
  unreachable/off `**62*<DID>#`.
- **Landline (either mode):** best-effort — present the `*61*<DID>#` /
  `*21*<DID>#` style codes (most IT copper supports them) plus a fallback line:
  "or set call-forward-on-no-answer to `<DID>` in your carrier's app/portal."

No per-carrier scripting — generic best-effort text only.

### 3. Mode-aware, shop-personalized greeting

- **Schema:** add `shop_config.greeting_overflow text NOT NULL DEFAULT ''`
  (migration, `ADD COLUMN IF NOT EXISTS`).
- **API:** add `greeting_overflow` to the `voice_config.py` PATCH allowlist and
  request model (same treatment as `greeting_after_disclosure`).
- **Prompt:** `prompt_assembler.py` selects the greeting by mode —
  `answer_mode='overflow'` → `greeting_overflow` (fallback to a sensible Italian
  default that interpolates `display_name`, e.g. acknowledging the salon can't
  take the call right now); otherwise `greeting_after_disclosure`.
- **Webapp (separate repo/branch):** an editor field for `greeting_overflow`.
  Out of scope here. Contract: the field is read/written through the existing
  `GET/PATCH /voice/config/{shop_id}` endpoints.

### 4. Heartbeat false-alarm fix

`forwarding_heartbeat.py::find_silent_forwarded_shops` currently alerts *any*
`setup_path='forward'` shop with no inbound call in `threshold_days`. Overflow
shops are legitimately silent for days. Fix: join `shop_config` and restrict the
alert to `answer_mode='always_on'` shops. Overflow shops are excluded.

## Explicitly out of scope (YAGNI)

- Shared inbound trunk / SIP `Diversion`-header disambiguation.
- Number porting.
- Per-carrier landline portal scripting.
- **Active forwarding verification** (a test call confirming the salon actually
  set forwarding). This is a real gap for overflow shops — silence no longer
  proves breakage — but it is a separate feature. Note it; do not build it.
- All webapp UI (greeting editor, mode picker) — separate repo, separate branch.

## Tests

- `build_instructions`: correct GSM string per mode × line-type; seconds clamp
  (ring_count 1→5s, 10→30s); mobile vs landline branch on number prefix.
- `prompt_assembler`: overflow mode picks `greeting_overflow`, falls back to the
  default when empty; always_on picks `greeting_after_disclosure`.
- `find_silent_forwarded_shops`: an `overflow` shop that is silent is NOT
  returned; an `always_on` silent shop IS.

## Footprint

- New: `services/setup_instructions.py`, one route file, one migration column.
- Edits: `prompt_assembler.py` (greeting branch), `forwarding_heartbeat.py`
  (WHERE clause), `voice_config.py` (allowlist + model field).
- No change to `voice_texml.py` routing.
