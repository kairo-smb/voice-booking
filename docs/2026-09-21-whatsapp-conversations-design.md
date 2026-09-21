# WhatsApp Conversations — design

**Working document.** Delete when shipped; the durable record is the CLAUDE.md
entry written at merge. Convention follows
`docs/2026-09-02-whatsapp-template-engagement-design.md`, not
`docs/superpowers/specs/` (deleted wholesale 2026-07-24, owner decision).

Branch: `feat/whatsapp-conversations`.

---

## 1. What this is

WhatsApp stops being a one-way campaign channel. Customers write, the salon
answers, and — in the second increment — an agent answers on the salon's behalf
and books the appointment itself.

Split into two increments, in this order, because the second needs every piece
of the first:

- **A — Conversations.** Threads, the 24h window, echo from the owner's phone,
  voice-note transcription, free-form reply, intent classification. The owner
  answers by hand.
- **B — Booking agent.** A turn loop over the tools that already exist, gated
  by an intent whitelist, suspended the moment a human writes.

A ships on its own. B is specified here (§9) rather than in a separate file so
the decisions that bind both — the window, the echo, the intent whitelist —
live in one place.

---

## 2. The facts that constrain everything

**The 24h customer service window.** Meta permits free-form (non-template)
messages only within 24 hours of the customer's **last inbound message**.

- The window **resets on every customer message**. A customer who writes again
  after two days of silence reopens it themselves; we can always answer a
  message we have just received.
- Outbound does **not** extend it. Our reply buys no time.
- So the only thing the window actually forbids is **us speaking first** after
  24h of customer silence. That path needs an approved template.

**Service conversations are free.** Since 2024-11-01 Meta does not charge for
conversations opened by the customer. A reply inside the window costs nothing,
and the Tech Provider model means there is no credit line to share anyway.
`send_credits` stays out of this path, consistent with every other WhatsApp
route. Only AI work is metered.

**Every sender is `source='coexistence'`.** The number is still live in the
salon's WhatsApp Business App and the owner answers from their phone. Any
design that assumes we are the only writer is wrong on day one.

---

## 3. Existing machinery this reuses

Nothing about booking is rebuilt. `voice_agent.calls` is already a session
table, not a telephony table:

```
shop_id · caller_number · customer_id · customer_match
outcome ('booked'|'rescheduled'|'cancelled'|'escalated'|…) · summary · appointment_id
```

`twilio_call_sid` is UNIQUE but nullable. Only `duration_seconds` is
telephony-specific.

| Component | Reused as-is |
|---|---|
| 12 tools + `/voice/tools/{name}` routes | yes |
| `mint_call_token` / `verify_call_token` / `execute_tool` | yes |
| `authorize_booking_change` — pure function, already transport-agnostic | yes |
| `booking_constraints` (lead time, past slots, multi-leg chains) | yes |
| `prompt_assembler` (per-shop tone, greeting, safety prompt) | yes |
| Escalation → `outcome='escalated'` → Action Center tile | yes |
| `service_catalog_match.py` — free-text → catalogue, no LLM | yes |

On WhatsApp, `caller_number` is the customer's WhatsApp number, which Meta has
verified — strictly stronger evidence of identity than a voice call's caller ID.
`authorize_booking_change` therefore works unchanged.

---

## 4. Schema — migration 23

```sql
-- whatsapp.inbound_messages
ADD COLUMN customer_id   uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL
ADD COLUMN wa_message_id text          -- + UNIQUE index: Meta retries webhooks
ADD COLUMN transcript    text          -- voice notes; NULL = not transcribed
ADD COLUMN intent        text
ADD COLUMN confidence    numeric
ADD COLUMN summary       text
ADD COLUMN read_at       timestamptz

-- whatsapp.outbound_messages
ADD COLUMN origin text NOT NULL DEFAULT 'kairo'   -- 'kairo' | 'phone'
```

`transcript` is a separate column and never overwrites `body`: NULL means "we
do not know", the same reasoning as `body_hash` (migration 21) and
`token_expires_at` (migration 22). No backfill.

A free-form reply is an `outbound_messages` row with `template_name IS NULL`,
`campaign_key IS NULL`, `preview` = the text. The column is already nullable and
the campaign idempotency index is partial on `campaign_key`, so nothing
conflicts — same shape the receipt path already uses.

**Per-service intake questions** (§7) get their own table rather than a column
on `business_app_core.services`: that schema belongs to the webapp and this repo
does not alter it.

