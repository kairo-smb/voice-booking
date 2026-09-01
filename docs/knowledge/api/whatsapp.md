# WhatsApp API

`booking_engine/api/routes/whatsapp.py`, mounted at `/api/v1`. Onboards a salon onto WhatsApp with its own WABA, injects Kairo's approved templates into it, and queues personalised marketing that drips out across the day — or across a week, for a bulk campaign.

**Read [Providers → WhatsApp](../providers.md#whatsapp-meta-cloud-api-tech-provider) first** if you're new to this: the constraints (approved templates only, per-recipient marketing caps, coexistence) explain why these endpoints exist in this shape.

> **This channel does not go through Twilio.** Twilio cannot register a WABA it did not create ([error 63103](https://www.twilio.com/docs/api/errors/63103) — it must attach the WABA to *Twilio's* Meta credit line, and Meta won't release an existing payment method), and its migration path deletes the salon's WhatsApp Business App. Kairo is a Meta **Tech Provider** and talks to `graph.facebook.com` directly. Twilio still owns voice and SMS.

## Auth

| Routes | Scheme |
|---|---|
| `/whatsapp/onboarding/*`, `/whatsapp/status/*`, `/whatsapp/templates/*`, `/whatsapp/campaigns*`, `/whatsapp/messages/*` | Control-plane bearer (`CONTROL_PLANE_SECRET`) — the webapp is the only caller |
| `GET /whatsapp/webhook` | Meta's handshake: `hub.verify_token` must equal `META_VERIFY_TOKEN` |
| `POST /whatsapp/webhook` | `X-Hub-Signature-256`, HMAC-SHA256 of the **raw body** with `META_APP_SECRET` |

One app secret covers every customer's traffic — unlike Twilio, which signs with the token of the account owning the resource. `booking_engine/services/meta_signature.py` verifies the bytes as received; re-serialising the parsed JSON changes whitespace and key order and the digest stops matching.

---

## Onboarding

**Two calls.** Meta's Embedded Signup is a browser popup with no server-side equivalent — a WABA can only be created (or connected) by the salon itself — but the popup also performs verification, so there is no OTP round trip and no inbound-SMS webhook.

### `POST /whatsapp/onboarding/start`

```json
{ "shop_id": "…", "display_name": "Salone Bellezza" }
```

**BYO WABA only — coexistence, always.** The salon's **existing WhatsApp Business App number** stays live on their phone: they keep chatting with clients from the app while Kairo sends templates through Cloud API. This is what the whole channel is sold on, and what Twilio cannot offer. An earlier `source` parameter also accepted `"new"` — provisioning a fresh WABA through Kairo, on a number not yet on WhatsApp — removed 2026-08-30: there is no second path any more, so there is nothing to select.

Creates nothing provider-side; it records intent and returns the popup config:

```json
{"data": {"ok": true, "status": "pending_signup",
          "signup": {"app_id": "…", "config_id": "…", "solution_id": "…",
                     "feature_type": "whatsapp_business_app_onboarding",
                     "session_info_version": "3"}}}
```

`feature_type` is what turns the popup's first question into *"connect your existing WhatsApp Business App account?"*. Without it the salon is offered only a brand-new WABA — i.e. told to delete their app.

### `POST /whatsapp/onboarding/complete`

```json
{ "shop_id": "…", "code": "AQD…", "waba_id": "1234567890",
  "phone_number_id": "9876543210" }
```

Everything Meta's popup hands back to the browser.

Server-side, in this order — and the order is load-bearing:

1. Exchange the one-time `code` for the salon's business token, and **persist it before using it**. A crash after this point leaves a resumable row; losing the token leaves a WABA we can neither reach nor unsubscribe from.
2. `POST /{waba_id}/subscribed_apps`. Without it every send still succeeds while we receive no delivery status, no template verdicts and no opt-outs — broken in the one way nothing surfaces. No `/register` call follows it: a coexistence number is already registered, and Meta's own guidance is not to call it on one.
3. Read the number back (`is_on_biz_app`, `platform_type`) rather than trusting what the popup told the browser.
4. Inject the template catalogue.

```json
{"data": {"ok": true, "status": "online", "phone_number": "+39…",
          "coexistence": true, "templates": 1}}
```

Errors (409): `not_started`, `onboarding_limit_reached`, `code_exchange_failed`, `meta_error`.

### `GET /whatsapp/status/{shop_id}`

```json
{"data": {"status": "online", "source": "coexistence", "phone_number": "+39…",
          "display_name": "Salone Bellezza", "quality_rating": "GREEN",
          "messaging_limit": "TIER_1K", "coexistence": true,
          "daily_cap": 50, "configured_daily_cap": 50,
          "meta_tier": "TIER_1K", "meta_tier_daily": 1000,
          "recipient_cooldown_hours": 168,
          "offline_reason": null, "sent_today": 12, "sent_last_24h": 47,
          "sent_this_month": 87,
          "pricing": [{"kind": "marketing", "usd": 0.0691},
                      {"kind": "utility",   "usd": 0.0341},
                      {"kind": "service",   "usd": 0.0}],
          "templates": [{"template_key": "promo_v1", "status": "approved"}],
          "signup": {"…": "…"}}}
```

`status` is `not_started | pending_signup | verifying | online | offline | failed`. A salon can send only when `status == "online"` **and** the template is `approved`.

`pricing` is returned even for `not_started`: it is not a sender fact, and the webapp shows "what this would cost you" before onboarding begins.

**Two different ceilings, don't conflate them.**

| Field | Whose limit | What happens at it |
|---|---|---|
| `meta_tier_daily` | **Meta's** volume tier, per *rolling* 24h | hard: never crossed, by construction |
| `daily_cap` | the binding rate = `min(meta_tier_daily, configured_daily_cap)` | a campaign takes more days |

**`monthly_quota` was removed on 2026-08-24.** The plan allowance added on
2026-08-22 is gone from the payload and from both send gates: as a Meta Tech
Provider we have no credit line to share, so the salon's own card is on their
own WABA and Meta bills them directly. A Kairo-side ceiling recovered no cost
of ours and only suppressed the usage that makes the product stick. Meta's tier
is the real limit and we can read it.

**Counters are marketing-only; the tier window is not.** `sent_today`,
`sent_this_month` and `recently_contacted` (the 131049 cooldown) join
`whatsapp.templates` on `(shop_id, name)` and count `category = 'MARKETING'` —
an appointment reminder is not a promotion, must not appear in the owner's
campaign counter, and must not block next week's offer. `sent_last_24h`
deliberately counts **everything**, because Meta's tier is measured in
business-initiated conversations including utility; narrowing it would let a
salon send its marketing on top of its reminders and blow through the tier.
This split has no failure mode — nothing breaks when it is wrong, the numbers
are just silently incorrect — so two tests in `test_whatsapp.py` pin it.

`daily_cap` is deliberately the *effective* number, not the raw column — showing our 5000 when Meta allows 250 would promise throughput we refuse to deliver. The raw value is `configured_daily_cap`. See [Meta's limits are a floor](#metas-limits-are-a-floor-nothing-may-cross).

`sent_today` is the calendar-day counter the owner reads; `sent_last_24h` is the rolling one the Meta tier is checked against. They are not interchangeable.

`pricing` is an estimate from `services/messaging/whatsapp_pricing.py` — Meta's Italian per-category rate, and nothing else. `service` is genuinely **$0** now that Twilio's flat per-message fee is out of the path. No `credits` field: see [Billing](#billing).

### `POST /whatsapp/templates/ensure/{shop_id}`

Re-runs template injection — after a rejection, or after the catalogue (`services/messaging/whatsapp_templates.py`) gains an entry. Already-created templates are skipped: Meta blocks reusing a deleted template's name for 30 days.

**Gated on Kairo's own WABA.** A template is created by hand on Kairo's own WABA first (`scripts/kairo_waba.py push-templates`, then `scripts/kairo_waba.py templates` to watch it move to `approved`) — this endpoint only pushes a catalogue entry into a *customer's* WABA once that same template is `approved` there, checked live via `GET {waba_id}/message_templates?name=...`. Rejection is a Meta judgment on the content, identical on every WABA, so this avoids burning the same rejection (and its quality-rating hit) once per salon. Requires `META_KAIRO_WABA_ID`/`META_KAIRO_TOKEN`; unset means nothing propagates, not "propagate unchecked."

Returns `{"created": N, "failed": ["key", …], "not_ready": ["key", …]}`. `not_ready` is not approved on Kairo's WABA yet (or Kairo's WABA isn't configured) — expected right after adding a new catalogue entry, before you've pushed and gotten it approved on Kairo's own WABA. One rejected template never aborts the rest of the catalogue.

**Template names are `{locale}_{key}` — composed, never looked up (2026-09-01).** `it_promo_v1` today, `en_promo_v1` the day English copy exists. Meta scopes a name per-WABA and **cannot translate a template**, so a second language is a second template with its own name, its own submission and its own verdict on the same WABA; composing the name from the shop's locale is what lets the platform pick between them with no table in the middle. This replaced the flat `kairo_` prefix, which could only ever name one language's copy.

- `template_name(key, language)` takes the locale as a **required** argument — a default would let a caller that never considered locale silently address the Italian copy.
- The locale is `business_app_core.shops.language`, read at the moment of use (`wq.get_shop_language`) rather than snapshotted onto the sender, so switching a shop's locale switches which templates it addresses.
- `whatsapp_templates.SUPPORTED_LANGUAGES` is the locales whose **copy actually exists** (`("it",)`). `resolve_language` falls a shop on any other locale back to Italian: composing `es_promo_v1` for a Spanish shop would name a template on no WABA, and the symptom would be every template reading `missing` with nothing able to explain why.
- The **approval gate is per (language, key)**: `approved_on_kairo_waba` returns pairs, because `it_promo_v1` being approved says nothing about `en_promo_v1`. `retire-template` deletes every locale's copy of a key for the same reason.
- Still one name per (locale, key) across all shops: a per-shop name would make "is promo_v1 approved for this salon?" unanswerable without a lookup.
- **Sends never compose.** `enqueue_campaign` uses the `name` stored on the shop's own template row — the send must address exactly what was injected into that WABA.
- The status payload's descriptor carries `name` and the shop's resolved `language`; marketing-engine's `buildOfferSystem` writes the generated slot in that language, so the model follows the platform locale without a second source of truth.

Pinned by `test_template_names_compose_the_locale_with_the_key`, `test_a_shop_on_an_unsupported_locale_falls_back_to_copy_that_exists`, `test_ensure_templates_names_the_shops_own_locale`, and `test_push_templates_uses_the_name_the_gate_looks_for`.

**`push-templates` reconciles, it does not blind-create (2026-09-01).** It reads the WABA's templates once, then per catalogue entry: creates what is missing, `POST /{template_id}` on a body that differs, and skips what already matches. Before this it POSTed everything and `_api` exited on the first `name already exists`, so re-running after a copy change pushed nothing *and* never reached the entries added since the last run — with no symptom, because `kairo_waba.py templates` prints names and statuses, not bodies. An edit puts the template back to `PENDING` (Meta allows it only on `APPROVED`/`REJECTED`/`PAUSED`, ~10 edits/month), so `--dry-run` prints the plan first. A **category** change is never edited — UTILITY→MARKETING doubles the cost of the highest-volume messages we send, so it is reported and skipped for a human. Pinned by `test_push_templates_edits_a_stale_body_and_pushes_past_the_ones_that_exist`.

> The customer-side `ensure_templates` still skips any key the shop already has and compares no bodies: an edited template reaches Kairo's WABA only. Salons already carrying the old copy keep it — that is what `feedback_v2` exists for.

**The UTILITY pair was shortened on 2026-09-01** to `feedback_v2` = name + visit date + where to review, `reminder_v1` = name + appointment date and time. The salon name and the service list are gone from both: coexistence means the message arrives from the salon's own number under its own display name, so the body repeating it read like a mailshot. The **date stays** — it is the anchor to the customer's own transaction, and that anchor is what keeps Meta reading these as UTILITY instead of recategorising them as MARKETING at double the price. The review **link is no longer sent**: `render_variables` still accepts `link=` and ignores it, so the `link` field on the automation rule is inert until the webapp drops it. `feedback_v1` was deleted from the catalogue the same day — no sender had ever received it, so there was nothing to retire downstream.

**A `not_ready` key is retried by the hourly tick (2026-08-31).** Approval lands later, on *Kairo's* WABA, with no per-shop event attached — so without that retry a salon that onboarded while a template was pending could never send, permanently and silently. See [The hourly tick](#the-hourly-tick).

### Retiring a template

`scripts/kairo_waba.py retire-template --key promo_v1` deletes a template from Kairo's WABA **and from every customer WABA that has it**, then drops the rows so the catalogue entry can be re-pushed later.

- **Kairo's copy goes first**, deliberately. The reverse order leaves the propagation gate still answering "approved" if a later step fails, and the next tick re-pushes everything just deleted. Ours first closes the gate, so a partial run stops dead and re-running finishes it.
- **Not in the tick.** The tick could infer "gone from Kairo's WABA → delete downstream", but then one transient Graph read error wipes the template from every customer at once. A destructive fan-out gets an explicit operator behind it, and the command confirms before running.
- **Meta blocks reusing the name for 30 days.** This is a kill switch for a template that must stop going out, not an editing workflow. New copy means a new key (`promo_v2`), not delete-and-recreate.
- A per-shop delete failure keeps that row, so the next run retries it; "already gone" counts as success everywhere.

---

## Campaigns

### `POST /whatsapp/campaigns`

```json
{ "shop_id": "…", "campaign_key": "bulk_at_risk_2026-08-24", "template_key": "promo_v1",
  "recipients": [{"customer_id": "…", "variables": {"1": "Giulia", "2": "Salone X",
                                                     "3": "taglio e piega a 35€."}}] }
```

Up to **2000** recipients (was 500 — bulk sends to the whole consenting book are the point of the Touchpoint tile).

There is **no `body` field, and there cannot be one.** A business-initiated WhatsApp marketing message is an approved template plus variable values; the caller supplies the values, the template supplies everything else. The webapp's LLM copy generator writes `{{3}}`, not the message.

Returns immediately with the schedule; nothing is sent inline:

```json
{"data": {"ok": true, "queued": 380, "suppressed": 18, "already_sent": 2,
          "first_at": "2026-08-24T09:00:00+02:00",
          "last_at": "2026-08-31T19:47:00+02:00"}}
```

- `suppressed` — a row written with a `suppressed_reason` (`no_consent`, `no_phone`, `customer_not_found`). Refusals are recorded, never silent.
- `already_sent` — this `campaign_key` already reached that customer; the unique index made the retry a no-op. That guard earns its keep here, where "invia a 400 clienti" is exactly the button someone double-clicks.

`spread()` lays the campaign across the salon's opening hours (`WHATSAPP_SEND_START_HOUR`–`WHATSAPP_SEND_END_HOUR`, Europe/Rome), rolling onto **following days** once a day's `daily_cap` is used. A 400-recipient campaign against a 50/day sender is eight days of drip, and the owner is told so at enqueue time.

There is **no `over_daily_cap` rejection any more**: exceeding a day's allowance is a longer schedule, not an error. Keeping it would have made bulk impossible, and piling everything onto today just hands `send_due` hundreds of rows to defer by an hour, repeatedly, until nobody can read the queue.

Errors (409): `sender_not_online`, `unknown_template`, `template_pending`/`template_rejected`/…, `sender_has_no_allowance`.

**Only the code crosses the wire.** `enqueue_campaign` returns richer refusals than the route can carry — `HTTPException(detail=<code>)` flattens them to the bare string. The webapp translates it (`mapWaError`) and reads any numbers from `GET /whatsapp/status/{shop_id}` instead.

### `GET /whatsapp/campaigns/{shop_id}/{campaign_key}`

Counts per status plus `last_due_at`. A drip that runs for days is otherwise invisible between "inviata" and whatever arrives later. Polled by the bulk tile.

### `DELETE /whatsapp/campaigns/{shop_id}/{campaign_key}`

Cancels whatever hasn't gone out (`queued`/`sending` → `cancelled`). Already-sent rows are untouched history.

### `GET /whatsapp/messages/{shop_id}?customer_id=`

Everything one customer was part of: every `outbound_messages` row actually
sent to them **plus** the campaigns they were assigned to but never received
(the holdout arm). The webapp's Anagrafiche → "Campagne" tab renders this, and
it doubles as the GDPR subject-access artifact — "what did you send me, and
when".

The campaign `goal` and `personalization` come from `market_intel.campaigns`,
linked through `outbound_messages.campaign_key = campaign id` — the campaign_key
the webapp passes when it enqueues a campaign built by the AI flow. Campaigns
enqueued with a hand-made key (the older Touchpoint tile's `bulk_...`) have no
`market_intel` row and come back with a null goal.

Each row: `message_id` (null for holdout), `campaign_key`, `goal`,
`personalization`, `preview` (the rendered message), `delivery_status`,
`sent_at`, `suppressed_reason`, `arm` (`send`/`holdout`), `created_at`.

---

## `POST /whatsapp/webhook`

One app-level URL for every customer. Meta identifies the tenant only by `entry[].id` — the WABA id — so `whatsapp.senders.waba_id` is the sole route from a payload to a shop.

Always answers **200** on a genuine request. Meta retries on anything else and disables a webhook that keeps failing, which would silently cost every delivery status and every opt-out.

| `field` | Effect |
|---|---|
| `messages` → `statuses[]` | `sent`/`delivered`/`read`/`failed` written to `outbound_messages` by `wamid` |
| `messages` → `messages[]` | Inbound reply persisted to `whatsapp.inbound_messages` (migration 17) — campaign measurement ("replied within 72h", design §9) reads it; a reply is matched back by phone (`from_phone` == the sent message's `to_phone`) |
| `message_template_status_update` | Meta's verdict, applied to `(shop_id, name)` — **never by name alone**, since every salon's copy carries the same name |

Template verdicts arrive here within minutes instead of on the next hourly tick. The tick's poll survives as a **reconciler**: a missed webhook would otherwise leave a template `pending` forever, blocking every send for that shop and looking like nothing at all.

### Opt-out vs. frequency cap

Two error codes that look alike and must not behave alike:

| Code | Meaning | Action |
|---|---|---|
| `131050` | the recipient used Meta's native **"Stop promotions"** button | permanent — clears `business_app_core.customers.marketing_consent` |
| `131049` | Meta's per-user, **cross-brand** marketing cap ("healthy ecosystem engagement") | *not* an opt-out — requeued 24h later by `whatsapp_send` |

Collapsing them, as the Twilio version's single `63033`/`63050` bucket effectively did, permanently silences customers who did nothing wrong. Meta's native opt-out button is why this channel has the self-service opt-out that SMS gave up on 2026-08-15 — see [Decisions](../decisions.md).

---

## Billing

**Nothing here debits AI credits.** As a Meta Tech Provider (unlike a Solution Partner) Kairo has no credit line to share: the salon's own card sits on the salon's own WABA and Meta charges it directly. Debiting `send_credits()` on top would bill the same message twice.

`outbound_messages.price_usd` is our own send-time estimate and is never corrected — Meta reports no amount on send or on the webhook. `credits_charged` is unused on this channel.

The plan allowance still applies: it is a product limit, not cost recovery.

The SMS path is unchanged and still debits at 2× — there Kairo really does pay Twilio.

---

## Meta's limits are a floor nothing may cross

`services/messaging/meta_limits.py` is the single home of every Meta-imposed
ceiling, and the layering is deliberate:

```
Meta's limits    — platform facts. Never exceeded, by construction.
    ↓  min()
Kairo's limits   — commercial knobs: daily_cap, the plan allowance.
    ↓
the queue
```

**A commercial knob can only ever make us send less.** `effective_daily_cap()`
returns `min(Meta's tier, our daily_cap)`, so setting `senders.daily_cap` to
5000 on a Tier-250 sender buys nothing rather than getting the WABA
rate-limited and downgraded. `GET /whatsapp/status` returns that binding number
as `daily_cap`, with the raw column exposed separately as
`configured_daily_cap`.

**Everything fails closed.** An unrecognised tier is treated as the unverified
250, an unknown throughput as the slowest rate that exists. If Meta invents
`TIER_5K` we under-send until someone adds the row — the harmless direction.

| Meta limit | Where it's enforced | How |
|---|---|---|
| Volume tier (business-initiated conversations / **rolling 24h**) | `enqueue_campaign`, `send_due` | `effective_daily_cap()` against `sent_last_24h()` |
| Throughput (mps, per number) | `send_due` | global pacer clamped to `MAX_SENDS_PER_MINUTE` |
| Graph API app-level rate | `send_due` | `Pacer`, `WHATSAPP_SENDS_PER_MINUTE` |
| Per-user cross-brand marketing cap (131049) | `enqueue_campaign` + `send_due` | `WHATSAPP_RECIPIENT_COOLDOWN_HOURS` (default 168) |
| Marketing to +1 recipients (paused since 2025-04-01) | `enqueue_campaign` | `marketing_allowed()` |
| Tech Provider onboarding, 10 (or 200) per rolling 7 days | `complete()` | `onboarded_last_7_days()`, checked *before* spending the popup's single-use code |

### The rolling window is not the calendar day

Meta measures the tier over a **rolling 24 hours**. `sent_today` resets at
midnight, so using it for the tier check would hand a sender sitting at its
ceiling at 23:00 a second full allowance ninety minutes later — nearly two
tiers' worth of traffic inside one of Meta's windows. `sent_last_24h()` is the
Meta check; `sent_today()` survives only for the owner-facing counter, where
"quanti ne ho mandati oggi" is what the number means.

### Why there is no per-number pacer

`MAX_SENDS_PER_MINUTE` is `COEXISTENCE_MPS × 60` = 1200. Below that, no single
number can be over-driven however the claimed batch happens to fall across
shops, because 20 mps is the slowest per-number throughput Meta grants. The
invariant is enforced once, by clamping the global rate, instead of with a
second mechanism that would be dead machinery at any sane configuration.
`send_due` clamps rather than trusts `WHATSAPP_SENDS_PER_MINUTE`, so raising
the env var to something absurd cannot silently remove the ceiling.

### The cooldown is the by-design half of 131049

Reacting to `131049` costs the send and a quality-rating hit; not sending
costs nothing. The cooldown (7 days by default) means we stay under Meta's
undisclosed per-user ceiling instead of discovering it. It is checked at
enqueue *and* re-checked at send, for the same reason consent is: a row on a
multi-day drip can be overtaken by another campaign. It counts `sent_at`, so a
message that never left never starts a cooldown.

New `suppressed_reason` values: `recently_contacted`,
`marketing_blocked_destination`.

---

## The hourly tick

`POST /messaging/tick` ([Number Provisioning](number-provisioning.md)) has two WhatsApp stages, each independently wrapped so one failure can't suppress the others:

- `whatsapp` — reconciles sender and template state against Meta, for verdicts the webhook didn't deliver, and carries the **only retry of the propagation gate**: live senders missing part of the catalogue are re-pushed once Kairo's own copy turns `approved`. Kairo's WABA is asked once per run, not once per shop — the answer is identical for everyone. An empty gate (unconfigured, or a Graph error) skips the stage entirely rather than pushing on a guess. Counts add `propagated` and `approved_on_kairo`.
- `whatsapp_sends` — claims what is due and sends it. Counts: `sent`, `suppressed` (`no_consent`, `opted_out`, `recently_contacted`), `failed`, `deferred` (over daily cap, retried in an hour), `rate_capped` (Meta 131049, retried in 24h), `requeued` (claimed but never sent, recovered from a crashed tick).

---

## Out of scope

- Inbound replies are **persisted** (migration 17) and read by campaign measurement, but nothing answers them. A reply opens Meta's 24h session window, inside which free-form messages *are* allowed — the obvious next phase, and the only path to genuinely free-form personalised copy.
- Contact / chat-history sync (`POST /{phone_number_id}/smb_app_data`). One-shot and irreversible per onboarding, and there is nowhere to put the data yet.
- One template in the catalogue (`promo_v1`). The machinery takes N; the LLM template-picker that would make N worth having isn't built.
