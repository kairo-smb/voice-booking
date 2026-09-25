# WhatsApp API

`booking_engine/api/routes/whatsapp.py`, mounted at `/api/v1`. Onboards a salon onto WhatsApp with its own WABA, injects Kairo's approved templates into it, and queues personalised marketing that drips out across the day — or across a week, for a bulk campaign. It also serves the two-way Inbox: reading a customer's thread and answering it free-form inside Meta's 24h window.

**Read [Providers → WhatsApp](../providers.md#whatsapp-meta-cloud-api-tech-provider) first** if you're new to this: the constraints (approved templates only, per-recipient marketing caps, coexistence) explain why these endpoints exist in this shape.

> **This channel does not go through Twilio.** Twilio cannot register a WABA it did not create ([error 63103](https://www.twilio.com/docs/api/errors/63103) — it must attach the WABA to *Twilio's* Meta credit line, and Meta won't release an existing payment method), and its migration path deletes the salon's WhatsApp Business App. Kairo is a Meta **Tech Provider** and talks to `graph.facebook.com` directly. Twilio still owns voice and SMS.

## Auth

| Routes | Scheme |
|---|---|
| `/whatsapp/onboarding/*`, `/whatsapp/status/*`, `/whatsapp/templates/*`, `/whatsapp/campaigns*`, `/whatsapp/receipts`, `/whatsapp/messages/*`, `/whatsapp/threads/*`, `/whatsapp/reply` | Control-plane bearer (`CONTROL_PLANE_SECRET`) — the webapp is the only caller |
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

`solution_id` is **normally empty, and that is the correct state.** A partner solution is a joint arrangement with a Solution Partner (BSP); Kairo is an independent Tech Provider — each salon attaches its own payment method — so there is nothing to name. The webapp passes it as `extras.setup.solutionID` when set and omits the field when not, since Meta rejects a blank one.

`complete` records `token_expires_at` from the code exchange's `expires_in`, returned by `GET /whatsapp/status`. Our Login Configuration mints 60-day tokens and nothing renews them: when the date passes, that sender is dead until the salon redoes Embedded Signup.

**Renewing: `reconnect: true` on both onboarding calls.** Redoing the popup *is* the renewal — there is no refresh endpoint at Meta. Both `start` and `complete` take an optional `reconnect` (default `false`), and it changes three things:

- `start` returns the signup config **without touching the row**. The sender stays `online` on its old token and keeps sending for as long as the owner leaves the popup open; marking it `pending_signup` would take a working sender off the air to fix a problem that hasn't happened yet.
- `complete` skips the "already online, nothing to do" early return — the exit that makes an ordinary double-submit idempotent, and the one that would otherwise report success while leaving the expiring token in place.
- `complete` skips Meta's **new-customer onboarding cap** (10 or 200 per rolling 7 days). That cap counts new customers; a salon renewing its own token is not one, and counting it would let a busy onboarding week block an existing salon from renewing — its sender then dies at day 60 over someone else's signup.

The webapp drives this from `token_expires_at`: a cockpit banner (`WhatsAppTokenBanner`) and the WhatsApp panel both nudge inside the last 7 days, and the panel's button opens the same Embedded Signup popup with `reconnect: true`.

**It is no longer pull-only (2026-09-20).** The banner required the owner to open the app inside the one week that matters — and never showed at all while their session was in employee view, since every WhatsApp route is owner-only. The hourly tick now also emails: `list_senders_needing_token_reminder` finds online senders inside `RENEW_WINDOW_DAYS` (7, the same constant the banner uses — two answers to "is it urgent yet?" would drift unnoticed until a salon went dark) and POSTs to the webapp's `/whatsapp/token-expiring`, which owns the mailbox, the owner's address, the shop's locale and the already-written `whatsappExpiringEmail` template. Same `MARKET_INTEL_SECRET` bearer as the credit charge; a second secret for the same hop would be a second thing to rotate.

`token_reminder_sent_at` (migration 23) records the **attempt**, not the delivery, and `REMINDER_COOLDOWN_HOURS` (72) caps it at roughly three mails across the window. A salon with no owner mailbox, or a Resend refusal, is marked anyway: retrying hourly against a fact about the shop is noise, and the banner still covers that salon. Senders already past the date are included — that one is dead and the reconnect still fixes it.

Built because the expiry stopped being an inference: the first real onboarding came back with `token_expires_at` exactly 60 days out, so Meta does return `expires_in` and the 60-day configuration is doing what its name says.

### `POST /whatsapp/onboarding/complete`

```json
{ "shop_id": "…", "code": "AQD…", "redirect_uri": "https://app.kairo.…/" }
```

**`redirect_uri` is required in practice (2026-09-20).** Meta binds the code to the origin the OAuth dialog was opened with and refuses an exchange that does not repeat it byte for byte — the failure is a flat `code_exchange_failed`. It comes from the browser, not from config here: this service knows none of the webapp's origins, and the webapp's own proxy sees Amplify's internal `https://localhost:3000`. Optional in the schema only because Meta's JS SDK opens the dialog without one, and this flow does not use the SDK.

**Only `code` is required otherwise (2026-09-19).** `waba_id` and `phone_number_id` are accepted and optional, and in practice never sent: Meta posts them to the browser on a `WA_EMBEDDED_SIGNUP` message that it emits **only through its JS SDK**, and this flow cannot use the SDK — it routes `FB.login` through FedCM, dropping `config_id`, so the popup that opens is a plain OIDC login Meta then refuses with *"this app needs at least one supported permission"*. The webapp builds the dialog URL by hand instead, which gets the real coexistence flow and the `code`, and nothing else.

So both ids are read back from the exchanged token server-side, which is the better source regardless — Meta reporting what it granted, rather than the browser relaying what it was shown:

- **`waba_id`** from `GET /debug_token?input_token=<business token>` (authenticated with the *app* token), taking the union of `granular_scopes[].target_ids` for `whatsapp_business_management` and `whatsapp_business_messaging`.
- **`phone_number_id`** from `GET /{waba_id}/phone_numbers`.

Neither zero nor several can be resolved by guessing — picking one would attach the salon's sender to someone else's WhatsApp account, unrecoverably and with nothing downstream disagreeing — so they return `waba_ambiguous` / `phone_ambiguous` and write no sender.

**`waba_ambiguous` is a question, and it is answerable (2026-09-20).** An owner who administers several WABAs is the ordinary case, not an error: only they know which is the salon's. The refusal therefore carries `wabas: [{id, name, phone_numbers}]` — a 15-digit id is not something a hairdresser can pick from, and the **name alone often isn't either**: a WABA is frequently named after a company registration the owner has never read, so two of them side by side say nothing about which is the salon's. The number is what they know by heart. Both are best effort (`GET /{waba_id}?fields=name`, `GET /{waba_id}/phone_numbers`) and degrade to the id rather than failing the onboarding. The route returns this as the whole `detail` object rather than the bare slug every other refusal flattens to.

The answer is a **second `complete` with `waba_id` and no `code`**. The code is single-use and was spent asking, so the service resumes from the token, which is now persisted **before** the lookups rather than after them. That reordering is what makes this work at all, and it also closes a smaller hole: the lookups used the token before anything had written it down, so a crash between them lost a credential that cannot be minted again without another popup. The resume skips the Tech Provider onboarding cap too — the popup already happened and was already counted; re-checking would strand a salon holding a token it cannot name a WABA for. `token_expires_at` is left alone on a resume (there is no `expires_in` to report), so an expiring token cannot be silently promoted to a non-expiring one.

Server-side, in this order — and the order is load-bearing:

1. Exchange the one-time `code` for the salon's business token, and **persist it before using it** — including before the id lookups above, which need it. A crash past this point leaves a resumable row; losing the token leaves a WABA we can neither reach nor unsubscribe from, and Meta will not reissue it without another popup.
2. `POST /{waba_id}/subscribed_apps`. Without it every send still succeeds while we receive no delivery status, no template verdicts and no opt-outs — broken in the one way nothing surfaces. No `/register` call follows it: a coexistence number is already registered, and Meta's own guidance is not to call it on one.
3. Read the number back (`is_on_biz_app`, `platform_type`) rather than trusting what the popup told the browser.
4. **Not** the template catalogue — it is pushed by a background task *after* this responds (2026-09-20). One Graph round trip per catalogue entry took the whole call past the gateway timeout in front of the webapp, so the owner got a 504 for a sender that was already online. Templates were always the last step precisely because they are the only safely re-runnable one, which is what lets them move off the request path: if the background push dies, the hourly sweep's `list_senders_needing_templates` picks the shop up, and `POST /whatsapp/templates/ensure` is the manual retry.

```json
{"data": {"ok": true, "status": "online", "phone_number": "+39…",
          "coexistence": true}}
```

Errors (409): `not_started`, `onboarding_limit_reached`, `code_exchange_failed`, `meta_error`.

### `DELETE /whatsapp/onboarding/{shop_id}`

The owner closed Meta's popup without finishing (or the code exchange failed on `complete`). `start` persisted a `pending_signup` row to record intent; this is the other end of that contract — it drops the row so the next status read is `not_started` and the panel offers the connect button again instead of a stuck "Meta is verifying" box.

Idempotent and narrow: it deletes only a `pending_signup` row (an `online` or `failed` sender is real state and is never touched), and deleting nothing still returns `{"data": {"ok": true}}`.

### `DELETE /whatsapp/sender/{shop_id}?requested_by=`

The owner disconnects their WABA, in any state (added 2026-09-25). Unsubscribes our app from the WABA's webhooks (best effort: a revoked or expired token is a common reason to disconnect, so Meta refusing is logged, not fatal), then in one statement cancels the shop's `queued` messages, deletes its `whatsapp.templates` rows (they mirror *that* WABA's approvals — a reconnect to another WABA would otherwise skip pushing templates to it) and deletes the sender. Outbound history, inbound threads and automation rules stay. Audited as `sender.disconnect`. Idempotent: `{"data": {"ok": true, "cancelled": <n>}}`.

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

`pending_signup` is reported as `not_started` once the row has been untouched for 15 minutes (`ABANDONED_AFTER`): the webapp aborts explicitly when its popup closes, but an owner can walk away from or close the whole tab with nothing firing, and a permanent "verifying" box with no way back is worse than forgetting the attempt.

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
campaign counter, and must not block next week's offer (the review request
does, because Meta reclassified it as marketing). `sent_last_24h`
deliberately counts **everything**, because Meta's tier is measured in
business-initiated conversations including utility; narrowing it would let a
salon send its marketing on top of its reminders and blow through the tier.
This split has no failure mode — nothing breaks when it is wrong, the numbers
are just silently incorrect — so two tests in `test_whatsapp.py` pin it.

`daily_cap` is deliberately the *effective* number, not the raw column — showing our 5000 when Meta allows 250 would promise throughput we refuse to deliver. The raw value is `configured_daily_cap`. See [Meta's limits are a floor](#metas-limits-are-a-floor-nothing-may-cross).

`sent_today` is the calendar-day counter the owner reads; `sent_last_24h` is the rolling one the Meta tier is checked against. They are not interchangeable.

`pricing` is an estimate from `services/messaging/whatsapp_pricing.py` — Meta's Italian per-category rate, and nothing else. `service` is genuinely **$0** now that Twilio's flat per-message fee is out of the path. No `credits` field: see [Billing](#billing).

### `POST /whatsapp/templates/ensure/{shop_id}`

Re-runs template injection — after a rejection, after the catalogue (`services/messaging/whatsapp_templates.py`) gains an entry, **or after an existing body is edited**.

Three outcomes per key: **create** what the WABA doesn't have, **edit in place** what it has under an outdated body, leave the rest alone (resubmitting an identical body burns Meta's edit quota and puts an approved template back into review for nothing).

**A template under review is never touched (2026-09-20).** The sweep runs hourly, and a row whose body no longer matches the catalogue is an edit. Meta refuses to edit a template it is still ruling on, so without this the sweep would fail against the same template every hour until the verdict landed — and on the reading where the edit *does* land, it restarts the review, pushing approval further out exactly as often as we asked for it. Skipped while `pending`/`received`; the drift is not dropped, because the shop keeps matching `list_senders_needing_templates` and the edit happens on the first sweep after Meta has ruled. A row whose hash already matches was never re-pushed to begin with.

**A template Meta already holds is adopted, not refused (2026-09-20).** Found on the first real onboarding: all six were created on the WABA, and every later push failed with a bare `100: Invalid parameter`. The cause is that **Meta re-categorises on review** — a UTILITY body it reads as promotional comes back MARKETING — so resubmitting our own category is refused for as long as the name exists. Nothing was recorded, so the sweep retried the identical create every hour while the panel reported the feature as waiting for Meta, which was true of nothing.

A create that fails for a key with no row now reads the template back by name and adopts it: **Meta's** id, status, category and body hash, not ours. Meta's category because storing our guess is exactly what makes the next create repeat the refusal; Meta's body hash because a template adopted with stale copy should read as drifted and be fixed by the edit path, rather than the row asserting an alignment nobody checked. A name Meta does not have is still a failure — adoption must not turn a genuine rejection into a silent success.

Related: `MetaError` now leads with `error_user_msg`. Graph's `message` is a generic label shared by a whole family of unrelated refusals, and dropping the field that names the cause is what made this opaque.

**Gated on Kairo's own WABA — status *and* body (2026-09-02).** A template is pushed to Kairo's own WABA first (`scripts/kairo_waba.py push-templates`, then `scripts/kairo_waba.py templates` to watch it move to `approved`); this endpoint only propagates a template into a *customer's* WABA once Meta has approved **that exact body** there, read live from `GET {waba_id}/message_templates?name=…&fields=…,components`. The gate now covers the catalogue **and** the document templates (2026-09-23): `purchase_receipt_1` is fetched on Kairo's WABA by its verbatim preset name — never `{locale}_{key}` — and needs the same approved-status-plus-body-match before the loop below will push it. Status alone answers a question about a *name*: change the copy here and deploy before `push-templates` runs, and a name-only gate reports "approved" for last month's text — then hands the drift path below a green light to push unreviewed copy to every salon. Rejection is a Meta judgment on content, identical on every WABA, so this avoids burning the same rejection (and its quality-rating hit) once per salon. Requires `META_KAIRO_WABA_ID`/`META_KAIRO_TOKEN`; unset means nothing propagates, not "propagate unchecked."

**The document templates ride the same loop (2026-09-23).** After the catalogue, `DOCUMENT_TEMPLATES` (the receipt) runs the identical gate/skip/adopt sequence with a document-specific create/edit: a missing row is created with `create_document_template` — the `HEADER`/`DOCUMENT` component requires `META_RECEIPT_SAMPLE_URL` (a missing URL is reported as `not_ready` rather than submitted to a guaranteed rejection, and never aborts the catalogue results above) — a stale body is edited body-only (the header is untouched, since Meta never hands the `header_handle` back), a create refusal adopts Meta's copy by name, and a row Meta is still reviewing is skipped. Counts share `created`/`edited` with the catalogue.

Returns `{"created": N, "edited": N, "failed": ["key", …], "not_ready": ["key", …]}`. `not_ready` is not approved on Kairo's WABA yet (or the approved copy there isn't the current body, or Kairo's WABA isn't configured) — expected right after changing copy, before you've pushed it and had it approved on Kairo's own WABA. One rejected template never aborts the rest of the catalogue.

**Copy drift is tracked by `whatsapp.templates.body_hash` (2026-09-02).** It records *which version* of the copy each WABA holds — `status = 'approved'` only ever meant "a template with this name passed review". Before it, an existing row was an unconditional skip: re-voicing a template reached Kairo's WABA and stopped there, and every connected salon kept sending the old text with nothing anywhere disagreeing. An edit keeps the name (`POST /{template_id}`), so the salon keeps sending the previously approved copy while Meta re-reviews the new one — delete-and-recreate would take them off the air for Meta's 30-day name lock. `NULL` means "unknown version" and is treated as stale, which is what every pre-migration row is.

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

**It also pushes the DOCUMENT templates (2026-09-02).** `whatsapp_templates.DOCUMENT_TEMPLATES` — `purchase_receipt_1` today — reconciles in the same run, in its own loop: the payload is an attachment, so it carries a `HEADER`/`DOCUMENT` component and needs `META_RECEIPT_SAMPLE_URL` (a publicly hosted sample PDF) **to create**, though not to edit, since an edit resubmits only the body. Its name is Meta's pre-built preset used **verbatim**, not `{locale}_{key}`. Only the body text is compared: Meta hands back an opaque `header_handle`, never the URL we submitted, so comparing headers would report drift on every run. Missing sample URL is reported and counted as not-pushed rather than submitted to certain rejection. Pinned by `test_push_templates_creates_the_document_template_with_its_sample`.

> **The full copy-change loop:** edit the body in `whatsapp_templates.py` → `push-templates` (creates/edits on Kairo's WABA, back to `PENDING`) → Meta approves it there → the hourly tick's gate now matches on body, so `ensure_templates` edits every connected salon's copy in place and marks it `pending` → each salon's verdict arrives by `message_template_status_update` webhook → the webapp's tiles, which all gate on `status === 'approved'` from `GET /whatsapp/status`, re-enable themselves. Nothing after the first step is manual; `POST /whatsapp/templates/ensure/{shop_id}` only exists to skip the wait.

**The marketing trio was re-voiced on 2026-09-02** (never live, so bodies were rewritten in place with keys kept): `promo_v1`/`winback_v1`/`rebook_v1` are now signed by the stylist of the customer's last visit (`{{2}}`, the shop moving to `{{3}}`) and close with the shared soft CTA «Se ti va, scrivimi pure.» `promo_v1`'s generated slot (now `{{4}}`) is a gentle check-in *observation* about the last visit, not an offer; `rebook_v1` never mentions money (owner rule, pinned by a test). `receipt_v1` (UTILITY, itemised visit + total) joined for a not-yet-built receipt feature — inert until a sender references it, but it enters the push list like any catalogue entry.

**The automation pair was shortened on 2026-09-01, and Meta split its categories (2026-09-23).** `feedback_v2` = name + visit date + where to review, `reminder_v6` = name + appointment date and time + salon. Meta reclassified the review request as **MARKETING** — asking a customer for a public review is promotion by its definition — while `reminder_v6` stays **UTILITY**. The split is deliberate and load-bearing: the feedback rule is now consent-gated, cooldown-suppressed and paused on YELLOW/RED quality, and it counts in the owner's marketing counters; the reminder rule is none of those, because an appointment reminder that last week's offer could delay or suppress would be a defect, not a compliance win. `feedback_v2` carries the module's one named exemption from the "MARKETING generates a slot" invariant (`MARKETING_WITHOUT_GENERATED_SLOT` in `whatsapp_templates.py`): every variable is still a fact, so there is nothing for a model to write and nothing that could hallucinate. The service list is gone from both bodies, and the salon name from the review request — coexistence means that one arrives from the salon's own number under its own display name, so repeating it read like a mailshot. The reminder keeps the salon name: it may reach a number the customer never saved, and without it nothing in the message says who is waiting for them. `reminder_v6` is named for the copy actually approved on Kairo's WABA — **the catalogue key tracks Meta's template name, never the reverse**, because Meta locks an approved body and a key that drifts addresses a template that does not exist. The **visit date stays** — it is the anchor to the customer's own transaction. The review **link is no longer sent**: `render_variables` still accepts `link=` and ignores it, so the `link` field on the automation rule is inert until the webapp drops it. `feedback_v1` was deleted from the catalogue the same day — no sender had ever received it, so there was nothing to retire downstream.

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
  "recipients": [{"customer_id": "…", "variables": {"1": "Giulia", "2": "Chiara",
        "3": "Salone X", "4": "sono passate tre settimane dal tuo colore, com'è la ricrescita?"}}] }
```
`{{2}}` is the stylist of the customer's last visit, `{{3}}` the shop name, and `{{4}}` the observation the LLM generator writes (see below).

Up to **2000** recipients (was 500 — bulk sends to the whole consenting book are the point of the Touchpoint tile).

There is **no `body` field, and there cannot be one.** A business-initiated WhatsApp marketing message is an approved template plus variable values; the caller supplies the values, the template supplies everything else. The webapp's LLM copy generator writes the template's generated slot, not the message — for `promo_v1` (example above) that is `{{4}}`, the observation.

Returns immediately with the schedule; nothing is sent inline:

```json
{"data": {"ok": true, "queued": 380, "suppressed": 18, "already_sent": 2,
          "first_at": "2026-08-24T09:00:00+02:00",
          "last_at": "2026-08-31T19:47:00+02:00"}}
```

- `suppressed` — a row written with a `suppressed_reason` (`no_consent`, `no_phone`, `customer_not_found`). Refusals are recorded, never silent.
- `already_sent` — this `campaign_key` already reached that customer; the unique index made the retry a no-op. That guard earns its keep here, where "invia a 400 clienti" is exactly the button someone double-clicks.

**Exception: `source: "offer"`** (the webapp's single win-back modal) is sent at enqueue time — `send_due` scoped to that shop + `campaign_key`, same consent/cooldown/cap checks — and the response carries `sent_now`. One click for one customer shouldn't wait for the next scheduled drain. It shares the drain lock: if a drain is running, the row stays queued for the next one and `sent_now` is 0. Out of opening hours `spread` has already put the row on tomorrow's first slot, so it still waits.

`spread()` lays the campaign across the salon's opening hours (`WHATSAPP_SEND_START_HOUR`–`WHATSAPP_SEND_END_HOUR`, Europe/Rome), rolling onto **following days** once a day's `daily_cap` is used. A 400-recipient campaign against a 50/day sender is eight days of drip, and the owner is told so at enqueue time.

There is **no `over_daily_cap` rejection any more**: exceeding a day's allowance is a longer schedule, not an error. Keeping it would have made bulk impossible, and piling everything onto today just hands `send_due` hundreds of rows to defer by an hour, repeatedly, until nobody can read the queue.

Errors (409): `sender_not_online`, `unknown_template`, `template_pending`/`template_rejected`/…, `sender_has_no_allowance`.

**Only the code crosses the wire.** `enqueue_campaign` returns richer refusals than the route can carry — `HTTPException(detail=<code>)` flattens them to the bare string. The webapp translates it (`mapWaError`) and reads any numbers from `GET /whatsapp/status/{shop_id}` instead.

### `GET /whatsapp/campaigns/{shop_id}`

Every campaign of the shop with rows still `queued`/`sending`, with the same counts plus `next_due_at`/`last_due_at`. The webapp's bulk tile renders it on load so a scheduled drip survives a reload, and a campaign drops out once its last row has left.

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
`sent_at`, `suppressed_reason`, `error_code` (Meta's error on a `failed` row),
`scheduled_at` (when a `queued` row will leave; null on holdout/inbound),
`arm` (`send`/`holdout`), `created_at`.

---

## Threads (the two-way Inbox)

A "thread" is not a table: it is every message to and from one phone number,
collapsed per phone at read time out of `whatsapp.inbound_messages` and
`whatsapp.outbound_messages` (`booking_engine/db/whatsapp_thread_queries.py`).

**Phone numbers are matched with the leading `+` stripped on both sides**
(`ltrim(phone,'+')`, in the SQL and nowhere else). Meta reports `from` as bare
E.164 while the webapp holds whatever the customer record says, usually with
the plus — either spelling addresses the same thread, and the routes forward
the caller's spelling verbatim rather than normalising a second time.

### The 24h service window

Meta permits **free-form** (non-template) messages only within 24 hours of the
customer's *last inbound message*. The window resets on every customer message;
our own sends do not extend it, and neither does an echo from the owner's own
WhatsApp Business App (`outbound_messages.origin = 'phone'`) — that is not a
customer message, which is why the window is computed from `inbound_messages`
alone. At exactly 24h it is **closed** (`now < expires`, strictly).

Outside it, a send fails at Meta with **`131047`**, which reaches the owner as
an opaque provider error they cannot act on. So `POST /whatsapp/reply` checks
the window **before** the Graph call, never after: a closed window returns a
named refusal and makes no Graph request at all. Reaching a customer after the
window means a template (a campaign), not a reply.

### `GET /whatsapp/threads/{shop_id}`

One row per phone the shop has heard from, newest first. Keyed on **inbound**,
so a customer who was only ever messaged by a campaign and never replied does
not appear — there is no window on that phone and nothing there to answer.

One query, not one per thread: the session's routed intent is derived inside
the list SQL, because this is the Inbox's first screen.

Each row: `phone`, `customer_id`, `last_inbound`, `last_message` (a voice note
reads as its transcript), `message_type`, `unread`, `last_outbound`,
`window_expires_at`, `intent` (the **session's** verdict, not the last
message's), `escalated` (the newest WhatsApp session's `outcome = 'escalated'`
— see below), plus two fields computed per row from the pure helpers:

| field | meaning |
|---|---|
| `window_open` | is a free-form reply legal right now |
| `needs_attention` | belongs in "Da gestire": the session was escalated, or its intent is unrouted / outside `wa_routing.WHITELIST` — fails toward the human |
| `agent_active` | is the booking agent answering this thread |
| `agent_reason` | why it is not, when it is not — `null` while it is |

`agent_active`/`agent_reason` come from `wa_agent.agent_status`, which is
`may_speak` with one verdict renamed: the **same rule the agent itself obeys**,
so the Inbox cannot claim the agent is handling a thread it has stood down on.
The row supplies `agent_enabled` (LEFT JOIN on `shop_config` — no row is no
opt-in), `escalated`, `outcome_reason` and `human_replied_at`; that last one is
scoped to the newest session's `started_at`, because a reply the owner sent last
month must not read as them holding today's conversation. The webapp renders one
sentence per reason (`src/lib/whatsapp/agent.ts`), never a generic "the
assistant is off" — see the refusal table above for why.

`turn_limit` never appears here: it is the refusal that marks the session
escalated, so by read time it presents as `escalated`, which is the true thing
to say. Four reasons reach the owner, not five.

### `POST /whatsapp/threads/{shop_id}/{phone}/takeover`

"Rispondo io": the owner takes one conversation off the agent. Marks the
session `outcome = 'escalated'` with `outcome_reason = 'human_took_over'`
(`wa_agent.TAKEOVER_REASON`), opening a session first if the agent has not
spoken on the thread yet — with no row there would be nothing to mark, and the
next inbound message would find a clean slate and answer anyway.

The distinct `outcome_reason` is what lets the read side tell the owner pressing
the button apart from the agent giving up. Both are the same `escalated` row;
"hai preso tu questa conversazione" and "l'assistente te l'ha passata" are not
the same sentence.

**There is no endpoint to hand a thread back**, deliberately. The agent resumes
by itself on the customer's next conversation (a new session, past
`wa_routing.SESSION_GAP`), which is what "resume" can honestly mean — and
un-escalating *this* session would put the agent back into a thread a person is
in the middle of. It could not work fully in any case: `may_speak` also silences
on `human_replied_at`, derived from an outbound row that cannot be unsent, so a
resume button would clear the escalation, change nothing visible, and read as
broken.

`escalated` is joined from the newest `voice_agent.calls` row for that phone
with `channel = 'whatsapp'`. `needs_attention` has always read the field;
nothing wrote it until the booking agent existed. Without it an escalated
thread whose intent is still `booking` reads as handled — inside the allowlist,
therefore not the owner's problem — which is precisely the thread that most
needs them.

### The booking agent, and every reason it stays quiet

On a `'route'` decision the inbound worker hands the thread to
`services/messaging/wa_agent.py`. **Three writers share one thread and only one
is ours** — the customer, the owner (webapp *and* the WhatsApp Business App on
their own phone, since every sender is coexistence), and the agent. So every
rule in that module is about the agent standing down.

`may_speak(thread) -> (bool, reason)` is pure — a dict in, a verdict out, no
clock and no database, the same shape as `wa_routing.decide` and
`number_health.decide_health`. It is an **allowlist of conditions**, so an
unknown thread state defaults to silence rather than to speech; a blank dict
falls out at the first rule. Every refusal carries a distinct reason, because
"the agent is quiet and nobody can say why" is the state that makes an owner
switch it off:

| reason | meaning |
|---|---|
| `not_opted_in` | `voice_agent.shop_config.whatsapp_agent_enabled` is false. **The default** — a salon that has not asked for a robot must never get one |
| `intent_not_whitelisted` | the session's intent is outside `wa_routing.WHITELIST`. Opted in is not enough; a complaint is a person's |
| `escalated` | the session was handed to a human and stays handed over — the *next* message does not run a turn either |
| `human_took_over` | the owner replied, from the webapp (`kairo`) or their phone (`phone`). Not marked escalated: they are already handling it |
| `turn_limit` | `MAX_SESSION_TURNS` (12) reached **in this session**. A booking is four or five exchanges; twelve means the conversation is not going where the agent thinks it is, and the honest move is a person. The one refusal here that is escalated, because it is something happening rather than a thread that was never the agent's |

**The debounce is a sleep plus a re-read, not a per-thread timer.**
`DEBOUNCE_SECONDS = 2.0`: people send "ciao" / "volevo prenotare" / "per
sabato" as three messages, and answering each is three replies to one thought
and three billed turns. Every task sleeps, then asks the database one question —
"is my message still the newest on this thread?" — whose answer is the same for
whoever asks it. The last message wins because it is last, not because anyone
coordinated. A timer would need a mutable per-thread registry plus cancellation,
and two Fly machines would each keep their own copy, so it would not actually
debounce across them. It **batches rather than drops**: the surviving task reads
the whole session back out of the database, so all three messages reach the
agent — only the two earlier *turns* are dropped.

**An escalation sends nothing.** `text` is empty whenever `escalate` is true,
and the empty basket (402 from the gateway) arrives as `reason='no_credit'` and
takes the same path: silence, and the thread lands in the owner's queue via
`outcome = 'escalated'`.

### `GET /whatsapp/threads/{shop_id}/{phone}`

The timeline: inbound and outbound merged, oldest first. Each message carries
`direction` (`in`/`out`), `at`, `text`, `message_type`, `origin` on outbound
(`kairo` = we sent it, `phone` = the owner answered from the Business App),
`status`, `intent`, `read_at`.

**Reading the thread is what marks it read** — the two are the same act, so
there is no separate mark-read endpoint for the webapp to forget to call. The
update is idempotent (`read_at IS NULL`), and an unknown phone returns an empty
`messages` list rather than a 404: "this customer has never written" is an
answer, not an error.

### `POST /whatsapp/reply`

```json
{ "shop_id": "…", "phone": "+393331112222", "body": "Ciao, a domani!" }
```

Sends one free-form text now (synchronous, like receipts — not the campaign
queue) and records it in `outbound_messages` with `origin = 'kairo'`,
`template_name` and `campaign_key` NULL. The campaign idempotency index is
partial on both of those, so it does not apply here and the owner may
legitimately send the same words twice.

Returns `{"data": {"sent": true, "provider_sid": "wamid…"}}`. Refusals come
back 200 with `{"ok": false, "error": …}`:

| error | when |
|---|---|
| `empty_body` | blank or whitespace-only — Graph rejects it, and refusing locally names the problem |
| `sender_offline` | no sender row, or its status isn't `online` |
| `session_window_closed` | the 24h window has passed (or the customer never wrote) — **no Graph call is made** |

If Meta accepts the send but recording it fails, the response is
`{"sent": true, "provider_sid": …, "recorded": false}` and the loss is logged
(`whatsapp.reply_not_recorded`). The customer's phone already has the message;
reporting failure would have the owner send it a second time.

**No credit debit**, like every other send on this channel — see
[Billing](#billing).

---

## Receipts (Smart Receipt)

### `POST /whatsapp/receipts`

Send one receipt PDF as a WhatsApp document, right after a paid ticket closes.
Synchronous and immediate — **not** the queue+drip of `campaigns`. The webapp
renders the PDF (it owns the receipt data); this endpoint uploads it to Meta and
sends it as the `DOCUMENT` header of `purchase_receipt_1` (Meta's pre-built
utility receipt template).

```json
{
  "shop_id": "…",
  "customer_id": "…",
  "phone": "+393331112222",
  "payment_id": "…",
  "reference": "A1B2C3D4",
  "filename": "ricevuta.pdf",
  "pdf_base64": "…",
  "requested_by": "staff-uuid",
  "source": "receipt"
}
```

Flow: sender must be `online` → template `purchase_receipt_1` must be `approved`
on the shop's WABA (propagated proactively by the hourly sweep since 2026-09-23;
`ensure_receipt_template` still creates it lazily here if the row is missing —
the send-time self-heal) →
`POST /{phone_number_id}/media` (upload) → `POST /{phone_number_id}/messages`
with a `header` document parameter → record into `outbound_messages` with
`campaign_key = NULL` (so the campaign idempotency index does not apply and a
receipt can be re-sent).

Refusals surface as a `409` whose `detail` is a bare enum (`sender_not_online`,
`template_pending`, …), the same shape the webapp's `mapWaError` already reads.

**Template is not in `CATALOGUE`** — it lives in `DOCUMENT_TEMPLATES` in the same
file (2026-09-02), which is what puts its body under version control and in
`push-templates`' reconcile instead of leaving it to be hand-built in WhatsApp
Manager. Separate from the catalogue because the payload is an attachment: a
`HEADER`/`DOCUMENT` component, `create_document_template` rather than
`create_template`, no variables to fill, and the name is Meta's preset used
**verbatim** (`purchase_receipt_1`, never `it_purchase_receipt_1`). It propagates
to customer WABAs proactively — since 2026-09-23 it rides the hourly sweep
inside `ensure_templates` (gate: the same name **and the same body** approved on
Kairo's WABA, created with `create_document_template` + the sample URL), and the
worklist is keyed on `propagation_fingerprints()` = catalogue + document
fingerprints, so a shop missing the receipt is revisited like any other gap.
`ensure_receipt_template` remains as the lazy send-time self-heal, gated the same
way (`META_RECEIPT_SAMPLE_URL` required to create). Fails closed when either is
unconfigured.

---

## `POST /whatsapp/webhook`

One app-level URL for every customer. Meta identifies the tenant only by `entry[].id` — the WABA id — so `whatsapp.senders.waba_id` is the sole route from a payload to a shop.

Always answers **200** on a genuine request. Meta retries on anything else and disables a webhook that keeps failing, which would silently cost every delivery status and every opt-out.

| `field` | Effect |
|---|---|
| `messages` → `statuses[]` | `sent`/`delivered`/`read`/`failed` written to `outbound_messages` by `wamid` |
| `messages` → `messages[]` | Inbound reply persisted to `whatsapp.inbound_messages` (migration 17) — campaign measurement ("replied within 72h", design §9) reads it; a reply is matched back by phone (`from_phone` == the sent message's `to_phone`) |
| `smb_message_echoes` → `message_echoes[]` | A message the **owner** sent from their own WhatsApp Business App, recorded as an `outbound_messages` row with `origin = 'phone'` (migration 24) |
| `message_template_status_update` | Meta's verdict, applied to `(shop_id, name)` — **never by name alone**, since every salon's copy carries the same name |

Any other field is ignored and still answers 200 — Meta adds fields (`history`, `smb_app_state_sync`) to a subscription without asking.

Template verdicts arrive here within minutes instead of on the next hourly tick. The tick's poll survives as a **reconciler**: a missed webhook would otherwise leave a template `pending` forever, blocking every send for that shop and looking like nothing at all.

### Deduplication: Meta replays webhooks

`inbound_messages.wa_message_id` (Meta's `wamid`, migration 24) is the dedup key, and the insert is `ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL DO NOTHING RETURNING *`. A replay therefore returns **no row**, which is also how the caller knows to skip everything downstream — the dedup and "have we already processed this?" are the same question, answered in one statement with no check-then-act race. Without it a retry is a second bubble in the thread and a second AI classification that costs real money.

**The `WHERE` in that clause is not optional.** `inbound_messages_wa_id_uniq` is a *partial* index; Postgres cannot infer a partial index unless the `ON CONFLICT` clause repeats its predicate, and the statement fails outright with *"no unique or exclusion constraint matching the ON CONFLICT specification"* — the message is lost and the webhook 500s back to Meta. Verified against a real Postgres, both directions. See `AGENTS.md` 2026-07-18 and 2026-07-21, which are the same inference failure twice.

A message Meta sends without an `id` conflicts with nothing and always records, which is why the column is nullable and the index partial.

### Echoes from the owner's phone

Every sender is `coexistence`: the number is still live in the WhatsApp Business App on the owner's phone and they answer from there. Meta reports those under its own field, `smb_message_echoes`, whose `value.message_echoes[]` entries carry `from` (the business), `to` (the customer), `id`, `timestamp`, `type` and the type-specific body. Recorded so the thread is a whole conversation rather than Kairo's half of one.

Two rules, both load-bearing:

- **An echo does not extend the 24h service window.** That window is `max(received_at)` over *customer* inbound alone, and `record_echo` never touches `inbound_messages.received_at`. An echo that extended it would let us send into a conversation Meta considers closed — which comes back as an opaque provider error long after the cause.
- **An echo clears the unread state** on that thread (`read_at`). The owner has already answered; the Inbox must not keep asking them to.

Echo dedup is best-effort: the wamid goes into `provider_sid`, which has only a plain index behind it, so a genuinely concurrent retry could still double-write. A duplicate bubble is cosmetic; the inbound path, where a duplicate costs an AI call, is the one a unique index guards.

> **Unverified against a live WABA.** The echo payload shape is confirmed from Meta's Coexistence documentation as mirrored by two BSPs (Gupshup, 360dialog), not from a real webhook — no connected WABA exists yet. The parser is `.get()` chains that degrade to empty throughout, and the handler accepts both `smb_message_echoes` and `message_echoes` as the field name, because accepting a name Meta never sends costs nothing and missing the one it does send costs every echo.

### Interactive replies

A tap on a button or list we sent arrives as `type: "interactive"` with `interactive.button_reply.id` (or `list_reply.id`). That id is one **we** defined, so it *is* the intent: it is stored straight into `inbound_messages.intent` with `confidence = 1.0`, and the reply's `title` is stored as `body` so the thread shows the customer what they saw themselves tap. No model call, no cost, no possibility of a hallucinated intent. Anything typed arrives with both columns NULL, for the classifier.

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

`POST /messaging/tick` ([Number Provisioning](number-provisioning.md)) has four WhatsApp stages, each independently wrapped so one failure can't suppress the others:

- `whatsapp` — reconciles sender and template state against Meta, for verdicts the webhook didn't deliver, and carries the **only retry of the propagation gate**: live senders missing part of the catalogue — the receipt included since 2026-09-23 (`propagation_fingerprints()` = catalogue + document fingerprints) — **or holding an outdated body** are pushed once Kairo's own copy of that exact text turns `approved`. The worklist (`list_senders_needing_templates`) is keyed on `template_key|body_hash` pairs rather than on a count of rows, which fixed two things at once: a count could not see a body that changed under an unchanged name, and it was inflated by non-pushed templates, so a shop could look complete while missing something. The receipt flipped sides in 2026-09-23: it used to be the padding to exclude, and is now one of the fingerprints a shop must hold. Kairo's WABA is asked once per run, not once per shop — the answer is identical for everyone. An empty gate (unconfigured, or a Graph error) skips the stage entirely rather than pushing on a guess. Counts add `propagated`, `edited` and `approved_on_kairo`.
- `whatsapp_sends` — claims what is due and sends it. Counts: `sent`, `suppressed` (`no_consent`, `opted_out`, `recently_contacted`), `failed`, `deferred` (over daily cap, retried in an hour), `rate_capped` (Meta 131049, retried in 24h), `requeued` (claimed but never sent, recovered from a crashed tick).
- `whatsapp_nudges` — **the 20h nudge**. Meta's service window permits free-form messages only within 24h of the customer's *last* message, and it resets every time they write — so the only thing truly forbidden is speaking first after 24h of silence, which needs an approved template. One last free message inside the window (`NUDGE_AFTER_HOURS = 20`, `wa_nudge.NUDGE_BODY`) invites the customer to write back, and their reply is what reopens it. `should_nudge` is pure, `now` an argument, and refuses on every one of: shop not opted in, thread escalated, the owner already replied (webapp or phone echo), the customer replied after the agent (the thread is waiting on *us*), the agent never spoke, already nudged since their last message, and the window already closed. "At most once" is **derived from a row**, not a column: the nudge is recorded like any other agent reply (`origin='agent'`, `preview = NUDGE_BODY`) and `list_nudge_candidates` reads that back — so it survives a restart, and there is no second fact about the same send to keep true. Counts: `nudged`, `errors`.

---

## Out of scope

- Inbound replies are **persisted** (migration 17), read by campaign measurement, and now readable and answerable by the owner through [Threads](#threads-the-two-way-inbox) — but nothing answers them *automatically*. An agent that replies on the salon's behalf is the next phase; `POST /whatsapp/reply` is the same send path it will use.
- Contact / chat-history sync (`POST /{phone_number_id}/smb_app_data`). One-shot and irreversible per onboarding, and there is nowhere to put the data yet.
- The LLM template-picker that would *choose* among the marketing templates (`promo_v1`/`winback_v1`/`rebook_v1`/`promo_manual_v1`) per customer isn't built — the webapp names the `template_key` explicitly today.