```sql
CREATE TABLE IF NOT EXISTS voice_agent.service_intake (
  shop_id    uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  service_id uuid NOT NULL REFERENCES business_app_core.services(id) ON DELETE CASCADE,
  questions  text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, service_id)
);
```

In `voice_agent`, not `whatsapp`, because the phone agent needs exactly the same
thing in the next iteration.

---

## 5. Webhook

`_handle_change` gains a third field beside `messages` and
`message_template_status_update`:

**`message_echoes`** — messages the owner sent from the WhatsApp Business App.
Written to `outbound_messages` with `origin='phone'`, `status='sent'`.

- Does **not** open or extend the 24h window (only customer inbound does).
- **Clears the unread state** on that thread: the owner already answered.
- In B, an echo **suspends the agent on that thread immediately**. This is why
  echo handling is load-bearing rather than a nicety — without it the agent
  talks over the owner, and with coexistence that happens on day one.

> **Operator step.** `message_echoes` is a separate webhook field and must be
> subscribed in the Meta App Dashboard (App → Webhooks → WhatsApp Business
> Account). It is not code. Same category as `META_RECEIPT_SAMPLE_URL`.
> Unverified against a live WABA, like every WhatsApp item in CLAUDE.md.

**Inbound background task.** The webhook must answer 200 fast and must never
fail, so per-message work is a fire-and-forget `asyncio` task — with the task
object **retained in a module-level set** and discarded on completion. asyncio
holds only a weak reference; the call supervisor shipped this bug once already
(CLAUDE.md 2026-07-21) and it reappeared as intermittent silence.

The task does, in order:

