# Messaging: SMS marketing + WhatsApp booking & reminders — design draft

**Status (2026-08-24): PARTLY SHIPPED, and every WhatsApp section below is
SUPERSEDED.** Kept for §8 and §9 alone — WhatsApp reminders and self-booking,
still unbuilt, and this is their only design record.

| Section | State |
|---|---|
| §1–§3, §4.1, §4.3, §5.2, §5.3, §7, §10 (SMS) | **shipped** 2026-08-12/15 — the truth is `docs/knowledge/api/sms.md` |
| §4.2, §5, §6 (WhatsApp on Twilio) | **dead.** WhatsApp left Twilio on 2026-08-24 |
| §8, §9 (WhatsApp reminders, self-booking) | **still the design.** Not built. Read these. |

**What changed under the WhatsApp sections and why they can't just be
patched:** Twilio cannot register a WABA it did not create (error 63103) and
its migration path deletes the salon's WhatsApp Business App, so the whole
subaccount/Senders/Content-API shape in §4.2 and §6 is gone. Kairo is a Meta
Tech Provider talking to `graph.facebook.com` directly. The billing model in
§5 inverted too: the salon's own card is on the salon's own WABA and Meta
charges it directly, so nothing debits AI credits on this channel.

**Read instead:** `CLAUDE.md` §2026-08-24 (both entries),
[api/whatsapp.md](knowledge/api/whatsapp.md),
[providers.md](knowledge/providers.md#whatsapp-meta-cloud-api-tech-provider).

**Date:** 2026-08-11. Supersedes the first cut of the same day (shared-sender model).

> Working document, not durable history. Delete it once §8 and §9 ship.
> Deliberately not under `docs/superpowers/specs/` — that directory was removed
> on 2026-07-24 by owner decision.

Spans two repos: **voice-booking** (schemas, sending, webhooks, billing) and
**webapp** (all salon-facing UI). No `marketing-engine` changes.

---

## 1. Channel split (decided 2026-08-10)

| Use case | Channel | Why |
|---|---|---|
| Marketing — few, targeted, per-customer copy | **SMS** | WhatsApp forbids free-form business-initiated messages; templates can't carry per-customer generated copy |
| Self-booking (LLM) | **WhatsApp** | Customer-initiated → 24h service window → free-form replies unlimited **and free** |
| "Confirm tomorrow's appointment" | **WhatsApp** | Utility template: cheaper than SMS, quick-reply buttons |
| "Leave a review" | **WhatsApp** | Same, with a category caveat — §8.1 |

## 2. One number per shop, three channels (decided 2026-08-11)

The shop's existing Estonian mobile DID (`voice_agent.shop_telephony.kairo_number`)
is the voice number, the SMS sender **and** the WhatsApp sender. No second
number, no shared Kairo sender, no shop-disambiguation turn: the recipient
number identifies the shop on every channel, exactly as `X-Shop-Id` does today.

### 2.1 The blocker this creates — verify before anything else

**Can a Twilio number simultaneously serve PSTN voice (SIP → OpenAI) and be a
registered WhatsApp sender?** The whole design rests on yes. Registration uses
voice-OTP for non-SMS-capable numbers, which implies coexistence, but that is an
inference, not a verified fact.

Verify by registering **one** QA number as a WhatsApp sender and then placing a
normal inbound voice call to it. If voice breaks, the fallback is a second DID
per shop for WhatsApp (+$3/mo/shop) and §2 is rewritten — so this is the first
task in Phase 0, not a footnote.

### 2.2 What per-shop senders cost that a shared sender wouldn't

Each sender needs a **Meta display name approval** — asynchronous, per shop.
This is *not* the per-salon KYC the 2026-07-16 Estonia decision eliminated
(business verification stays at the Kairo-entity level, one WABA), but it is a
per-shop async review with a pending state, structurally the same shape as the
old Telnyx `pending_review` flow that Twilio's synchronous purchase replaced.

Consequence: provisioning is **two-phase** — the number is live for voice and
SMS immediately, WhatsApp becomes available when Meta approves the display name.
`shop_telephony` needs `whatsapp_status` (`none|pending|approved|rejected`) and
the UI needs a pending state. `NumberActivationBanner.tsx` already exists in the
webapp Inbox and is the natural home.

## 3. What already exists — do not rebuild

Verified against QA `br-damp-recipe-agnys6xk` and both working trees.

**voice-booking**
- `execute_tool()` → `/voice/tools/{name}` → safety layer → `queries.py`.
  12 tools, authz, booking constraints, 10s timeout. Nothing below the
  token-resolution step is voice-specific.
- `authorize_booking_change()` is already a pure function over
  `(caller_number, call_shop_id, owner)` — channel-agnostic.
- `booking_engine/db/token_basket_queries.py` — already debits
  `business_app_core.ai_token_basket` granted-first and logs to `ai_token_log`.
- `clients/twilio_numbers.py`, `X-Twilio-Signature` verification.
- `POST /voice/heartbeat/forwarding` behind `require_control_plane_token` —
  the scheduler-endpoint pattern (and currently orphaned, §6.2).

**business_app_core**
- Consent: `customers.marketing_consent`, `_granted_at`, `_withdrawn_at`,
  `_source`, plus `privacy_consent`. **Single source of truth for both channels.**
- `appointments.confirmation_status` CHECK already allows
  `pending_sms_confirmation`; `appointments.source` CHECK already allows
  `'whatsapp'`.
- `ai_token_basket` / `ai_token_log`, `shops.plan_id → subscription_plans`.

**webapp**
- `hasActiveMarketingConsent()` (`src/lib/privacy/marketing-consent.ts`) —
  used both client-side and re-checked server-side, fail-closed.
- **The marketing message generator already exists and already meters credits**:
  `POST /api/v1/hair-salon/customers/[id]/retention-message` →
  `MARKET_INTEL_API_URL/retention-message` → `deductCredits(shopId,
  rawToUserCredits(llm_cost_usd))`. Consent re-checked at generation.
- `RetentionMessageModal.tsx` renders the draft and stops at clipboard —
  *"The owner reads it, edits it in their head, and copies it into WhatsApp."*
  That last step is what §7 replaces.
- Inbox shell: `InboxTabBar`, `ConversationsTab`, `AnalyticsTab`,
  `ConfigurationTab`, `BalanceBanner`, `NumberActivationBanner`,
  `ForwardingHeartbeatBanner`. Currently behind `ComingSoonVeil`.

### `business_app_core` changes: two nullable columns on `ai_token_log` (§5.2). Nothing else.

## 4. Schemas

Two dedicated schemas in the shared Neon DB, `sms` and `whatsapp`, owned by
voice-booking. Both reference `business_app_core`/`voice_agent` by id, never by
copy. Register them in the webapp's migration chain **after** `business_app_core`
and `voice_agent` (FK targets) — since 2026-07-24 webapp applies all schemas in
a fixed order.

### 4.1 `sms`

```sql
CREATE SCHEMA IF NOT EXISTS sms;

CREATE TABLE sms.campaigns (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id     uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  name        text NOT NULL,
  status      text NOT NULL DEFAULT 'draft'
              CHECK (status IN ('draft','approved','sending','sent','cancelled')),
  approved_at timestamptz,
  approved_by uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sms.outbound_messages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id       uuid REFERENCES sms.campaigns(id) ON DELETE CASCADE,  -- NULL = one-off
  shop_id           uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  customer_id       uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  to_phone          text NOT NULL,   -- E.164
  from_number       text NOT NULL,   -- shop DID, snapshotted
  body              text NOT NULL,   -- final wire text, opt-out footer included
  segments          smallint NOT NULL,
  encoding          text NOT NULL CHECK (encoding IN ('gsm7','ucs2')),
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','sent','delivered','failed','suppressed')),
  suppressed_reason text,            -- no_consent|opted_out|no_phone|insufficient_credits
  provider_sid      text,
  price_usd         numeric,         -- what Twilio charged us
  credits_charged   integer,         -- what the shop paid (§5)
  error_code        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  sent_at           timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Idempotency as a DB constraint, not application logic.
CREATE UNIQUE INDEX sms_outbound_campaign_customer_uniq
  ON sms.outbound_messages (campaign_id, customer_id)
  WHERE campaign_id IS NOT NULL AND customer_id IS NOT NULL;
CREATE INDEX sms_outbound_provider_sid_idx ON sms.outbound_messages (provider_sid);

CREATE TABLE sms.opt_outs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id          uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  phone_normalized text NOT NULL,
  keyword          text NOT NULL,
  raw_body         text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shop_id, phone_normalized)
);
```

**Why `sms.opt_outs` when consent lives in `customers`:** a STOP must be honoured
from a phone that maps to no customer row (import, wrong number, deleted
customer). This is the suppression list of last resort plus the legal evidence
trail. When the phone *does* map to a customer, the handler **also** sets
`marketing_consent = false, marketing_consent_withdrawn_at = now(),
marketing_consent_source = 'sms_stop'` — otherwise the webapp keeps showing the
customer as consenting while SMS silently suppresses them.

### 4.2 `whatsapp`

```sql
CREATE SCHEMA IF NOT EXISTS whatsapp;

CREATE TABLE whatsapp.conversations (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id            uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  customer_id        uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  wa_phone           text NOT NULL,   -- customer, E.164
  sender_number      text NOT NULL,   -- the shop's DID
  session_expires_at timestamptz,     -- 24h from last inbound; NULL/past = closed
  state              jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_inbound_at    timestamptz,
  last_outbound_at   timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sender_number, wa_phone)    -- sender_number ⇒ shop, so this is shop-scoped
);

CREATE TABLE whatsapp.messages (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id  uuid NOT NULL REFERENCES whatsapp.conversations(id) ON DELETE CASCADE,
  direction        text NOT NULL CHECK (direction IN ('inbound','outbound')),
  kind             text NOT NULL CHECK (kind IN ('freeform','template')),
  template_name    text,
  template_vars    jsonb,
  body             text,
  provider_sid     text,
  status           text,              -- queued|sent|delivered|read|failed
  billing_category text,              -- service|utility|marketing (NULL inbound)
  price_usd        numeric,
  credits_charged  integer,
  error_code       text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX whatsapp_messages_conv_created_idx
  ON whatsapp.messages (conversation_id, created_at DESC);

CREATE TABLE whatsapp.scheduled_sends (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind           text NOT NULL CHECK (kind IN ('appointment_confirm','review_request')),
  shop_id        uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  appointment_id uuid NOT NULL REFERENCES business_app_core.appointments(id) ON DELETE CASCADE,
  customer_id    uuid NOT NULL REFERENCES business_app_core.customers(id) ON DELETE CASCADE,
  send_after     timestamptz NOT NULL,
  status         text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','sent','skipped','failed')),
  skip_reason    text,
  message_id     uuid REFERENCES whatsapp.messages(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, appointment_id)       -- a reminder is sent at most once, ever
);
CREATE INDEX whatsapp_scheduled_due_idx
  ON whatsapp.scheduled_sends (send_after) WHERE status = 'pending';
```

**No `whatsapp.templates` table.** Template names, bodies and approval status are
owned by Meta; a local mirror drifts — the exact anti-pattern the 2026-07-24
entry called out about hand-copied schema. Templates live as a dict in code
(name → category → variable order), keyed by `shops.language`.

### 4.3 `voice_agent.shop_telephony` additions

```sql
ALTER TABLE voice_agent.shop_telephony
  ADD COLUMN whatsapp_status  text NOT NULL DEFAULT 'none'
    CHECK (whatsapp_status IN ('none','pending','approved','rejected')),
  ADD COLUMN whatsapp_sender_sid text,
  ADD COLUMN whatsapp_display_name text;
```

## 5. Billing — 2× Twilio, through the AI credit basket

Number rental is covered by the subscription. **Per-message cost is billed as AI
credits at 2× what Twilio charges us.** The existing rail already carries
`1000 credits = $1`.

### 5.1 A separate converter — do not reuse `rawToUserCredits`

`rawToUserCredits()` (`webapp/src/lib/ai/run-credits.ts`) applies `MARGIN = 10`
and `Math.max(1, …)`. Both are wrong here:

- 10× is the LLM margin; sends are 2×.
- The floor of 1 would charge a credit for a **free** WhatsApp service message.
  Free must stay free, or the economics of §9's "customer speaks first" model
  quietly invert.

```
send_credits(twilio_usd) = twilio_usd <= 0 ? 0 : ceil(twilio_usd * 2 * 1000)
```

| Message | Twilio (list) | Credits |
|---|---|---|
| SMS to Italy, 1 segment | $0.093 | **186** |
| SMS to Italy, 2 segments (typical marketing) | $0.186 | **372** |
| WhatsApp Utility template (IT, approx.) | ~$0.045 | **~90** |
| WhatsApp free-form inside 24h session | $0 | **0** |

> **The WhatsApp rows here are dead (2026-08-24), and so is the correction that
> used to sit in this box.** On 2026-08-22 this said a free-form reply costs
> ~$0.005 because Twilio charged a flat per-message platform fee on every
> category. That was true of Twilio and is no longer true of anything: WhatsApp
> now goes direct to Meta, the platform fee is gone, and a free-form reply
> inside the 24h window really is **$0** — the original table above was right
> by accident. More importantly **no WhatsApp send debits AI credits at all**,
> because the salon's card is on the salon's own WABA and Meta bills it
> directly. The shipped table is
> `booking_engine/services/messaging/whatsapp_pricing.py`, documented in
> [providers.md](knowledge/providers.md#whatsapp-meta-cloud-api-tech-provider).
> §5.1's SMS reasoning still holds in full: the 2× margin, the separate
> converter, and the reason not to floor at 1 credit.

Reality check: Base (€39, 40 000 granted credits/mo) buys ~107 two-segment
marketing SMS *if the shop spends nothing else* — and the generation is charged
separately at 10× LLM. Surface the cost in the UI before the click (§7); an
owner who blows a month's basket on one campaign will not blame arithmetic.

**Generation and sending are two charges, deliberately.** Generation already
bills 10× LLM at `retention-message`. Sending adds 2× Twilio. A regenerate costs
generation only; a send costs the send only.

### 5.2 Where the charge is written

The send runs in voice-booking (Python), so it uses the existing
`token_basket_queries.py` deduct — granted-first, logged to `ai_token_log`.
`ai_token_log` has `voice_call_id` but no equivalent for a message, so a debit
would be untraceable. Mirror the existing shape:

```sql
ALTER TABLE business_app_core.ai_token_log
  ADD COLUMN sms_message_id      uuid,
  ADD COLUMN whatsapp_message_id uuid;
```

(Nullable, no FK — `ai_token_log` must not depend on schemas it doesn't own.)

**Insufficient credits fails closed:** the send is not attempted, the row is
marked `suppressed_reason = 'insufficient_credits'`, and `BalanceBanner` surfaces
it. Never send something that can't be billed. This differs from the voice path,
which drains the basket and proceeds — a live call can't be un-answered, a
message can simply wait.

### 5.3 Provisioning gate — any active paid plan (decided 2026-08-11)

Provisioning requires `shops.plan_id IS NOT NULL` and a live subscription; no
tier logic. Note this is the **first** subscription gate in the webapp —
`grep tier src/lib/*.ts` currently returns nothing, and gating today is by
vertical bundle (`vertical-features.ts`) and role (`role-visibility.ts`). Put it
in one predicate, `hasActiveSubscription(shopId)`, server-side enforced on the
provisioning route, and let the UI mirror it. Do not scatter tier checks.

## 6. voice-booking: components

New package `booking_engine/services/messaging/`:

| File | Purpose |
|---|---|
| `gsm7.py` | encoding detection, sanitisation, segment count (pure) |
| `send_credits.py` | §5.1 converter + fail-closed debit (pure conversion, thin debit) |
| `sms_send.py` | consent/opt-out gate → sanitise → footer → Twilio → log |
| `sms_inbound.py` | STOP parsing + consent withdrawal |
| `wa_send.py` | template + free-form send, session-window aware |
| `wa_agent.py` | LLM loop over the booking tool allowlist |
| `wa_router.py` | inbound (sender_number, wa_phone) → shop, customer, conversation |
| `scheduling.py` | enqueue reminders, drain due sends |

### 6.1 Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `POST /messaging/tick` | control-plane token | cron entry: enqueue + drain reminders, advance campaigns |
| `POST /sms/send` | control-plane token | synchronous one-off send (webapp "Invia SMS") |
| `POST /sms/webhook/inbound` | `X-Twilio-Signature` | STOP handling |
| `POST /sms/webhook/status` | `X-Twilio-Signature` | delivery receipts + real price |
| ~~`POST /whatsapp/webhook/inbound`~~ | — | **gone.** One Meta webhook now: `POST /whatsapp/webhook`, `X-Hub-Signature-256` |
| ~~`POST /whatsapp/webhook/status`~~ | — | **gone.** Same endpoint, same signature |

`/sms/send` is synchronous on purpose: the owner clicks Invia in a modal and
needs "Inviato" or "Credito insufficiente" now, not within the hour. Bulk and
scheduled sends go through the tick.

**Verify:** whether webapp→voice-booking calls already exist and with which
secret. `docs/knowledge/providers.md#voice-booking-microservice` (webapp)
documents the reverse direction; `CONTROL_PLANE_SECRET` is a Fly app secret. If
no path exists, `/sms/send` needs the URL + secret wired into webapp env.

### 6.2 Scheduler

One workflow, `.github/workflows/messaging-cron.yml`, `cron: '0 * * * *'`,
POSTing `/messaging/tick`. GH Actions cron drifts 5–15 min; irrelevant because
`send_after` is a threshold and `UNIQUE (kind, appointment_id)` makes a double
fire a no-op.

**Fixed in passing:** the same workflow calls `POST /voice/heartbeat/forwarding`,
which has had no scheduler since the 2026-07-18 Lambda/EventBridge removal and
currently never runs.

### 6.3 Consent gate (both channels)

```
may_send_marketing(customer) =
      customer.marketing_consent
  AND customer.marketing_consent_granted_at IS NOT NULL
  AND customer.marketing_consent_withdrawn_at IS NULL
  AND NOT EXISTS (sms.opt_outs for this shop + phone)
```

Mirrors webapp's `hasActiveMarketingConsent()` exactly, plus the opt-out list.
Utility messages (confirm-tomorrow) need an appointment and `privacy_consent`,
not marketing consent. The opt-out footer
(`" Rispondi STOP per non ricevere più."`) is appended **server-side** — never
left to the LLM.

## 7. webapp: Marketing → Touchpoint Clienti

**Rename.** `src/i18n/it.ts:623` `tab_customers: 'Salute Clienti'` →
`'Touchpoint Clienti'`, plus the `en` equivalent. Component filenames
(`CustomersTab.tsx`, `customers/`) stay — a rename there is churn with no reader.

**Wire the send.** `RetentionMessageModal.tsx` today ends at Copia. Add:

```
[ Invia SMS ]  [ Copia ]  [ Rigenera ]                    [ Chiudi ]
 ↳ +39 ••• 4821 · 2 SMS · 372 crediti          (before the click)
 ↳ confirm → POST /api/v1/hair-salon/customers/[id]/send-sms
 ↳ "Inviato" | "Credito insufficiente" | "Il cliente ha revocato il consenso"
```

Recipient, segment count and credit cost are shown **before** the click.
Segment count comes from the same GSM-7 logic as `gsm7.py` — one shared
implementation would be ideal but crosses a language boundary; duplicate the
rule and cross-reference both, since the webapp number is a preview and
voice-booking's is authoritative at send time.

New route `POST /api/v1/hair-salon/customers/[id]/send-sms`: `getShopId` →
`getCustomerById` → `hasActiveMarketingConsent` re-check (fail closed, same as
the generation route) → forward to voice-booking `/sms/send`. It does **not**
deduct credits; the sender does, so there is exactly one debit path.

**No campaign CRUD API for now.** One-off per-customer sends is what the
existing UI does and what §1 calls for ("few, targeted"). `sms.campaigns` exists
for the batch case; nothing builds against it in Phase 2.

## 8. Reminders (WhatsApp)

| Template | Trigger | `send_after` | Category |
|---|---|---|---|
| `appointment_confirm` | appointment tomorrow, status `scheduled`/`confirmed` | 18:00 Europe/Rome day before | Utility |
| `review_request` | appointment `completed` | `end_time + 3h` | Utility (**at risk**) |

Quick-reply buttons on `appointment_confirm` write
`appointments.confirmation_status = 'confirmed'`. A tap also opens the 24h
window, so "actually, can I move it?" is handled free-form — and free — by the
same agent as §9.

### 8.1 Risk: `review_request` category

Meta treats Utility as transaction-specific follow-ups; review solicitation is
frequently reclassified as Marketing on review. Submit as Utility, but gate it
through `may_send_marketing()` **regardless**, so a reclassification cannot
produce an unconsented send. Don't budget for it as free.

`Europe/Rome` is a constant, not a column — `shops` has `language` but no
timezone, and every shop is Italian. Add the column when that stops being true.

## 9. WhatsApp self-booking

```
inbound webhook → verify signature → sender_number ⇒ shop, wa_phone ⇒ customer
  → upsert conversation, session_expires_at = now() + 24h
  → append inbound message
  → agent loop: LLM ⇄ execute_tool(allowlist)   [free-form, free, unlimited]
  → reply, append outbound message
```

**Tool allowlist**, not all 12: `get_services`, `check_availability`,
`create_booking`, `modify_booking`, `cancel_booking`, `get_my_bookings`.
Call-transfer, escalation and identity tools have no meaning here.

Bookings written with `source = 'whatsapp'` — already a legal CHECK value.

**Identity is stronger than voice here.** Inbound caller ID is spoofable; a
WhatsApp sender number is verified by Meta.

### 9.1 The one real refactor

`execute_tool()` verifies a token whose claims resolve to a `voice_agent.calls`
row; the handlers then read `caller_number`/`shop_id` off it. WhatsApp has no
call row.

- **(a) Synthetic call rows** — smallest diff, pollutes `voice_agent` and call
  analytics with non-calls. Rejected.
- **(b) Generalise the resolution step** so handlers ask a small accessor for
  `(caller_number, shop_id)` that either a call row or a WhatsApp conversation
  can answer. **Recommended** — `authorize_booking_change()` is already
  channel-agnostic, so this is one indirection, not a rewrite.
- **(c) Duplicate the tool routes under `/whatsapp/tools/`** — two copies of the
  safety layer, guaranteed to drift. Rejected.

(b) touches production authz: its own task, with the 2026-07-17 tool-dispatch
security tests re-run through the new entry point, **before** any agent code.

## 10. webapp: Inbox (decided 2026-08-11)

```
Inbox
├─ Conversazioni    ☎ voice + 💬 WhatsApp in one list, channel filter
├─ Promemoria       confirm + review queue: pending / sent / skipped
├─ Analytics        + WhatsApp volume, booking conversion, reply rate
└─ Configurazione   + Canali: number, WhatsApp status, reminders on/off
```

Channel-agnostic threads because a salon thinks "my conversations with Giulia",
not "my WhatsApp". `ConversationsTab`/`CallList`/`CallRow` generalise from call
to thread; `CallDetailDrawer` renders a transcript or a chat by channel.
`Promemoria` is separate because reminders are outbound-only and read oddly
interleaved with real conversations.

Two existing constraints to respect: `/inbox` is behind `ComingSoonVeil`, and
`role-visibility.ts` blocks `inbox_analytics`/`inbox_configuration` from
`member`. A new `inbox_reminders` tile key needs a deliberate decision on that
list — reminders expose customer contact patterns, so **block for `member`**
unless someone argues otherwise.

**Temporary by the owner's own framing.** When Inbox gets too crowded, the
natural cut is *conversations* (inbound, per-customer, Inbox) vs. *outbound
programmes* (campaigns, reminder rules — Marketing/Touchpoint). Reminders are
the piece that would move.

## 11. Open questions

1. **§2.1 — voice + WhatsApp on one Twilio number.** Load-bearing for the whole
   per-shop model. Verify first, on one QA number.
2. **Number provisioning flow is TBD** (owner). Known constraints it must
   satisfy: gate on active paid plan (§5.3); two-phase because WhatsApp display
   name approval is async (§2.2); `NumberActivationBanner.tsx` already exists as
   the surface.
3. **webapp → voice-booking call path** — verify it exists and with which secret
   (§6.1).
4. **Foreign sender number.** Under Path 1 forwarding, customers know the
   salon's Italian number and have never seen the `+372`. Mitigation for now:
   salon name in the first ~20 chars. Phase 4 is alphanumeric sender ID.
5. **`marketing_consent` provenance is unaudited.** The columns exist; nothing
   verifies *how* consent was obtained. Audit before the first real campaign.

## 12. Phases

| Phase | Repo | Contents | Blocked by |
|---|---|---|---|
| **0** | — | §2.1 verification; Twilio funded + EE bundle; Meta business verification; one approved sender; `/messaging/tick` skeleton + cron | ops |
| **1** | vb + webapp | SMS send: `sms` schema, `gsm7.py`, `send_credits.py`, `/sms/send`, STOP + status webhooks · Touchpoint rename + Invia SMS in the modal | 0 |
| **2** | vb + webapp | WhatsApp reminders: `whatsapp` schema, 2 templates, `scheduling.py` · Promemoria tab | 0 |
| **3** | vb + webapp | WhatsApp self-booking: §9.1 refactor **first**, then webhook, router, agent · channel-agnostic Conversazioni | 2 |
| **4** | — | alphanumeric sender ID, campaign batches | demand |

SMS before WhatsApp this time (reversed from the first draft): the generator,
the consent gate and the UI already exist, so Phase 1 is the shortest path to
something an owner can use, and it proves the credit rail before WhatsApp adds
template and session mechanics on top.

## 13. Testing

- Pure, unit-tested, no DB: `gsm7.py`, `send_credits.py`, `may_send_marketing()`,
  template variable binding, `send_after` computation.
- Twilio faked at the client boundary (the injectable `connect=` seam from
  `call_supervisor.py`).
- `tests/live_db/`: tick idempotency (run twice → one message), consent
  suppression, STOP from an unknown phone, insufficient-credits fail-closed,
  cross-shop isolation on WhatsApp routing.
- Phase 3 re-runs the 2026-07-17 tool-dispatch security tests through the
  WhatsApp entry point.
- webapp: `send-sms` route test mirroring the existing
  `retention-message/route.test.ts`, including the consent-revoked 403.

## 14. Documentation obligation

**voice-booking** — adds endpoints, tables and a provider integration:
`docs/knowledge/architecture.md`, `database.md`, `providers.md`, `api/`.

**webapp** — adds a feature and a provider integration, and changes a hard
constraint (the first subscription gate): `docs/knowledge/features.md`,
`providers.md`, `architecture.md`, `decisions.md`. Per webapp `CLAUDE.md`, in
the same change, not as a follow-up.