1. If `message_type == 'audio'` → fetch media (Graph `GET /{media_id}` → URL,
   then download with the salon's business token) → transcribe → `transcript`.
2. Classify (§6) → `intent`, `confidence`, `summary`.

Either step refused for lack of credit (402) leaves the row as raw text and the
thread in the owner's queue. Fails closed in the direction that costs nothing
and loses nothing.

---

## 6. Classification — two stages

Both stages go through the marketing-engine LLM gateway
(`src/lib/llm/client.ts`), new route `src/routes/whatsapp-triage.ts`, metered
there like every other route. voice-booking calls it directly and gains one env
var, `MARKET_INTEL_API_URL`; the shared secret `MARKET_INTEL_SECRET` is already
configured (CLAUDE.md 2026-09-03).

**Stage 1 — classify, on every inbound message.**

```json
{ "intent": "booking", "confidence": 0.86, "summary": "Chiede posto sabato per colore" }
```

**Intent whitelist** — the set B is allowed to act on:

```
booking · reschedule · cancel · hours
```

Everything else (`price`, `complaint`, `promo_reply`, `opt_out`, `other`) and
**every low-confidence result** goes to a human. In A this only orders the
queue; in B it is the safety router, so it fails closed by construction: the
agent acts on an explicit allowlist, never on the absence of a red flag.

**Stage 2 — richer triage, only for what stage 1 admits.** A booking gets the
catalogue, the intake questions (§7) and the thread history; a `hours` question
does not. This is the stage the booking agent (§9) runs inside.

### Model and cost

`typesafe/jev-1.13`, single provider, so the allowlist entry is trivial:

```ts
'typesafe/jev-1.13': ['typesafe'],
```

Read from OpenRouter's endpoints API on 2026-09-21 — **one read, confirm in the
dashboard before either fact is load-bearing**:

| | |
|---|---|
| Endpoint | `typesafe/jev-1.13-20260917`, TypeSafe only |
| Context | 32,000 · max completion 28,800 |
| Tool calling | yes (`none` \| `auto` \| `required` \| `function`) |
| **ZDR** | **not indicated** |
| Price | **$42 / M input** · $0 / M output |
| Uptime 24h | 100% |

Two consequences, both blocking:

1. **No ZDR marking means every request fails.** `client.ts` sets `zdr: true` on
   every call and that is deliberate — a model with no ZDR endpoint must fail
   loudly rather than run outside the retention guarantee, because the payload
   is a real salon's customer list. Either TypeSafe has a ZDR endpoint that the
   endpoints API did not surface, or this model cannot carry customer text under
   the current guarantee. Resolve before building on it.
2. **$42/M input is the expensive stage, not the cheap one.** At ~500 input
   tokens that is ~$0.02 per classified message; a salon at 100 messages/day is
   ~$60/month in classification alone, roughly 400× the flash models already in
   `PROVIDERS_BY_MODEL`. **Recommended ordering: a flash model runs stage 1 on
   every message, Jev runs stage 2 on the bookings.** That is the inverse of
   "Jev classifies first" and is written this way pending confirmation.

No service catalogue in the stage-1 prompt: it would be injected on every
inbound message for a result A does not use, and at Jev's input price that is
the single most expensive thing in the stack. Service chips in A come from
`service_catalog_match.py`, which costs nothing. LLM service matching lands in
stage 2, where the catalogue is in the prompt anyway.

### Agents vs tools

The booking flow **is** an agent: an LLM in a loop over `TOOL_DEFS`, which is
what §9 builds and what the voice path already runs.

Service retrieval is **a tool, not an agent**. A salon catalogue is 20–50 rows;
it fits whole in a prompt and `get_services` already exists. A dedicated
retrieval agent adds a hop, latency and a second LLM bill for a `SELECT`.
Upgrade path, if it is ever needed: catalogues large enough not to fit, or
synonym-heavy matching that the token matcher misses — the same upgrade path
`service_catalog_match.py` already names in its own `ponytail:` comment.

---

## 7. Catalogue grounding — always

**Rule: the agent may only ever name, propose, or book a service that exists in
that shop's own catalogue.** Enforced in three layers, not in prose:

1. **Injection.** The prompt receives only that shop's services — `id`, name,
   duration, price — and nothing else.
2. **Rejection.** A `service_id` in the model's output that is not in the
   injected list is dropped *before* any tool call. ~5 lines, and it is the
   layer that makes the rule checkable rather than hopeful.
3. **Tool validation.** `create_appointment_chain` already raises
   `invalid_service` for a service that vanished between the availability check
   and the write (CLAUDE.md 2026-07-21). Unchanged, and now a third net.

**Per-service intake questions.** The owner writes, per service, what the agent
must ask before booking it:

> *Colore* — "Chiedi se è ritocco radici o colore completo, e se ha già fatto
> una decolorazione negli ultimi 2 mesi."

Stored in `voice_agent.service_intake.questions`, injected only when that
service is in play. **Capped at 500 characters**, because it enters the prompt on
every turn of every conversation touching that service, and an owner pasting an
essay pays for it on each one.

UI: a field per service in the configuration panel, beside the existing tone and
greeting settings. Empty = no extra questions, which is today's behaviour.

---

## 8. Increment A

### Endpoints (voice-booking)

| | |
|---|---|
| `GET /whatsapp/threads/{shop_id}` | phone, customer, last message, window expiry, unread, intent |
| `GET /whatsapp/threads/{shop_id}/{phone}` | merged timeline, **and marks read** — one endpoint fewer |
| `POST /whatsapp/reply` | free-form send |

The window is `max(received_at) > now() - 24h` on `inbound_messages`, checked
**before** the Graph call. Closed → `{"ok": false, "error": "session_window_closed"}`,
never a request destined for Meta error `131047`.

New Graph client functions: `send_text()`, `get_media()`.

### UI

**Inbox → Conversazioni becomes the WhatsApp thread list, and that is the only
place this lives.** No tile in Marketing → engage: one surface, not two copies
of the same list. The telephony surfaces are hidden for now — `InboxTabBar`
already takes a `visible` prop, so this is a filter, not a deletion, and the
voice components stay on disk for the next iteration.

- Thread list in **two sections, "Da gestire" first**: threads stage 1 routed to
  a human (intent outside the whitelist, or low confidence), threads the agent
  escalated, and — once B ships — threads where the agent stood down. Everything
  else sits below in plain recency order. The section is the product: the owner
  should open the Inbox and see only what actually needs them.
- Row: window countdown, intent chip, `📱` badge on replies sent from the
  owner's phone, filters for unread and campaign replies.
- Thread: merged timeline, transcript shown inline for voice notes, reply box
  disabled with an explanation outside the window rather than an error after the
  click.
- Actions: reply · create booking (service chips from the token matcher) ·
  create/link customer for unknown numbers.
- Refresh: **polling, 15s**. There is no realtime infrastructure in the webapp
  and this does not justify introducing one.

### Deliberately out of A

- **No AI draft button.** B writes whole replies; a draft button would be
  near-dead the day B ships.
- **No consent revocation action.** `opt_out` surfaces as a chip; the owner acts
  in the customer record. Owner's decision — recorded because it leaves free-text
  opt-out requests dependent on the owner reading them.
- **No automated nudge.** That is agent behaviour (§9). In A the countdown is a
  signal to the owner.
- **No template re-open** of a closed window.
- **Notifications are pull-only.** `send_push` still only logs (flagged
  2026-07-24) and there is no email template in the webapp. A thread with a 24h
  fuse and no notification *will* expire unanswered. Known limit of A.
- **To add:** inbound bodies belong in the subject-access artifact
  (`GET /whatsapp/messages`), which does not currently include them.

---

## 9. Increment B — booking agent

- `channel` column on `voice_agent.calls` (`voice` | `whatsapp`). A WhatsApp
  conversation opens a row, mints a call token, and drives `execute_tool()`.
  No new authz, no new booking logic, no new tools.
- **Turn loop:** message → LLM with `TOOL_DEFS` → tool calls → text →
  `send_text()`. Simpler than voice: a text tool loop continues by itself, so
  the whole class of bug the call supervisor exists to fix (CLAUDE.md
  2026-07-21) does not arise here.
- **Activation:** per-shop opt-in **plus** the intent whitelist. The agent
  handles booking / reschedule / cancel / hours; everything else, and anything
  low-confidence, goes to the owner.
- **Handover:** any human write suspends the agent on that thread — including an
  echo from the phone. Resume is an explicit owner action. Escalation reuses
  `outcome='escalated'` and the Action Center tile.
- **Disclosure:** the first agent message states it is an assistant. Not
  optional.
- **Nudge at ~20h:** one last in-window message inviting the customer to write
  back, which is what reopens the window. Free, and the only mitigation that
  costs nothing.
- **Cost:** per-turn charge through `webapp_credits.py` (`run_type='whatsapp_turn'`),
  with a per-conversation ceiling. Empty basket → the agent stands down
  silently and the thread goes to the owner with a badge. A customer never gets
  half an answer.
- **Prerequisite:** close `update_customer_from_call`'s missing shop check
  (flagged 2026-07-17). It is reachable only with a valid minted token, so it is
  not exposed today — but opening a second channel that mints those tokens is
  the wrong moment to still be carrying it.

### Known holes in B, accepted

- **Double booking against the owner's phone.** The owner types "sì, sabato alle
  15" from their phone while the agent assigns that slot to someone else. The
  constraint layer catches the slot conflict; the customer has already had a yes
  from a human. Not fixable in code — the echo-suspends-agent rule is the
  mitigation.
- **Shared phones.** A family phone or a receptionist booking for someone else
  fails `authorize_booking_change`'s phone match, exactly as on voice today.
- **No locking** between owner-on-phone and owner-in-webapp. Acceptable at one
  chair; revisit with more staff.

---

## 10. Verification

- Migration 23 applied **twice** against a scratch Postgres, exit 0 both times,
  columns and comments confirmed by `\d`. Non-trivial statements (the thread
  list query, the window predicate, the echo write) executed against real rows
  rather than assumed — the standing bar on every migration in this repo.
- Unit tests: window open/closed/reset-by-new-inbound; echo clears unread and
  does not extend the window; duplicate `wa_message_id` is a no-op; reply
  refused outside the window before any Graph call; classifier 402 leaves raw
  text; a `service_id` outside the injected catalogue is rejected.
- `python -m pytest tests/ --ignore=tests/live_db --ignore=tests/live_twilio -q`
  against a baseline measured on this branch with `git stash`, not quoted from a
  previous entry.
- Webapp `npx tsc --noEmit` exits 0.
- **No live Meta call.** Consistent with every WhatsApp entry in CLAUDE.md, the
  Graph interactions are verified against the API contract and unit tests. The
  `message_echoes` payload shape is the largest unverified assumption here.

---

## 11. Open items

1. **`typesafe/jev-1.13` and ZDR.** The endpoints API does not mark the single
   TypeSafe endpoint zero-data-retention, and `client.ts` sets `zdr: true` on
   every request by design. Confirm in the OpenRouter dashboard. If there is no
   ZDR endpoint, this model cannot carry salon customer text without changing a
   retention guarantee that was set deliberately on 2026-09-16 — which is a
   separate decision, not a flag to flip in passing. **Blocking for §6.**
2. **Stage ordering.** §6 recommends flash for stage 1 and Jev for stage 2, on
   the $42/M input price. Confirm, or state that Jev classifies everything and
   the cost is accepted.
3. **Agent granularity.** §6 builds service retrieval as a tool and booking as
   an agent. Confirm, or say that retrieval should be its own agent from the
   start.
