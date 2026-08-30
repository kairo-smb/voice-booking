# Project History

A running log of architectural decisions and the reasoning behind them, kept
so future work (by anyone, human or Claude) doesn't have to rediscover the
same trade-offs. Newest entry on top. Don't rewrite old entries when they're
superseded — add a new entry and note what changed and why; the old entry
stays as the record of what was true and decided at the time.

## 2026-08-30 — WhatsApp is BYO WABA only; the `source='new'` provisioning path is removed

**Decision:** dropped the second onboarding path entirely — owner's call, "we
will stick with the solely customer BYO WABA." Every sender is now
`coexistence`: the salon connects a WABA it already has, full stop. There is
no longer a path where Kairo provisions a brand-new WABA on a number the
salon doesn't yet have on WhatsApp.

**What came out, together:** `whatsapp_onboarding.py::SOURCES` and its
validation (source is no longer a caller-supplied input at all — `start()`
hardcodes `"coexistence"`); the `if row["source"] == "new": register…` branch
and the `pin` parameter threading through `complete()` and `CompleteRequest`;
`meta.register_phone_number()` in the Graph client, now fully dead (no
remaining caller — `scripts/kairo_waba.py`'s own `register` command talks to
Graph directly for Kairo's *own* number and was never this function).
Migration `18_whatsapp_coexistence_only.sql` narrows
`senders_source_check` from `IN ('coexistence','new')` to `= 'coexistence'`,
following migration 15's own drop-constraint/backfill/re-add shape — safe
because, per the 2026-08-24 entry, no live Meta call has ever gone through
this path, so the backfill `UPDATE` has nothing real to touch.

**Why removing it is safe to do outright, not just deprecate:** `source='new'`
was flagged as a rare, unexercised path from the day it shipped (2026-08-24:
*"one template per shop and a rare `source='new'` path"*) and never had a
UI — the webapp's `WhatsAppPanel.tsx` only ever offered a disabled "Bring your
own number (coming soon)" teaser button for it, never a working control. There
was nothing live depending on it in either repo.

**Kept, deliberately:** `whatsapp.senders.source` the column, and
`GET /whatsapp/status`'s `source` field — even at one legal value, it is
still meaningful provenance on the row, and dropping the column outright would
be schema churn for no behavioural gain. The `list_verifying_senders`
reconciler also survives untouched: it was never really about `source='new'`
availability polling (nothing sets `status='verifying'` anywhere in this
codebase, on either path) — it is what catches `complete()` crashing after
persisting the token/waba/phone_number_id but before flipping to `online`,
which can happen on a coexistence onboarding too.

**Webapp (own repo, own commit): the disabled BYON teaser came out too.**
`WhatsAppPanel.tsx`'s "Bring your own number (coming soon)" button never
called the removed path — it was inert, disabled from the start — but leaving
dead-end UI for a permanently removed capability is worse than no button, so
it's deleted along with its `byon_label`/`byon_teaser` i18n keys (all three
locales) and the now-meaningless `source`/`pin` plumbing in the two onboarding
proxy routes and `lib/whatsapp/client.ts` (Pydantic ignores unknown fields by
default, so these weren't breaking anything — just sending values the
`source`/`pin` upstream no longer looks at).

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — **470 passed, 14 skipped, 0 failed** (down
from 472/14/0: two tests deleted outright —
`test_complete_registers_the_number_only_for_a_brand_new_waba` and
`test_complete_rejects_an_unknown_source`, both asserting behaviour that no
longer exists — and `test_complete_subscribes_to_webhooks_before_anything_else`
was rewritten to assert subscribe-before-read-the-number instead of
subscribe-before-register, since register no longer exists to order against).
Migration 18 not applied against a live/scratch Postgres in this pass — no
live Meta call has been made on this feature at all yet, per every WhatsApp
entry above, so there's no real `source='new'` row anywhere to backfill; the
`UPDATE … WHERE source <> 'coexistence'` is exercised only by migration 15's
own test coverage of the identical pattern, not re-verified here.

---

## 2026-08-30 — Template propagation gated on Kairo's own WABA approving it first

**Decision:** `ensure_templates` (`services/messaging/whatsapp_onboarding.py`)
no longer pushes a catalogue entry into a salon's WABA unconditionally. It now
checks, live via Graph, that the same-named template is `approved` on **Kairo's
own WABA** first — the one `scripts/kairo_waba.py push-templates` submits to
by hand, for App Review evidence. Two new settings,
`META_KAIRO_WABA_ID`/`META_KAIRO_TOKEN`; unset means the gate fails closed —
every catalogue entry comes back in a new `not_ready` list, nothing is created
on any customer WABA — rather than falling back to the old unchecked behavior.

**Why:** a template rejection is Meta's judgment on the *content* — identical
whichever WABA it's submitted to. Pushing an unvetted catalogue entry straight
into every salon's WABA meant a bad template got rejected once per salon
instead of once, each rejection also costing that WABA's quality rating. Owner
wants the actual workflow to be: add/edit a template by hand on Kairo's own
WABA, watch it get approved there, and only then let `ensure_templates`
(already running at onboarding and hourly via `sweep()`) submit it — for
approval, independently — to every customer's WABA. That per-customer
submission was already the entire mechanism; the only change is refusing to
attempt it before Kairo's own copy is a known-good template.

**Not built: syncing template *content* from Kairo's WABA.** The gate only
reads a status (`approved`/not), not the template body — `whatsapp_templates.py`'s
`CATALOGUE` stays the one place body text and variable positions
(`{{1}}`=name, `{{2}}`=salon, `{{3}}`=offer) are defined, since
`whatsapp_send.py`/the webapp need to know what each variable *means*, not
just what text Meta currently has on file. Kairo's WABA is the approval gate,
not a content source.

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — 472 passed, 14 skipped, 0 failed (up from
471/14/0 the same day, from the two new gate tests). No live Meta call made —
same as every WhatsApp entry below, this is unverified against a real Kairo
WABA, which doesn't exist yet (see the standing "Still needed" list on the
2026-08-24 entry).

---

## 2026-08-29 — Graph API pinned v25.0 → v26.0

**Decision:** bump the pinned `GRAPH` base URL from `v25.0` to `v26.0` in both
callers (`booking_engine/clients/meta_whatsapp.py`, `scripts/kairo_waba.py`),
plus the `docs/knowledge/providers.md` note that says "all v25.0, pinned".

**Why now:** owner-requested. The webhook verification mechanism (verify token
on `GET`, HMAC `X-Hub-Signature-256` with the app secret on `POST`) is
version-independent — nothing there changes with the Graph version, so this
touches only the REST base URL, not the tokens. None of the endpoints in use
(`oauth/access_token`, `subscribed_apps`, `register`, `message_templates`,
`messages`) showed a breaking change in v26.0 when checked; they are the
stable core of the Cloud API and have carried shape across many versions.

**Unverified, flagged:** Meta's own changelog pages are JS-rendered and not
readable via plain fetch, and no secondary source turned up a v26.0
breaking-change list for these endpoints — so this is a pin bump on the
"no change found" evidence, not a verified upgrade. As before (2026-08-24
entry), **no live Meta call has been made**, so the bump cannot be exercised
against a real WABA from this repo; the first real onboarding/send is the
effective test.

---

## 2026-08-25 — Per-customer history: campaign_key is the market_intel campaign id

**Decision:** the WhatsApp campaigns feature (webapp design
`2026-08-24-whatsapp-campaigns-design.md`) links what was *sent* here to what
was *decided* in marketing-engine by the text `campaign_key`. When the webapp
enqueues an AI-built campaign it passes `campaign_key = market_intel.campaigns.id`,
so `whatsapp.outbound_messages` joins back to `market_intel.campaigns` on
`id::text = campaign_key` for the goal. Hand-made keys from the older
Touchpoint tile (`bulk_...`) have no `market_intel` row and come back with a
null goal — accepted: the per-customer tab is about AI campaigns.

**New read:** `GET /whatsapp/messages/{shop_id}?customer_id=` — the first
reader of `outbound_messages` (previously flagged as "no reader" in
providers.md). It returns every message sent to a person plus the campaigns
they were assigned to but never received (the holdout arm, from
`market_intel.campaign_recipients`), and doubles as the GDPR subject-access
artifact. Reads `market_intel` across schemas — the same shared-DB pattern as
this repo's existing `business_app_core` reads, and `market_intel` already
reads this repo's `whatsapp.outbound_messages` for the audience cooldown, so
the two schemas are already mutually dependent in the read direction.

**Second signal needed, second change:** campaign measurement's "replies within
72h" had nothing to read — the inbound webhook logged and discarded replies.
Migration `17_whatsapp_inbound.sql` adds `whatsapp.inbound_messages` and the
webhook persists each reply there. A reply is matched back to the message it
answers by phone (`from_phone` == the sent message's `to_phone`), no sender
identity required. Still nothing *answers* the reply — the 24h session window
it opens stays a future phase.

**Verification:** full non-`live_db` suite green (469 passed, 15 skipped).

---

## 2026-08-24 — Meta's limits become a floor nothing may cross; our counters sit on top

**Follow-up to the migration entry below, same day.** That change moved
WhatsApp onto Meta but left our commercial numbers and Meta's platform
ceilings as unrelated things that happened to be far apart. This makes the
relationship structural.

**Decision:** `services/messaging/meta_limits.py` is the single home of every
Meta-imposed ceiling, and every commercial knob is composed with it by
`min()`, never independently.

```
Meta's limits    — platform facts. Never exceeded, by construction.
    ↓  min()
Kairo's limits   — commercial knobs: daily_cap, the plan allowance.
    ↓
the queue
```

**Three real ways we could have exceeded a Meta limit, all closed:**

1. **`daily_cap` had no relationship to the sender's actual tier.** It was a
   hand-set column defaulting to 50, and *nothing read `messaging_limit` at
   all* — the field was stored and never consulted. Setting it to 5000 on a
   Tier-250 sender would have sent 5000. `effective_daily_cap()` is now
   `min(Meta's tier, ours)`, and `GET /whatsapp/status` returns that binding
   number as `daily_cap` with the raw column demoted to
   `configured_daily_cap`. A commercial decision can only ever narrow.
2. **`sent_today` counted the calendar day; Meta counts a rolling 24 hours.**
   A sender at its ceiling at 23:00 was handed a full fresh allowance ninety
   minutes later — nearly two tiers' worth inside one of Meta's windows. New
   `sent_last_24h()` is the Meta check. `sent_today()` survives *only* as the
   owner-facing counter, where "quanti ne ho mandati oggi" is what the number
   honestly means, and its docstring says never to use it for a ceiling.
3. **Migration 15 stored the wrong field in `messaging_limit`.** It was
   populated from the phone number's `throughput.level` — messages per
   *second* — while the name and every downstream reader mean conversations
   per 24h. Two different ceilings with different consequences, silently
   swapped. Migration 16 adds `throughput_level` for the real thing and nulls
   any pre-existing `messaging_limit` that isn't a `TIER_*` value, so the
   fallback path fails closed to 250 until the next sweep reads the truth.

**Everything fails closed, and the tests are about the direction of a
mistake.** An unrecognised tier is the unverified 250; an unknown throughput
is the slowest rate that exists. If Meta invents `TIER_5K` we under-send until
someone adds the row — recoverable. Over-sending costs quality rating, tier,
and eventually the sender.

**The per-recipient cooldown is the by-design half of `131049`.** The
migration entry below handled that error *reactively* (requeue +24h). But
reacting costs the send and a quality-rating hit, and Meta never publishes the
per-user threshold, so reacting is the only way to find it. The cooldown
(`WHATSAPP_RECIPIENT_COOLDOWN_HOURS`, default 168) means we simply stay under
it: one batched query at enqueue, re-checked at send for the same reason
consent is re-read — a row on a multi-day drip can be overtaken by another
campaign. It counts `sent_at`, so a message that never left never starts a
cooldown, which keeps the 131049 retry path coherent with it. New
`suppressed_reason` values: `recently_contacted`,
`marketing_blocked_destination`.

**No per-number pacer, deliberately — the invariant is enforced once
instead.** `MAX_SENDS_PER_MINUTE` is `COEXISTENCE_MPS × 60` = 1200. Below
that, no single number can be over-driven however the claimed batch happens to
fall across shops, because 20 mps (coexistence) is the slowest per-number
throughput Meta grants. So `send_due` *clamps* `WHATSAPP_SENDS_PER_MINUTE`
rather than trusting it, and a second per-number mechanism would be dead
machinery at any sane configuration. A test asserts the clamp on the `Pacer`
the loop actually constructs, so raising the env var to something absurd
cannot silently remove the ceiling.

**Two more limits that would have failed opaquely:** marketing to `+1`
recipients (Meta paused it entirely on 2025-04-01) is now refused at enqueue
instead of queued as a certain provider failure; and the Tech Provider
onboarding cap — **10 new customers per rolling 7 days** until Access
Verification, 200 after (`META_ACCESS_VERIFIED`) — is checked in `complete()`
*before* the popup's single-use code is spent, so the 11th salon gets a named
error and a "wait" instead of a broken flow they cannot retry.

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — **471 passed, 14 skipped, 0 failed**, up from
441 earlier the same day. Migrations 14→16 applied **twice** in sequence
against a scratch Postgres, both passes exit 0. Migration 16's corrective
`UPDATE` was checked in both directions against real rows rather than assumed:
a stale `messaging_limit = 'STANDARD'` is cleared to NULL, and a legitimate
`'TIER_1K'` survives a re-run untouched — the `NOT LIKE 'TIER\\_%'` guard is
what makes it safe to re-apply. `sent_last_24h`, the batched
`recently_contacted` lookup and `onboarded_last_7_days` were each executed
against that DB with real rows, and both new partial indexes confirmed
created.

**Flagged, not actioned:** template *creation* rate (100/hour/WABA) and the
number registration cap (10 per number per 72h, error 133016) are not
enforced — one template per shop and a rare `source='new'` path make them
unreachable today, but they belong in `meta_limits.py` the day either
assumption stops holding.

---

## 2026-08-24 — WhatsApp leaves Twilio: Meta Cloud API direct, coexistence, and the salon pays Meta

**This supersedes the 2026-08-21 entry's architecture entirely.** That entry
recorded "one WABA per salon, in its own Twilio subaccount" as *forced by
external rules, not chosen*. One of those two rules was real and remains
true (marketing = pre-approved template only). The other — "one WABA per
Twilio account, therefore a subaccount per salon" — turned out to be the
wrong problem to solve, because **Twilio cannot hold a salon's existing WABA
at all**, at any account granularity.

**Decision:** WhatsApp moves off Twilio onto **Meta Cloud API direct**, with
Kairo as a Meta **Tech Provider**. New `clients/meta_whatsapp.py`,
`services/meta_signature.py`, `services/messaging/pacer.py`, migration
`15_whatsapp_meta.sql`; `clients/twilio_whatsapp.py` deleted. Twilio keeps
voice and SMS, unchanged.

**Three facts killed the Twilio model, and all three are documented by
Twilio itself.** (1) Registration requires Twilio to attach the WABA to
*Twilio's* Meta credit line, and Meta only permits a payment method to be
revoked, never removed — so a WABA created anywhere else fails with error
`63103`. Twilio's Self Sign-up doc states the rule outright: *"Don't select
a WABA that's been created outside of Twilio."* (2) Twilio's migration path
for an existing number warns *"You won't be able to continue using WhatsApp
or WhatsApp Business App with the same phone number."* Our customer is a
hairdresser who runs their business from that app; asking them to delete it
is not friction, it is a refusal. (3) Templates cannot be created in a WABA
Twilio does not own — and injecting Kairo's flow templates into the salon's
WABA *is* the feature.

**Coexistence is what makes the whole thing sellable, and only Meta has it.**
Meta's Embedded Signup (May 2025) connects a number already live on the
WhatsApp Business App to Cloud API and keeps both active: the salon keeps
chatting from their phone, we send templates through the API. One flag —
`featureType: "whatsapp_business_app_onboarding"` — no allowlist request, but
it does require Advanced access to `whatsapp_business_management`. 360dialog
was evaluated as the "delegate it to a BSP" option and rejected on price:
~€49/number/month is roughly 3× the cost of the messages themselves at 50/day.

**Onboarding collapsed from three round-trips to one, and the entire OTP
apparatus is deleted.** Meta's popup performs verification itself, so
`/whatsapp/webhook/otp`, the temporary inbound-SMS webhook binding,
`handle_otp_sms` and `submit_code` are all gone — as is the `source='kairo'`
path that reused the Estonian voice number. That path only ever existed to
dodge a second number purchase under Twilio's model; with coexistence the
salon's own number is the entire point. `source` is now
`('coexistence','new')`. `start()` also stops creating anything
provider-side: the old version created a Twilio subaccount before the salon
had done anything, and leaked one on every abandoned onboarding.

**Ordering inside `complete()` is load-bearing, not stylistic.** Exchange the
code and *persist the token before using it* (a crash after that point leaves
a resumable row; losing the token leaves a WABA we can neither reach nor
unsubscribe from). Then `subscribed_apps` **before anything else**, because
forgetting it fails in the worst possible direction: every send still
succeeds while we receive no delivery status, no template verdicts and no
opt-outs. Then register the number — **skipped for coexistence**, where it is
already registered and Meta's guidance is explicitly not to call it. Then
read the number back from Meta rather than trusting what the popup told the
browser. A test asserts the subscribe-before-register order rather than
leaving it to comment discipline.

**Billing changes materially, and this is the consequential decision here.**
A Tech Provider, unlike a Solution Partner, has **no credit line to share**:
each salon attaches its own card to its own WABA and Meta bills it directly.
Continuing to debit `send_credits()` on top would charge the salon twice for
one message, so `try_debit_for_message` is removed from the WhatsApp path and
`ai_token_log.whatsapp_message_id` goes back to being unwritten — the column
the 2026-08-21 entry noted "nothing has written until now" is unwritten
again. `whatsapp_pricing.py` loses Twilio's flat fee, which incidentally
restores `docs/messaging-design.md` §5.1's original "$0 for a free-form
message": the 2026-08-22 entry's correction was right for Twilio and is now
moot. **The SMS path is untouched and still debits 2×** — there Kairo really
does pay Twilio. What survives is the **plan allowance**: it is a product
limit, not cost recovery, and the 2026-08-22 reasoning for it holds exactly.

**`price_usd` is now an estimate that is never corrected.** Meta reports no
amount on send and none on the status webhook, so unlike the SMS path there
is no later reconciliation to a real price. The column's docstring and a
`COMMENT ON COLUMN` both say so, because a number called `price_usd` that
isn't a price is exactly the kind of thing someone builds a report on.

**`131049` and `131050` look alike and must not behave alike — the Twilio
version conflated them.** `63033`/`63050` were one "stop asking" bucket.
Meta's `131050` is the real opt-out (the recipient used the native "Stop
promotions" button) and is permanent. `131049` is the per-user, **cross-brand**
marketing cap — the recipient has had enough marketing today, from *anyone*,
possibly having never heard from this salon. Treating it as an opt-out
permanently silences a customer who did nothing; treating it as an ordinary
retry hammers Meta. It gets a third outcome: requeued 24 hours out.

**Webhooks are one URL for every tenant, signed over the raw body.** Meta
posts all customers' traffic to a single app-level endpoint and identifies
the shop only by `entry[].id`, the WABA id — hence a unique index on it and
`get_sender_by_waba` as the sole route from payload to shop. Signature is
`X-Hub-Signature-256`, HMAC-SHA256 with the **app secret**, over the bytes as
received: re-serialising the parsed JSON changes whitespace and key order and
every genuine request starts failing. `senders.subaccount_auth_token` is
therefore dropped. The handler always answers 200 on a genuine request —
Meta retries otherwise and disables a webhook that keeps failing, which would
silently cost every delivery status and every opt-out.

**Template verdicts now arrive by webhook, and the poll survives anyway.**
`message_template_status_update` lands within minutes instead of on the next
hourly tick. The tick's poll is deliberately kept as a **reconciler**, not
deleted: a missed webhook leaves a template `pending` forever, which blocks
every send for that shop and looks like nothing at all. Two mechanisms for
one fact is normally the wrong shape; here the failure mode of the fast one
is silent and unbounded. Status updates key on `(shop_id, name)` and never on
name alone — every salon's copy of the catalogue carries the *same* name
(`kairo_promo_v1`), since Meta scopes names per-WABA, so a global update
would rule on every shop at once from one shop's webhook.

**The scheduler: two different problems, two different fixes.** (1) `spread()`
only ever laid messages across a single day and `enqueue_campaign` *rejected*
anything over `daily_cap` — which makes bulk impossible. It now chunks by the
cap and rolls onto following days, and the `over_daily_cap` rejection is
deleted, because spreading **is** the correct handling. Piling 400 recipients
onto today would just hand `send_due` 350 rows to defer by an hour,
repeatedly, until the queue is unreadable and the owner cannot tell when
anyone gets contacted. The monthly plan allowance remains the only hard
ceiling. (2) One tick claims up to 200 rows across every tenant and fired
them as fast as the loop ran. Meta's per-number ceiling (20/s, fixed, under
coexistence) is far above anything one salon does; the limit that can
actually be tripped is the **Graph API's app-level** one, shared by all
tenants. `send_due` now paces every send through a process-wide `Pacer` at
`WHATSAPP_SENDS_PER_MINUTE` (default 60). Dispatch stays **serial** on
purpose: the loop mutates per-shop remaining-cap counters as it goes, and
making it concurrent would turn those into locks for throughput nobody needs.

**Webapp (own repo, own commits): a bulk tile in Marketing → Touchpoint
Clienti.** The existing tile is per-customer win-back — pick one at-risk
name, generate bespoke copy, send. Bulk is a different verb, so it is a
different tile: pick an audience (a rischio / VIP / tutti i consenzienti),
write one offer line, watch it drip. New `BulkCampaignTile.tsx` plus
`/api/v1/hair-salon/whatsapp/{campaign,status}` proxies. **The audience is
filtered client-side from the list the tab already loaded**, and the engine
gained no audience query: the webapp's `payments/analytics` →
`retention_risk` is already the definition of "a rischio" the owner is
looking at, and a second one in this repo's SQL would drift from it. The
recipient cap on `POST /whatsapp/campaigns` went 500 → 2000. The tile also
renders the assembled template around the owner's `{{3}}`, so "why isn't my
text the whole message?" doesn't arrive as a support question.

**Flagged, not actioned:**
- **Coexistence credit lines cannot be migrated to another provider.** Meta
  requires a new WABA to move. That is real lock-in of the salon onto Kairo
  — good commercially, and it should be said during the sale rather than
  discovered later.
- **`senders.access_token` is stored in plaintext**, like
  `subaccount_auth_token` was. It is now a strictly bigger secret: full
  authority over one salon's WhatsApp, with no shared parent credential
  behind it. Encryption at rest wasn't in scope and isn't done.
- **Inbound replies are still logged and discarded**, and contact/chat-history
  sync (`smb_app_data`) is not built — one-shot and irreversible per
  onboarding, with nowhere to put the data yet.
- **Migration 15 drops columns rather than backfilling**, which is only safe
  because nothing has ever been sent through migration 14's tables.

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — **440 passed, 14 skipped, 0 failed**, up from
the 404/14/0 baseline recorded on 2026-08-22. Migrations 14+15 were applied
**twice** in sequence against a scratch Postgres (stub `business_app_core`
tables, same pattern as 12–15 before) — both passes exited 0, and the second
pass proved the `DROP CONSTRAINT IF EXISTS` + re-add of `senders_source_check`
is genuinely idempotent rather than accidentally working once. The generated
constraint name was confirmed against the live `\\d` output, not assumed. The
non-trivial statements were then run against that scratch DB with real rows:
`enqueue`'s `ON CONFLICT … WHERE` correctly inferred the partial unique index
and the second identical insert returned zero rows; `claim_due`'s CTE +
`FOR UPDATE OF … SKIP LOCKED` claimed and flipped a row, returning the new
`template_name`/`template_language`; `monthly_quota`'s inner join returned
400; `campaign_progress` counted correctly; and `set_template_status` was
confirmed to scope by `shop_id`. Webapp: `npx tsc --noEmit` exits 0. **No
live Meta call has been made** — every Graph interaction here is verified
against the API contract and unit tests, not against a real WABA.

**Still needed before this can be switched on** (see the Providers
page's WhatsApp section for the full clickpath): Meta app + Business
Verification for Kairo, Kairo's own WABA (`scripts/kairo_waba.py` drives it),
App Review for Advanced access to `whatsapp_business_management` +
`whatsapp_business_messaging`, Tech Provider onboarding for the Solution ID,
Access Verification to raise the onboarding limit from 10 to 200 customers per
rolling 7 days, then `META_APP_SECRET`/`META_SOLUTION_ID`/`META_VERIFY_TOKEN`
as Fly secrets and Meta's Embedded Signup JS (**v4** — v2 is deprecated
2026-10-15) in the webapp.

---

## 2026-08-22 — WhatsApp sends are capped by the subscription plan, and the price list has one home

**Decision:** how many WhatsApp messages a salon may send per month is a
property of its **plan**, read from
`business_app_core.subscription_plans.whatsapp_monthly_messages` (webapp
migration 54). Enforced twice — at `enqueue_campaign` and again in `send_due`
— and surfaced in the webapp's Inbox → Configurazione counter bar.

**Why the plan row and not a constant, a config var, or a column on `shops`.**
An allowance is a commercial term, so it changes for commercial reasons and at
commercial speed. On the plan row, changing one is
`UPDATE … SET whatsapp_monthly_messages = 400 WHERE tier = 'base'` — no deploy,
no per-shop backfill, and every shop on that tier moves together, which is what
"the Base plan includes 400 messages" actually means. A per-shop column would
have made the same change a migration over the whole customer table, and would
have quietly invited per-shop haggling as the default.

**Fails closed at zero.** `monthly_quota()` inner-joins `shops` to
`subscription_plans`; a shop with no plan matches no row and gets 0, sending
nothing. That is the §5.3 rule ("provisioning requires an active paid plan")
finally applied to *traffic* as well as to provisioning — before this, the only
ceiling on sending was `senders.daily_cap`, which is a Meta tier limit and has
nothing to do with what the salon paid for.

**Over quota suppresses; over the daily cap still defers.** They look alike and
must not behave alike. The daily cap clears at midnight, so a deferred row is a
promise the next tick keeps. A plan allowance clears on the 1st, so deferring
would leave rows re-scheduling themselves hourly for up to a month — a queue
nobody can read, and an owner who never learns why nothing went out. Suppressed
with `suppressed_reason = 'over_monthly_quota'` is a fact on a row.

**The counter counts `sent_at`, not rows.** A queued row that never left (no
consent, cancelled, out of credit) must not consume an allowance the salon
paid for.

**Correction to `docs/messaging-design.md` §5.1:** it priced a free-form
message inside the 24h session window at **$0 total**. Meta's share is zero;
Twilio's flat per-outbound-message platform fee is not, and applies to every
category (`channels-messaging-outbound` in Twilio's usage categories, and
"Every WhatsApp message sent through Twilio incurs a Twilio per-message fee"
in their WhatsApp FAQ). New `services/messaging/whatsapp_pricing.py` holds the
one table — Meta's per-category Italian rate plus that fee — and everything
else derives from it: the balance pre-check, the `MARKETING_USD` constant that
used to be a hand-copied `0.0741` literal, and the `pricing` block that
`GET /whatsapp/status/{shop_id}` now returns for the webapp to render. A price
the owner is quoted and a price we pre-authorise cannot drift apart if there is
only one of them.

**Still an estimate, deliberately.** The table prices a send before it happens;
the status callback writes the real `price_usd` after. The quoted number exists
to inform a decision, not to be the invoice.

---

## 2026-08-21 — WhatsApp marketing: one WABA per salon, in its own Twilio subaccount; templates, not free-form copy

**Decision:** personalised marketing moves from SMS to WhatsApp, one sender
per salon, ~50 messages/day dripped across opening hours. New `whatsapp`
schema (`booking_engine/db/sql/14_whatsapp_schema.sql`), new client
`clients/twilio_whatsapp.py`, three new services under
`services/messaging/whatsapp_{onboarding,send,templates}.py`, new routes
`booking_engine/api/routes/whatsapp.py`, and two new stages on the hourly
`POST /messaging/tick`.

**The architecture is forced by two external rules, not chosen.** (1) A
business-initiated WhatsApp marketing message can only be a template Meta
approved in advance — there is no free-form path outside Meta's 24h
customer-initiated session window. (2) Twilio allows exactly **one WABA per
account** ("You can't use multiple WABAs in one Twilio account"), so a
salon's WhatsApp Business Account cannot sit in Kairo's account alongside
every other salon's. Hence: Kairo is a Meta **Tech Provider**, each salon
gets a Twilio **subaccount** holding its own WABA, its own sender, and its
own approved templates. This is the same audited "do not reuse your own
business identity for a customer" rule the 2026-08-14 regulatory-bundle
entry already established, arriving from Meta's side this time.

**This directly contradicts the 2026-08-12 entry's reasoning for choosing
SMS, and that entry stands — the conclusion changed, the fact didn't.** That
entry said WhatsApp "forbids free-form business-initiated messages outside a
24h customer-initiated session — only pre-approved templates, which can't
carry the per-customer LLM-generated copy this feature exists to send." The
constraint is exactly as described. What changed is the answer to it:
personalisation now lives *inside template variables* — a fixed, Meta-approved
skeleton plus a per-customer offer line the LLM writes (`{{3}}` in
`promo_v1`). That is a genuine downgrade in expressive range from SMS, and
it is recorded as one rather than dressed up: the alternative, a template
whose body is essentially one big `{{1}}`, is the single most common cause of
Meta rejecting a template outright, and a rejected template sends nothing at
all. The webapp's existing win-back copy generator now writes a variable,
not a message.

**Onboarding is two round-trips because a WABA can only be created by the
salon, in Meta's browser popup.** No server-side API creates a WABA on a
customer's behalf. So `POST /whatsapp/onboarding/start` creates the
subaccount and hands back the Embedded Signup config; the salon completes
Meta's popup in the webapp; `POST /whatsapp/onboarding/waba` registers the
sender via `POST messaging.twilio.com/v2/Channels/Senders` with
`account_type: ISVSubAccount`.

**The number is not a new purchase — `source='kairo'` reuses the voice
number.** `voice_agent.shop_telephony.kairo_number` is already bought,
already SMS-capable (which the 2026-07-16 entry chose Estonia *Mobile* over
*Local* specifically to guarantee), and already sits under the salon's own
regulatory bundle. Reusing it avoids a second ~$3/mo per salon **and** avoids
the real trap in the obvious alternative: buying a number inside the
subaccount would need a regulatory bundle inside that subaccount, since
bundles don't cross accounts — re-running the entire 2026-08-14 KYC flow per
salon a second time, for a number they don't need. Transferring the existing
number into the subaccount was rejected too: `number_health`,
`number_release`, and the TwiML signature check all address it with parent
credentials, and moving it would have broken three working things to fix
none. `source='salon'` imports the salon's own number instead (they must
first delete its WhatsApp/WhatsApp Business App account).

**Meta's ownership OTP is the one place the two number paths diverge, and
the Kairo path needed an inbound-SMS webhook back.** For `source='salon'`
the code lands on the salon's phone and they type it in. For
`source='kairo'` it lands on a number *we* own, whose SMS webhook has been
deliberately unset since 2026-08-15. So `attach_waba` temporarily binds
`/whatsapp/webhook/otp`, and `_finalize` unbinds it the moment the sender
goes online. **This is not a reintroduction of STOP handling:** nothing
parses message content beyond a 6-digit run, it only fires for a shop
actually mid-verification, and the hook does not survive onboarding.

**WhatsApp gives back the self-service opt-out the 2026-08-15 STOP removal
gave up — from Meta, not from us.** Every marketing template carries Meta's
native "Stop promotions" button. A refusal comes back as error `63033`/
`63050` on the status webhook, which writes `marketing_consent = false` into
`business_app_core.customers`. So for this channel the "materially weaker
position under Italian marketing-SMS rules" that entry recorded plainly does
not apply: there *is* an always-available, per-message, self-service opt-out,
and honouring it also stops the next campaign burning a send on a guaranteed
failure. The SMS entry's tradeoff is unchanged for SMS; this is a second
channel, not a revision of that decision.

**Webhook signatures needed a per-subaccount token — the one security detail
that would have failed closed and looked like a Twilio outage.** Twilio signs
a webhook with the auth token of the account that *owns the resource*, so a
salon's WhatsApp traffic is signed with that subaccount's token, not
`TWILIO_AUTH_TOKEN`. Validating against the parent token would have rejected
100% of genuine callbacks. `services/twilio_signature.py` gained an
`auth_token` override (the single shared verifier stays single, per the
2026-08-12 "one verifier, not two, or they drift" precedent) and
`whatsapp.senders.subaccount_auth_token` stores the token Twilio returns
exactly once, at subaccount creation. Note the asymmetry: API calls *into* a
subaccount need no per-salon secret at all (subaccount SID as basic-auth
username, parent token as password) — only inbound signature validation does.

**Sending is queued, the opposite of `/sms/send`, because "50 per day
distributed across the day" is the feature.** `enqueue_campaign` writes one
row per recipient with a `scheduled_at` spread evenly across 09:00–20:00
Europe/Rome; the hourly tick claims what is due and sends it. The schedule is
decided at enqueue time rather than as a per-tick quota so the owner can see
exactly when each customer will be contacted, and cancelling is deleting
queued rows. `spread()` is a pure function — the awkward cases (before the
window, inside it, after it → roll to tomorrow) are unit-tested without a
clock.

**Consent is re-read at send time, not trusted from enqueue.** A queued row
can sit for hours. A customer whose consent is cleared in-store at 11:00 must
not receive the message scheduled for 15:00 — the same trust-boundary
reasoning as `sms_send.py`'s re-check, but it actually bites here, where the
SMS path's re-check was near-simultaneous with the send.

**Over the daily cap means later, not never.** A claimed message for a shop
that has exhausted `daily_cap` is requeued +60 min, not dropped: the owner
scheduled it and believes it is going out. Meta caps an unverified WABA at
250 business-initiated conversations/24h; 50 sits well inside that, which is
why Meta **Business Verification is not a prerequisite** for this feature
(it is for higher tiers, and for WhatsApp Business Calling).

**`status = 'sending'` is a claim, not a provider state.** `claim_due` flips
rows into it in the same statement that selects them (`FOR UPDATE … SKIP
LOCKED`), so two overlapping ticks — or two Fly machines — can never both
send the same row. A row stuck there because a tick died mid-send is requeued
by the next sweep rather than silently lost. Idempotency at the campaign
level is a partial unique index on `(shop_id, campaign_key, customer_id)`, so
a retried or double-clicked enqueue is a no-op, matching how
`sms.outbound_messages` already guards batch sends.

**Billing reuses the SMS path unchanged**: `send_credits()` (2×
pass-through, not the webapp's 10× LLM margin), balance checked before the
provider call, debited only *after* Twilio accepts. It writes
`ai_token_log.whatsapp_message_id` — the column the webapp's own migration
`46_ai_token_log_message_fk.sql` added on 2026-08-12 and nothing has written
until now.

**Flagged, not actioned:**
- **Number release doesn't deregister the WhatsApp sender.**
  `services/number_release.py` hands the Estonian number back to Twilio on a
  lapsed plan, but nothing closes the salon's subaccount or deregisters its
  sender — same class as the "Cancellation gap" the 2026-08-14 entry flagged
  for numbers, now reopened one layer up. A closed subaccount is not billed,
  so this is a hygiene/orphan problem, not a cost leak.
- **Inbound replies are logged and discarded.** A reply opens Meta's 24h
  session window, inside which free-form messages *are* allowed — the obvious
  next phase (conversational booking over WhatsApp), and the only path to
  sending genuinely free-form personalised copy. `/whatsapp/webhook/inbound`
  exists because the sender registration requires a callback URL, and does
  nothing else.
- **One template in the catalogue** (`promo_v1`). The machinery takes N; the
  LLM template-picker that would make N worth having isn't built.

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — **404 passed, 14 skipped, 0 failed**, up
from a 382/14/0 baseline measured on this branch by re-running the suite with
`tests/booking_engine/test_whatsapp.py` excluded (not the 373 recorded in the
2026-08-15 release entry — 9 tests landed between). All 22 new tests are in
that one file; no existing test was modified. Migration 14 was applied
**twice** against a scratch Postgres (stub `business_app_core.shops`/
`customers`, the same pattern migrations 12 and 13 were verified with) — both
runs exited 0. The three non-trivial statements were then executed against
that scratch DB with real rows rather than assumed correct: `enqueue`'s
`ON CONFLICT … WHERE` correctly inferred the partial unique index and the
second identical insert returned zero rows; `claim_due`'s CTE + `FOR UPDATE
OF … SKIP LOCKED` claimed and flipped the row; `requeue_stuck` correctly
no-opped on a fresh row while `requeue_one` pushed `scheduled_at` forward.
Confirmed `connection.py` registers no jsonb codec, so asyncpg really does
hand `variables` back as **text** — the `isinstance(variables, str)` branch in
`send_due` is the production path, not a defensive one, and has its own test.

**Still needed before this can be switched on** — none of it automatable
from this repo: create a Meta app and get it approved, accept Twilio's
Partner Solution link (~3–4 weeks, per Twilio's own estimate), turn on 2FA
and complete Meta business verification for Kairo's Business Portfolio,
register a WhatsApp sender for Kairo itself via Self Sign-up first (Meta
requires this before a portfolio is eligible), then set `META_APP_ID` /
`META_CONFIG_ID` and integrate Meta's Embedded Signup JS in the webapp. No
live send has been made; every Twilio interaction here is verified against
the API contract and unit tests, not against a real WABA.

## 2026-08-15 — Grace-period number release, closing the 2026-08-14 "Cancellation gap"

**Decision:** built `services/number_release.py`, closing the gap the
2026-08-14 self-service-provisioning entry flagged but didn't fix: when a
shop's subscription lapses (webapp's `billing/webhook/process-event.ts`
nulls `shops.plan_id`), nothing in this repo released the shop's Twilio
number — it kept costing ~$3/mo with no plan behind it. Not an instant
release: releasing the moment a plan lapses gives the salon no warning and
Twilio does not hold a released number for them to reclaim if they
resubscribe. Instead, a pure `decide_release(inp, now)` policy function
(`has_plan`, `release_scheduled_at`) → `('none'|'schedule'|'clear'|'release',
deadline)` drives an hourly-tick-shaped `sweep()`: the first tick that sees a
number with no plan behind it schedules a 14-day deadline
(`GRACE_DAYS`), a later tick clears it if the plan comes back in time, and
only a tick that finds the deadline already passed calls `release_for_shop`.

**The one rule that makes this safe under repeated hourly ticks:** "no plan,
already scheduled, deadline still in the future" must return `('none',
existing_deadline)`, not re-schedule — re-stamping `now() + 14 days` on
every tick would push the deadline forward forever and the number would
never actually be released. Covered by a regression test that runs
`decide_release` twice, an hour apart, and asserts the deadline didn't move.

**The deadline lives in `voice_agent`, not `business_app_core`, on
purpose** — a `plan_lapsed_at` column would belong on `shops`, which is the
webapp repo's schema, not this one's to alter. `shop_telephony.release_scheduled_at`
(migration `13_number_release.sql`) is derived from when *this* repo's tick
first noticed, which is all it needs to be.

**`release_for_shop` order is deliberate: Twilio first, the local row
second** — same orphan-prevention shape as `insert_telephony`'s
insert-only guarantee (2026-08-14 entry above). Deleting
`shop_telephony`'s row before Twilio confirms the number is gone would
leave us paying for a number nothing tracks anymore. A Twilio 404 is
treated as "already released" (e.g. a prior run released it and crashed
before deleting the row) and still proceeds to delete + record; any other
Twilio failure leaves the row untouched so the next tick retries — never
silently drops the shop from future consideration.

**`number_requests.status` gained `'released'`**, plus `released_at`/
`released_number` columns, so a released shop's history survives after its
`shop_telephony` row is deleted (and so a later re-provision starts from a
known prior state, not a gap). The status CHECK constraint (created inline
in migration 12) had to be dropped and re-added to widen it — its generated
name (`number_requests_status_check`) was confirmed by querying
`pg_constraint` against a scratch DB built from the full migration chain
through 12, rather than assumed. Idempotency proved by applying the entire
`booking_engine/db/sql/` chain twice against a fresh scratch DB (stub
`business_app_core.shops`/`customers`/`appointments`/`ai_token_log` tables,
same pattern migration 12 was verified with) — both runs exited 0, and
`'released'` was accepted while a bogus status was still rejected,
re-checked after the double apply specifically (not just the first).

**Wired into the hourly tick** as a third stage, after the health check, in
`booking_engine/api/routes/messaging_tick.py` — this entry originally
covered the release mechanism only (matching that task's explicit scope,
whose commit list didn't touch `messaging_tick.py`); the wiring landed in a
follow-up change the same day. Runs last so a failure in the release sweep
can never suppress the health refresh above it, wrapped in its own
try/except so it's counted under `errors` rather than 500ing the tick. Also
added in that follow-up: an explicit owner-initiated `POST
/voice/numbers/release` endpoint (`voice_telephony.py`) that calls
`release_for_shop` directly, bypassing the grace period on purpose — a
salon giving up its number deliberately, not a lapsed-plan guess. Still
needed: a QA-branch check that a shop with `plan_id IS NULL` and a live
`shop_telephony` row gets the flow it's supposed to.

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — 373 passed, 14 skipped, 0 failed (up from
the 359/14/0 baseline recorded in the 2026-08-15 STOP-handling entry below,
+14 new `test_number_release.py` tests, no existing test touched).

## 2026-08-15 — Removed STOP handling entirely; suppression is `marketing_consent` alone (owner decision)

**Decision (owner's, not a recommendation — implemented as directed, not
re-litigated):** marketing SMS no longer carries an in-message opt-out.
Removed, together, both halves of the old mechanism — the appended footer
(`" Rispondi STOP per non ricevere piu'."`) and the code that honoured a
STOP reply — on the reasoning that leaving either one alone is worse than
either clean end state (promising an opt-out we don't offer, or silently
dropping the promise while still parsing replies nobody can act on).
Suppression is now `business_app_core.customers.marketing_consent` alone;
opt-out happens in-store, when a staff member clears marketing consent in
the app.

**What was deleted:** `sms_send.py`'s `OPT_OUT_FOOTER` constant and the
append; its `is_opted_out` gate and `_suppress("opted_out")` branch
(`_has_active_consent` is now the *only* suppression rule — its docstring
says so); `services/messaging/sms_inbound.py` (STOP-keyword parsing) and its
test file entirely; the `POST /sms/webhook/inbound` route and its
`X-Twilio-Signature` verification (`POST /sms/webhook/status` is
untouched); `sms_queries.py`'s `is_opted_out`/`record_opt_out`/
`withdraw_marketing_consent`/`get_shop_by_sender_number` (that last one
existed only to route inbound messages to a shop); the `sms_url` parameter
on `twilio_numbers.py::purchase_number` and its two callers
(`number_provisioning.py`, `voice_telephony.py`) — a purchased number no
longer gets an SMS webhook bound at all, since nothing would answer it.

**`sms.opt_outs` is left in place, not dropped.** `11_sms_schema.sql` no
longer creates it (removed the `CREATE TABLE` block, replaced with a
one-line comment pointing here), but no `DROP TABLE` was added either —
environments that already have the table keep it, harmlessly empty and
unread by any code path now.

**`number_health.py::decide_health` now checks `voice_url` only.** It used
to flag `webhook_drift` unless *both* `voice_url` and `sms_url` pointed back
at us; with no inbound SMS handler there is nothing for `sms_url` to
correctly point at, so requiring it would have permanently flagged every
number red. `HealthProbe` dropped its `sms_url` field to match. (Twilio's
own `NumberStatus`/`fetch_number` still read `sms_url` back from the API —
harmless, unrelated to the health verdict — left alone since nothing asked
for it and it costs nothing to keep reading.)

**Bundled into the same pass: fixed `_twilio_send` never passing
`status_callback`.** Verified against a live send that Twilio's status
webhook (`POST /sms/webhook/status`) could never fire without it — the
`sms.outbound_messages` row stayed `status='sent'`/`price_usd=NULL` forever
even though Twilio has the real price. `send_marketing_sms` now takes
`public_base_url` and threads a `status_callback` (`{public_base_url}/api/v1/sms/webhook/status`,
mount prefix confirmed against `api/app.py` rather than assumed) through to
`_twilio_send`, which now passes it to `client.messages.create()`.

**This is a materially weaker position under Italian marketing-SMS rules —
recorded plainly, not softened.** An in-message STOP reply is common
practice for demonstrating an always-available, per-message opt-out to the
Garante; relying solely on staff clearing consent in-store means a customer
who wants to stop receiving messages has no self-service channel and must
rely on the salon acting on their request. This tradeoff was made
knowingly by the owner, not discovered and left unaddressed — restated here
so it isn't mistaken for an oversight later.

**Verification:** `python -m pytest tests/ --ignore=tests/live_db
--ignore=tests/live_twilio -q` — 359 passed, 14 skipped, 0 failed (down from
365 passed before this change: net of deleting `test_sms_inbound.py`'s 5
tests and `test_opted_out_phone_is_suppressed`, replacing
`test_opt_out_footer_is_appended_server_side` with an equivalent
no-suffix assertion, and adding 2 new `status_callback` tests).
`tests/live_db/test_sms_live.py` (not run in this environment — no
`TEST_DATABASE_URL` — but checked for dangling references) had its
`TestOptOutLive` class and `test_unrouteable_number_resolves_to_no_shop`
removed, since they exercised the deleted query functions directly against
real Neon. GSM-7 segment counts in the surviving tests did not change —
recomputed rather than assumed: every existing test body is short enough
(well under 160 GSM-7 chars) that removing ~37 characters of footer doesn't
cross a segment boundary either before or after.

## Documentation

Human-oriented docs (architecture, database, voice-agent logic, providers,
operations, API reference) live in `docs/knowledge/` — a Docsify site, `npx
--yes serve docs/knowledge` to browse. This file stays what it already is:
the append-only decision/incident history. `docs/knowledge/decisions.md` is
a short index into it, kept in sync by hand.

**Any change that adds, removes, or changes a REST/voice-tool endpoint, a
database table, a provider integration, or a safety/authz/booking-constraint
rule updates the matching `docs/knowledge/*.md` file in the same change** —
not as a follow-up. See `docs/knowledge/README.md` for the full rule.

---

## 2026-08-14 — Self-service Estonian number provisioning: the 2026-07-16 shared-bundle model is SUPERSEDED for this path

**The 2026-07-16 "one Kairo-entity bundle, reused across every shop, no
per-salon KYC" decision (below) does not hold for self-service.** Twilio's
ISV guidance is explicit: *"Each customer needs their own bundle. Do not
reuse your business information in customer bundles. End-User records must
reflect the actual end-user, not you. Twilio audits this."* A salon
requesting its own number through the webapp now gets its own regulatory
bundle, built from a form it fills in — not a slot in Kairo's shared one.
**This supersedes only the self-service path**: the shared-bundle
`TWILIO_BUNDLE_SID` and the manual `POST /voice/numbers/search` +
`/voice/numbers/provision` pair are untouched and still live, still used for
Path 1 (forwarding) and ops-triggered onboarding — reusing Kairo's own info
across *Kairo-triggered* provisioning was never the audited case Twilio's
rule is about. Only reselling to a salon under its own name required a
bundle-per-customer.

**What Estonia Mobile actually requires — queried, never hardcoded.**
`GET /v2/RegulatoryCompliance/Regulations?IsoCountry=EE&NumberType=mobile`
returns exactly one regulation (`RN26dca8d0e541a6c8fce4abd46e518506`,
business-only — no individual option, a sole trader with no company
registration cannot get a number at all): one End-User field
(`business_name`) and one supporting document
(`commercial_registrar_excerpt`, an Italian *visura camerale*). No address,
VAT, or personal ID — lighter than the 2026-07-16 research suggested, and
**sending fields the regulation doesn't ask for is a known cause of
evaluation failure**, so nothing beyond that is collected. The regulation is
looked up at request time and stored on the request row rather than
hardcoded, since Twilio's own docs say these can change;
`tests/live_twilio/test_estonia_regulation.py` is the canary — a red run
there means Estonia's rules moved, not a code bug.

**Twilio is the validator, not us.** `POST /Bundles/{sid}/Evaluations` is
synchronous and returns field-level violations, shown back to the salon
verbatim. Verified against a live noncompliant evaluation while building
this: the response objects have **no `description` field** — the human
explanation is in `failure_reason`. Parsing `description` doesn't error, it
just silently returns empty explanations; a genuine dead end that surfaced
because two people happened to eyeball the raw response rather than trust
the SDK's shape.

**Reintroduces the exact friction the 2026-07-16 decision eliminated, and
that's accepted, not a regression.** Per-salon KYC is unavoidable once Kairo
is reselling under each salon's own name rather than its own. It puts a
document fetch plus a multi-day Twilio review between "salon pays" and
"salon has a number" — see the "waiting experience" states in
`src/lib/numbers/request-state.ts` (webapp) for how that gap is surfaced
rather than left to look broken. No specific completion date is shown
(decided 2026-08-13): "pochi giorni lavorativi" instead of a number, since
there's no observed review-time data for Estonian mobile bundles yet and a
date that slips reads worse than no date. `REVIEW_BUSINESS_DAYS = 3`
(webapp) drives only the switch to "taking longer than expected" copy.

**A real bug this closed: the upsert orphaned purchased numbers.**
`upsert_telephony`'s `ON CONFLICT (shop_id) DO UPDATE` meant a second
provision call bought a second number and overwrote the row — the first
stayed billed by Twilio (~$3/mo) with nothing in the DB referencing it. The
primary key guaranteed one *row*, not one *purchase*, and self-service makes
a double-click a real path to hitting this, not just a theoretical race.
Fixed with a new `insert_telephony` (`ON CONFLICT DO NOTHING RETURNING *`)
used only by the provisioning paths (`upsert_telephony` itself is untouched,
still used for legitimate updates like activation status): check-first
(idempotent replay of a double-tick), insert-only, and on a lost race
`release_number` the just-purchased number back to Twilio rather than leak
it. A failed release is logged loudly, not raised — raising would abort that
shop's tick and the next run takes the `already_provisioned` path, so
nothing would ever look at the leaked number again.

**The health semaphore is outage-safe by construction.**
`services/number_health.py::decide_health` returns `None` (no verdict) for
an inconclusive probe — Twilio unreachable — which only stamps
`health_checked_at` and leaves `health_status` alone. Only a confirmed 404
or webhook drift is allowed to flip the light. Without this, a transient
Twilio outage during the hourly tick would repaint every salon's number red
at once.

**The gate is `shops.plan_id IS NOT NULL`, not a new 'gratis' plan row** —
free is the *absence* of a plan, matching how Gratis already works
elsewhere in the webapp (see that repo's `decisions.md`). This is the
**first** subscription gate in the webapp; gating is otherwise entirely by
vertical bundle and role — deliberately kept to one predicate
(`hasActiveSubscription`), no tier concept introduced.

**`send_push` is a stub that only logs** (`clients/push_notifications.py`,
unchanged by this work — flagged since 2026-07-24 below). Events
(`number_request_approved`/`number_request_rejected`) are still emitted for
consistency with the existing call-lifecycle/balance-alert emitters, so they
become real the day that stub is wired — nobody is actually notified today;
the salon learns the outcome by opening Inbox.

**Flagged, not actioned:**
- **Cancellation gap.** When a subscription lapses, the webapp's
  `process-event.ts` nulls `plan_id` but nothing releases the shop's number
  — it keeps costing ~$3/mo with no one paying for it. A product decision
  (grace period vs. release vs. bill separately), not a provisioning defect.
- **Crash-between-purchase-and-insert gap.** If the process dies between
  `purchase_number` and `insert_telephony` succeeding, the number is bought
  Twilio-side with no local row and no release path — the existing
  lost-race release only fires when the *insert* loses, not when the
  process never reaches it. Needs a reconciliation job; not built.
- `business_app_core.customers.marketing_consent*` columns appear in no
  migration in this repo — they arrive via the webapp's own chain. Noted in
  `docs/knowledge/database.md` so this doesn't get re-diagnosed as a missing
  migration.

**Docs:** `docs/knowledge/architecture.md` (new "Self-service number
provisioning" section), `database.md` (`number_requests` table,
`shop_telephony` health columns), `providers.md` (Twilio section — the two
coexisting bundle models, the regulation/evaluation gotchas), and a new
`docs/knowledge/api/number-provisioning.md` page (linked from `api/README.md`
and `_sidebar.md`). `docs/number-provisioning-design.md` (the working design
doc this entry and those pages were written from) is deleted as of this
change — superseded by the shipped docs above.

---
## 2026-08-12 — Phase 1 messaging: SMS marketing send (schema, send/inbound/status, fail-closed billing)

**Decision:** first shipped piece of a larger SMS/WhatsApp messaging design
(full draft: `docs/messaging-design.md`, a working document deleted once the
whole design ships — this entry plus `docs/knowledge/{architecture,database,
providers}.md` and `docs/knowledge/api/sms.md` are the durable record).
Phase 1: a salon owner can send one personalised marketing SMS to one
consenting customer, from the shop's own Twilio DID, billed as AI credits.
New `sms` schema (`booking_engine/db/sql/11_sms_schema.sql`:
`campaigns`/`outbound_messages`/`opt_outs`), new package
`booking_engine/services/messaging/` (`gsm7.py`, `send_credits.py`,
`sms_send.py`, `sms_inbound.py`), new routes `POST /api/v1/sms/send` +
`/sms/webhook/{inbound,status}`. Companion webapp change (own commits, own
repo): Marketing's Customers tab renamed "Salute Clienti" → "Touchpoint
Clienti", and its existing win-back-copy generator modal gained an "Invia
SMS" button.

**SMS for marketing, not WhatsApp.** WhatsApp forbids free-form
business-initiated messages outside a 24h customer-initiated session — only
pre-approved templates, which can't carry the per-customer LLM-generated
copy this feature exists to send. SMS has no such restriction. (WhatsApp
booking/reminders are a later phase of the same design, not built yet.)

**Billing: 2× Twilio cost via AI credits, through a dedicated converter —
not the webapp's `rawToUserCredits()`.**
`booking_engine/services/messaging/send_credits.py` implements
`ceil(twilio_usd * 2 * 1000)`. Deliberately not the webapp's existing
10×-margin, floor-at-1 converter: 10× is the LLM margin, wrong for a
pass-through send cost; a floor of 1 would charge a credit for a free
WhatsApp service message once that phase ships, quietly inverting the
"customer speaks first, and it's free" economics that phase depends on.

**Credits are debited after Twilio accepts the send, not before —
reversed mid-implementation (commit `4883814`).** The first cut checked
the balance and debited before calling Twilio; a Twilio-side rejection then
left the shop billed for a message that was never sent, on a call that cost
Kairo nothing. Fixed to check balance → send → debit only after a
successful Twilio accept, logging (not blocking) the narrow case where the
balance drains in that window — sends are owner-triggered and effectively
serial today, so this is an accepted race, not a real gap.
`booking_engine/db/token_basket_queries.py::try_debit_for_message` is
fail-closed either way: unlike the voice path's `insert_debit_event`
(drains-and-proceeds, because a live call can't be un-answered), a message
that can't be billed is never sent — the row is marked
`suppressed_reason='insufficient_credits'` instead.

**Consent lives in `business_app_core.customers`, the single source of
truth shared with the webapp; `sms.opt_outs` is the suppression list of
last resort.** A STOP reply must be honoured even from a phone number
matching no `customers` row (import, wrong number, deleted customer) —
nothing in `business_app_core` to flip in that case, hence the standalone
table. When the phone *does* match a customer,
`services/messaging/sms_inbound.py` writes **both**: the `opt_outs` row
(unconditional) and `customers.marketing_consent = false` (keeps the
webapp's own consent UI honest, since it reads `business_app_core`
directly). `opt_outs` also doubles as the legal evidence trail for the
Garante.

**The opt-out footer is appended server-side, never left to the LLM.**
Legally required on every Italian marketing SMS
(`" Rispondi STOP per non ricevere piu'."`). `sms_send.py` appends it
before sanitizing/encoding, so the segment count and any consent-suppressed
row both reflect the real wire text.

**Twilio's automatic STOP handling doesn't cover this number.** It's
US/Canada-long-code-only; the Estonian DID (2026-07-16 decision) gets none
of it, so `sms_inbound.py` reimplements STOP parsing as application code —
whole-message match against an IT/EN keyword list (`stop`, `alt`,
`cancella`, `basta`, `disiscrivimi`, … — widened in `4883814` after review),
never a substring match, so "non fermatevi, stop mai!" is not misread as an
opt-out.

**Generation and sending are two separate charges.** The webapp's existing
`retention-message` route already billed generation at 10× LLM cost; this
adds a second, independent charge for the send at 2× Twilio. A regenerate
costs only the former, "Invia SMS" only the latter.

**The webapp never deducts credits for a send — one debit path, full
stop.** `POST /api/v1/hair-salon/customers/[id]/send-sms` (webapp)
re-checks consent and forwards to this repo's `/api/v1/sms/send`; only
`sms_send.py` here ever calls `try_debit_for_message`. Two debit paths for
the same charge would eventually double-charge or drift.

**`/sms/send` is synchronous**, unlike the tick-based batch/reminder
mechanism the rest of the messaging design calls for (not built this
phase) — the caller is a salon owner watching a modal, who needs "Inviato"
or "Credito insufficiente" now, not within the hour.

**Segment/encoding counting is intentionally duplicated, not shared,
across the two repos.** `booking_engine/services/messaging/gsm7.py`
(authoritative, at send time) and the webapp's
`src/lib/messaging/sms-preview.ts` (a pre-click cost preview shown before
the owner clicks "Invia") independently implement the same GSM-7
septet/segment logic — the alternative is a network round-trip on every
keystroke while the owner is still typing. Small drift between the two is
accepted; a real send always goes through `gsm7.py`.

**Twilio signature verification now has one implementation for three
routes.** Extracted `_twilio_signature_valid` out of `voice_twiml.py` into
`booking_engine/services/twilio_signature.py`; the TwiML webhook and both
new SMS webhooks all call it — matching the 2026-07-16 entry's own
"one verifier, not two, or they drift" precedent from the Telnyx→Twilio
migration. Still a no-op (accepts unsigned requests) if `TWILIO_AUTH_TOKEN`
is unset — same known gap as before, not introduced or closed by this
change.

**Found in passing, not fixed here (out of this task's scope):**
`business_app_core.customers.marketing_consent`/`_granted_at`/
`_withdrawn_at`/`_source` — read directly by `sms_send.py`'s consent gate —
exist on the live database but appear in **no migration file inside this
repo**; they were added through the webapp's own migration chain. A
contributor grepping only this repo for those columns would wrongly
conclude they don't exist. Documented in `database.md`.

**Still needed:** live-DB verification (`tests/live_db/test_sms_live.py`)
and one real SMS sent to a real handset — both still open per
`docs/messaging-plan-phase1.md` Task 15 (not run in this environment,
`TEST_DATABASE_URL`/live Twilio not available here). WhatsApp (reminders +
self-booking), campaign batches (`sms.campaigns` exists, nothing writes it
yet), and number provisioning are later phases of the same design, not
started.

## 2026-07-24 — CI: migration ownership moved to the webapp repo (logged retroactively 2026-08-10)

**Recorded after the fact.** Commit `4124013` ("Updated CI") shipped this change
without an entry here or a matching `docs/knowledge/` update — found during a
2026-08-10 doc-alignment pass, when `operations.md`/`providers.md` still
described the superseded behaviour. The entry is dated to the change, not to
when it was written down.

**Decision:** this repo no longer applies migrations to the real QA or
production Neon branches. `deploy-qa.yml`'s `promote-qa` job and
`deploy-fly-prod.yml`'s `migrate-prod` job were both replaced by a single
`migrate-via-webapp` job that dispatches `kairo-smb/webapp`'s
`migrate-qa.yml`/`migrate-prod.yml` (via `benc-uk/workflow-dispatch@v1`,
`wait-for-completion`, 20 min timeout) and blocks the Fly deploy on it. The
QA-branch restore-from-production step moved there too.

**Why:** the shared Neon DB now has three schemas with a required application
order (`business_app_core` → `voice_agent` → `market_intel`) owned by three
repos. Each repo migrating its own schema independently means the order is
whatever the CI race happens to produce. Making `webapp` the single parent that
applies all three keeps the order deterministic and guarantees this service
never deploys ahead of its schema.

**Unchanged:** the ephemeral-branch validation from the 2026-07-18 entry below
still runs first, identically, in all three workflows — a throwaway
copy-on-write branch off production, migrated + seeded + `live_db`-tested, then
deleted. That remains the only Neon branch this repo migrates itself, and it
still gates the release. `scripts/migrate.sh` is unchanged and still the local
path.

**New GitHub Actions secret:** `WEBAPP_MIGRATE_DISPATCH_TOKEN` — needs
`actions:write` on `kairo-smb/webapp`. It is a CI secret, not a Fly app secret;
without it neither environment can deploy at all, since the deploy job now
depends on the dispatch job.

## 2026-07-24 — Repo cleanup: deleted dead docs/scripts, rewrote two stale docs, closed a dependency drift

**What was removed (confirmed dead first, not guessed):** `docs/DEPLOY_VOICE_GATEWAY_LIFECYCLE.md`
and `scripts/deploy-voice.sh` both described/deployed a standalone `voice_gateway/` Fly
service — confirmed via `find`/`grep` that no such package exists anywhere in this repo
(only a same-named `tests/voice_gateway/` directory survives, testing today's
`booking_engine/services/*`, a naming holdover from before that package was folded in).
`CODE_REVIEW_VOICE_AGENT_TOOLS_AND_IDENTITY.md` (a frozen 2026-06-07 review snapshot) and
`docs/DEPLOY_READINESS_BRIEF.md` (called the architecture-divergence issue "open" — it was
resolved months ago — and recommended the since-removed AWS Lambda deploy path) were both
fully superseded by this file. `docs/voice/tone-validation-report.md` was a QA checklist
template that was never filled in (no reviewer, no date, every box unchecked). Also deleted,
per owner decision: all 24 files under `docs/superpowers/specs/` and `docs/superpowers/plans/`
— one design-doc/plan pair per already-shipped feature back to 2026-03-25, every outcome of
which already has a definitive account in this file; owner confirmed this file is meant to be
the durable history, those were disposable working documents from the planning process.

**What was rewritten, not deleted, because it's still load-bearing:** `README.md` described
`voice_gateway/` as a live separate service with its own `Dockerfile`/`config.py` and gave run
commands (`uvicorn voice_gateway.api.app:create_app`) that fail outright — rewritten to
describe the actual single-service `booking_engine` + Fly + Twilio + OpenAI-native-SIP shape.
`docs/INTEGRATION_GUIDE.md`'s "Shared Database Schema" section hand-copied `business_app_core`
table definitions that had already gone stale and wrong at least twice before this (the
2026-07-16 and 2026-07-21 entries below each root-caused a real bug back to exactly this
pattern — a doc's copied schema silently diverging from live Neon). Rather than re-copy a
"corrected" schema I can't fully verify without live DB access (same mistake, just newer),
replaced that section with a pointer to the actual sources of truth
(`information_schema` on the live branch, or `booking_engine/db/queries.py`, which is
exercised by `tests/live_db/*`) and an explicit note of why hand-copying schema into docs
is the anti-pattern to avoid here. Also fixed its deployment-topology diagram (was still AWS
Lambda + a "Control Plane (TBD)" — the webapp Control Plane has existed and shipped features
for months per multiple entries below) and its voice-config endpoint table (paths didn't match
what's actually mounted in `booking_engine/api/app.py`).

**Also fixed:** root `requirements.txt` (used for local dev/test) was missing `mcp` — present
only in `booking_engine/requirements.txt` (what Docker actually installs) — meaning a fresh
local `pip install -r requirements.txt` would fail to import `booking_engine.mcp_server`
(mounted at app startup, not conditionally). Added it. Left three source comments
(`booking_engine/config.py`, `booking_engine/db/sql/09_shop_telephony_twilio_provider.sql`,
`booking_engine/services/call_supervisor.py`) that pointed at now-deleted spec files —
repointed each at the relevant entry in this file instead of leaving a dead path.

**Verification:** full non-`live_db` suite still green after all changes (306 passed, same
5 pre-existing `test_voice_twiml_webhook.py` failures confirmed present on a clean checkout
via `git stash` — a `TWILIO_AUTH_TOKEN`/signature environment issue unrelated to this cleanup,
left alone as out of scope). Confirmed nothing else in tracked files still referenced any
deleted path (`git grep` for each deleted filename came back clean).

**Not touched, flagged only:** `tests/voice_gateway/` keeps its pre-rename directory name
even though the `voice_gateway/` source package is long gone — cosmetic, low-value rename,
left alone. Root vs `booking_engine/requirements.txt` remain two separate files with
overlapping-but-different contents (root is dev/test superset, `booking_engine/`'s is what
actually ships) — drift-prone by construction, but consolidating them wasn't asked for and
touches the Docker build; left as a known seam, not a cleanup target this pass.

## 2026-07-24 — Root-caused session "dead air": tool calls were self-proxying over real HTTPS

**Finding (from real QA Fly logs + a call-graph trace, not a guess):** a live
SIP test call's access log showed every `/voice/tools/{name}` route
responding in under a second on its own, yet 20-36s gaps between successive
tool invocations across the session — pointing at per-call overhead rather
than DB query time. Traced the call graph from `mcp_server.py::_call_tool`
down: `execute_tool()` was invoked with `base_url=settings.public_base_url`,
meaning that on **every single tool call** (all 12 tools, not just the
ATTESA-gated ones), the app made a real outbound HTTPS request to its own
public Fly URL to reach a route defined in the exact same running process —
`/mcp` and `/voice/tools/*` are mounted on the same `FastAPI` app
(`booking_engine/api/app.py`). Worse, `execute_tool` opens a fresh
`httpx.AsyncClient` per call (no connection pooling across calls), so this
paid a full TCP+TLS handshake, every time, on top of whatever Fly's
edge/proxy hairpin routing added to reach "itself." This is architecture-wide
dead air, not a one-tool problem — it explains why the whole session felt
uniformly sluggish rather than one specific tool being slow.

**Fix:** `execute_tool()` already had an in-process fast path
(`ASGITransport(app=app)`) — every existing test already used it, production
never did. `booking_engine/mcp_server.py` now holds a module-level reference
to the live app (`set_app()`, called once from `create_app()` right after
`app.mount("/mcp", mcp_asgi)`), and `_call_tool` passes `app=_app_ref`
instead of `base_url=`. Tool dispatch is now a direct in-process ASGI call —
same code path tests already exercised, zero new dependency, no behavior
change to auth/constraint logic (still the same `/voice/tools/{name}`
handlers). `settings.public_base_url` is untouched elsewhere (still used to
build the URL OpenAI dials for `/mcp/` itself).

**Not chased further (no evidence either way):** the same log trace showed
`get_services` called twice, 2s apart, in one session. Access logs have no
request bodies, so there's no way to tell from this evidence alone whether
that's a legitimate filter-refinement follow-up call or a duplicate — flagged
for awareness, not treated as a bug.

**Follow-up caught by asking "will this hold up under real concurrency?"
before shipping:** `ASGITransport` has no built-in timeout, unlike the real
HTTP transport it replaced — `httpx.AsyncClient`'s default 5s timeout had
been an *incidental* safety net for a stuck downstream call, and removing
the HTTP hop silently removed it too. Under real concurrent load (many
phone calls competing for the DB pool at once — see watch-item below) a
genuinely stuck call would previously abort after ~5s; after this fix, as
first written, it would have hung that tool call — and that phone call —
forever, with nothing to recover it. Closed by wrapping the dispatch in
`asyncio.wait_for(..., timeout=TOOL_CALL_TIMEOUT_SECONDS)` (10s,
`booking_engine/services/mcp_tools.py`), returning a clean
`{"ok": false, "error": "tool_timeout"}` instead. Test forces a stuck
downstream call and asserts the clean timeout rather than a hang.

**Flagged, not actioned — next scaling knob to check:** `pool_max_size=10`
on the asyncpg pool (`booking_engine/db/connection.py`) and the QA Fly
machine's `concurrency.hard_limit=250` requests are unrelated to this fix
and unchanged by it, but worth knowing about together: `pool.acquire()` has
no timeout configured anywhere in this codebase, so if concurrent call
volume ever exceeds ~10 phone calls simultaneously mid-tool-call, the 11th+
would queue for a connection with no bound — the same
`TOOL_CALL_TIMEOUT_SECONDS` wait_for above would still catch that particular
hang, but the pool itself would still be a real contention point worth
sizing deliberately once there's real traffic data to size it against. Not
urgent now — Twilio is still unfunded, no live call volume yet — but the
first thing to look at if "many calls in parallel" ever stops being
hypothetical.

**Bundled into the same commit** (per-owner instruction, matching the
established pattern this session of committing verified parallel work
together): an unrelated, already-complete, already-tested change from a
separate concurrent effort — `SIP_TEST_FALLBACK_SHOP_ID` (`booking_engine/config.py`,
`voice_openai.py`) lets a raw softphone SIP test call route to a fixed shop
when there's no `X-Shop-Id` header (Twilio normally adds that header when
proxying; a bare SIP client dialing OpenAI directly has no such translation
layer). QA-only (empty by default; production calls with no shop id are
still rejected as unroutable). Pairs with the already-committed
`scripts/run_sip_test.sh` / `scripts/print_sip_test_uri.py`.

## 2026-07-21 — Reviewed voice-config WIP commit; found and closed a missing-migration gap for tone_id

**Context:** commit `1c45663` ("wip: remove voice preset/language fields from
voice config") was committed earlier the same day marked "full review still
pending" — removing the legacy `shops.welcome_message/tone_instructions/
personality/special_instructions` columns and the old
`/shops/{id}/voice/config` endpoints in favor of `voice_agent.shop_config` +
`voice_agent.voice_tones`. This entry is that review.

**The core removal was correct** — endpoints, field mapping, and
`voice_preset` vocabulary (alloy/ash/ballad/coral/echo/sage/shimmer/verse)
all matched the intended shape. Smaller issues fixed alongside: a stray
indentation bug in `prompt_assembler.py`; `docs/DEPLOY_VOICE_AGENT.md`'s
smoke test still curling the removed endpoint; added the missing
`GET /voice/config/tones` route (the DB query `list_preset_tones()` existed,
nothing routed to it — needed for the webapp tone picker).

**The real finding: `tone_id` had no migration behind it and would have
crashed on first use.** `voice_config.py`'s `ConfigPatch`/`_PATCHABLE_FIELDS`
and `prompt_assembler.py` already assumed `shop_config.tone_id` (a UUID FK
into `voice_agent.voice_tones`) existed. It didn't — migration 04 only ever
created `shop_config.tone_preset text DEFAULT 'warm'`, and no `voice_tones`
table was ever defined in any committed migration. `upsert_config` builds
its SQL from whatever field names it's given, so any PATCH setting `tone_id`
would fail with "column does not exist"; reads would always silently return
`None` and fall back to the default tone. This was invisible because the
only tests for it (`test_migration_06.py`, `test_voice_tones_db.py` — a
fully-specified spec for exactly this gap, evidently written earlier and
never acted on) require a live `DATABASE_URL` and had been skipping locally
this whole time.

**Fixed by writing the missing `06_voice_tones.sql`** to match those tests'
exact expectations (8 seeded presets: professionale, amichevole, efficiente,
luxury, tecnico, casual, empatico, conciso). Also added
`10_shop_config_voice_preset_default.sql` (the `voice_preset` column default
was still the pre-rename `'warm_female'`).

**Before writing migration 06 against real Neon, checked the actual QA
schema first — good thing, because it had already drifted.** Someone had
previously added `voice_tones` + `shop_config.tone_id` directly against QA
(presumably via Neon MCP, same as this session), seeded with the correct 8
presets, but as a **plain full `UNIQUE(name)` index**, not the partial
`UNIQUE(name) WHERE is_preset` index this migration file originally assumed.
Running the file as originally written would have hit the exact
"no unique or exclusion constraint matching the ON CONFLICT specification"
bug class recorded in the 2026-07-18 entry below — caught before running
anything by inspecting `information_schema`/`pg_indexes` on QA first, not by
trial and error against production data. Rewrote the migration to match the
already-live shape (full unique index, `description NOT NULL`, `is_preset
DEFAULT true`) instead of imposing a redundant second index. Verified
against a local Postgres both fresh and in a reconstructed copy of QA's
exact drifted state (table pre-existing, `tone_preset` not yet dropped)
before touching real data.

**Applied to the real QA branch** (`br-damp-recipe-agnys6xk`): confirmed
`tone_preset` dropped, `tone_id` intact on the one existing shop_config row
(pointing at a real tone someone had already set manually), preset count
still exactly 8 (no duplicates from re-seeding), `voice_preset` backfilled
from `warm_female` to `verse`.

**Companion webapp changes** (separate repo, own commit): swapped all
callers off the removed endpoint, fixed `VoiceShopConfig`'s type (was still
declaring the pre-rename `voice_preset` enum and a fictional `tone_preset`
field — the Settings tab's voice dropdown and tone picker were both
non-functional), added the tone picker UI, converged a second dead
duplicate of the config UI (the Inbox "Configuration" tab, independently
broken the same way) onto the same working component, and fixed the
onboarding wizard writing its welcome-message step into the now-dead
`shops` columns instead of `shop_config.greeting_after_disclosure`.

## 2026-07-21 — SIP call supervisor: production fix for the mute-after-MCP blocker (built)

**Supersedes the status of the earlier 2026-07-21 "Realtime + hosted MCP does
NOT auto-speak tool results" entry below**, which recorded production as
blocked and the fix as "not built here — needs its own design." It is now
built and merged to `QA` (flag-gated off). That entry stays as the record of
the diagnosis; this one records the fix.

**Decision:** Added `booking_engine/services/call_supervisor.py` — a per-call
**server-side Realtime control WebSocket**. On SIP accept, `voice_openai.py`
calls `maybe_supervise(call_id, settings)`, which spawns an `asyncio` task that
opens `wss://api.openai.com/v1/realtime?call_id=...` (confirmed mechanism: after
`/accept` you may connect a control WS keyed by `call_id` and send/receive
events), sends `response.create` to greet, and sends another after each
`mcp_call` completes so the agent voices tool results instead of going mute.
This is the server-side equivalent of the harness data-channel loop. It also
fixes a **second** prod gap the same way: nothing previously triggered the
opening greeting on a real call either.

**Why a WS worker and not a config flag:** OpenAI's Realtime hosted-MCP does
not auto-continue after a tool result (proven from a live event trace —
`response.done` fires before the tool returns, then the result item is orphaned
with no successor response), and no session setting is known to change that.
The SIP accept path is fire-and-forget with no connection to the session, so
the only place to inject `response.create` is a control WS we open ourselves.

**Design choices worth knowing:**
- Pure `decide(event, state)` core (greet/nudge/dedup) is the unit-testable
  heart; `supervise()` is thin async glue with an injectable `connect=` seam so
  tests use a fake WS (no live OpenAI). A `nudge_pending` guard triggers exactly
  one `response.create` per tool result — a single event type
  (`response.output_item.done`, not also `mcp_call.completed`) plus the guard
  removes both double-nudge sources (parallel tools, duplicate events).
- Best-effort + isolated: call audio is OpenAI↔Twilio, independent of this WS,
  so a worker crash degrades only that call (no greeting/nudge), never drops it.
  One reconnect on drop; `greeted` prevents re-greeting on reconnect.
- Per-call structured stdout logging (one JSON line/event, with tool
  `latency_ms`) for `fly logs` debugging.
- **Gated behind `ENABLE_CALL_SUPERVISOR` (default False)** — prod path
  unchanged until flipped.

**Caught by review before merge (subagent-driven development, final
whole-branch review):** the fire-and-forget `asyncio.create_task` return value
was originally un-retained — asyncio holds only a *weak* reference, so the
supervisor task could be garbage-collected mid-call (intermittently
reintroducing the exact mute/no-greeting bug it fixes). Fixed with a
module-level `set()` + `add_done_callback(discard)`. Also added the
reconnect / no-re-greet / give-up / non-JSON-frame tests that the first cut
lacked (18 supervisor tests total).

**Still needed before enabling the flag:** only observable with live
telephony — do the manual QA SIP-call check (confirm a `supervisor.greeted`
line in `fly logs`, a greeting, and post-tool speech with `latency_ms`), then
enable in prod. Deferred non-goals: barge-in/turn handling, post-call outcome
capture from the event stream, DB event persistence. The `QA` merge commit is
local as of this writing (not pushed to `origin`). Still no live inbound calls
(Twilio unfunded).

**Also fixed in passing:** the earlier trailing-slash fix (commit `f7363c2`)
had left `tests/voice_gateway/test_voice_test_server.py` asserting the old
`/mcp` URL; updated to `/mcp/`.

- Spec: `docs/superpowers/specs/2026-07-21-sip-call-supervisor-design.md`.
  Plan: `docs/superpowers/plans/2026-07-21-sip-call-supervisor.md`.
- Built in worktree `.worktrees/sip-call-supervisor`, branch
  `feat/sip-call-supervisor`, merged to `QA` (`--no-ff`).

## 2026-07-21 — Cost-gated pricing + multi-service/multi-staff bookings

**Decision:** Two voice-tool refinements, built together (subagent-driven
development, spec + code-quality review per task, 9 tasks): (1) `get_services`
now only returns `price_cents` when the caller passes `include_price=true`
(default omitted) — a `SAFETY_PROMPT` rule tells the model to set that flag
only when the customer explicitly asks about cost, never to volunteer it.
(2) `check_availability`/`create_booking` now take an ordered list of
`services`/`legs` (`{service_id, staff_id?}`) instead of a single
`service_id`/`staff_id`, so a visit can require multiple services performed
by different staff in sequence (e.g. colore by one stylist, piega by
another) — a plain single-service booking is just a one-element list, no
separate code path.

**Why additive, not a rewrite of the existing single-service functions:**
`booking_engine/db/queries.py::get_available_slots`/`create_appointment`
are shared ground-truth functions also used by
`booking_engine/api/routes/availability.py`/`appointments.py`, outside the
voice-agent's scope. Rather than change their signatures (which would have
touched unrelated callers and their tests), added new, additive functions —
`get_available_slot_chains` (multi-leg chain search, recursive first-fit
backtracking across staff×time×legs) and `create_appointment_chain`
(multi-leg write: one `appointments` row + one `appointment_services` row
per leg, each with its own `staff_id`/`start_time`, matching what the schema
already supports — `appointment_services.staff_id`/`start_time` existed
before this work but nothing in the voice layer used them). The wrapper
layer (`voice_tool_queries.py::find_availability`/`insert_booking_locked`)
picks single-leg (reuses the untouched original functions, zero behavior
change) vs. multi-leg (routes to the new chain functions) based on request
size — confirmed byte-for-byte via MD5 hash that `get_available_slots` has
zero diff from before this work.

**Gap constant:** `MAX_GAP_MINUTES = 20` (`booking_engine/services/booking_constraints.py`)
— the max idle time allowed between the end of one service and the start of
the next in a chain. Fixed, not per-shop configurable; nothing has asked for
it to vary.

**Ordering is the model's own domain knowledge, not a stored rule:** no
service-dependency/ordering table exists in the schema, and none was added.
`SAFETY_PROMPT` instead tells the model to sequence multi-service requests
by hairdressing convention (color/chemical treatments before cut/styling)
unless the customer states a different order — the system enforces
whatever order the `services`/`legs` list arrives in, it doesn't know *why*
a given order is correct.

**Bugs caught by review before merge, fixed same-session:**
- `get_available_slot_chains` originally compared a deduplicated
  services-found count against a non-deduplicated requested-id count,
  misreporting a chain that legitimately repeats the same `service_id`
  twice (different staff) as "unknown service."
- Missing a final sort of returned chains by `slot_start` — the spec had
  promised "sorted by proximity to preferred_when" but the first cut never
  sorted before returning, so a later chain from one staff member could come
  back ahead of an earlier one from another.
- The chain-extension search only tried the single earliest candidate start
  time per staff/window; if that exact instant conflicted with an existing
  appointment, the search abandoned that staff/window entirely instead of
  trying later starts still within `MAX_GAP_MINUTES` — could report "no
  availability" when a valid slot existed. Fixed to step through the full
  gap-allowed window in 5-minute increments.
- `create_appointment_chain` had an unguarded dict lookup that would
  `KeyError` (crashing the tool call) if a leg's service became
  inactive/missing between the availability check and the write — same bug
  class as the 2026-07-17 FK-crash entry below. Now raises a clean
  `RuntimeError("invalid_service")` instead, propagating through to a
  `{"ok": false, "error": "invalid_service"}` tool response.
- `insert_booking_locked` initially added an unconditional extra DB
  round-trip (re-fetching durations) for every booking, including the
  dominant single-service case, which previously needed zero extra queries.
  Fixed so single-leg bookings read `start_time`/`end_time` straight off
  what `create_appointment` already returns, matching pre-existing
  behavior; only multi-leg bookings still pay for the re-fetch (necessary,
  since `create_appointment_chain` only returns the parent `appointments`
  row, not a per-leg breakdown).
- Found by a final whole-branch review (not per-task review — only visible
  once the pieces were viewed together): `get_available_slot_chains`'s
  search broke out the instant it collected `max_results` chains, but
  candidate generation is staff-major within a day (exhausts one staff's
  whole day before trying the next, no `ORDER BY` on eligible staff), so a
  later slot from the first-iterated staff member could be returned instead
  of a genuinely earlier slot from a staff member iterated later — the
  earlier "add a final sort" fix above only reordered whatever had already
  been collected, it couldn't fix a search that stopped too early. Reproduced
  concretely (two eligible staff, first busy until 14:00, second free from
  09:00 — search returned the 14:00 slot). Fixed so the search only stops at
  a day boundary, never mid-day (since candidates within one day aren't
  time-ordered across staff, but candidates across different days always
  are) — this fully closes the "sorted by proximity" promise the earlier fix
  only partially delivered on. Regression test added with 2+ eligible staff
  for the first leg specifically, since none of the existing tests exercised
  that case.

**Flagged, not actioned:** `create_appointment_chain` validates each leg's
staff against *existing* DB rows but never validates the legs in one request
*against each other* — nothing stops a `create_booking` call (if the model
sent a fabricated `legs` array instead of copying a real `check_availability`
result verbatim) from assigning the same staff member to two overlapping
legs in the same request, which would write two conflicting
`appointment_services` rows. The single-leg path has an equivalent
"trust what's given, no re-validation beyond one overlap check" limitation
today; this just extends the same accepted risk shape to N legs. Not fixed
here — same reasoning as `update_customer_from_call`'s missing shop check in
the 2026-07-17 entry below (a policy/validation decision, not a narrow
error-handling fix); worth a fast-follow (pairwise leg overlap + ordering +
`gap_within_limit` check before the insert loop) given this codebase already
treats voice-agent tool arguments as untrusted input elsewhere.

**Still needed:** `tests/live_db/*` (the tests that exercise the real
dispatch chain against a real Neon-shaped DB, no mocking) were updated for
the new wire shape but — per this repo's existing convention — could not be
run in this environment (`DATABASE_URL` not set here). The chain algorithm
itself is currently only verified by mocked unit tests
(`tests/booking_engine/test_queries.py`); run the `live_db` suite against
the QA Neon branch to confirm `get_available_slot_chains`/
`create_appointment_chain` behave correctly against real staff schedules
and real overlapping-appointment data before treating this as fully proven
end-to-end.
- Spec: `docs/superpowers/specs/2026-07-21-cost-gating-multi-staff-booking-design.md`.
  Plan: `docs/superpowers/plans/2026-07-21-cost-gating-multi-staff-booking.md`.
- Built in worktree `.worktrees/cost-gating-multi-staff-booking`, branch
  `feat/cost-gating-multi-staff-booking`.

## 2026-07-21 — Realtime + hosted MCP does NOT auto-speak tool results (prod blocker)

**Finding (from a live harness event trace, not docs):** in the Realtime API
with a hosted `mcp` tool, after the model emits a tool call its response
**ends** (`response.done` fires *before* the tool even returns), the tool
executes server-side, `response.mcp_call.completed` + `response.output_item.done`
deliver the result — and then **nothing**. OpenAI does not open a new response
to voice the result, so the agent goes silent after every fetch. This
contradicts the Responses-API "hosted MCP auto-continues" behaviour and the
assumption recorded in the 2026-07-21 trailing-slash entry's follow-up; the
raw event log (`response.done` at output_index 0, then an orphaned
`mcp_call.completed` at index 1 with no successor response) disproves it.

**Harness fix (shipped):** `voice_test_static/index.html` now tracks
`responseActive` (`response.created`→true, `response.done`→false) and, on
`response.output_item.done` for an `mcp_call`, sends `{"type":"response.create"}`
after a 100 ms guard when no response is active — nudging the model to speak
the result (or chain the next tool). This is the standard Realtime tool-result
pattern we were simply never sending.

**Production is NOT fixed and is blocked by this:** the SIP path
(`voice_openai.py` → `accept_sip_call`) is fire-and-forget — it POSTs the accept
config to `/v1/realtime/calls/{id}/accept` and holds **no** websocket/event
connection to the session, so there is no client to send `response.create`.
Before real calls go live, production needs either (a) a server-side control
WebSocket to each realtime call that injects `response.create` on tool
completion, or (b) an OpenAI session/config mechanism that makes hosted-MCP
tool results auto-continue (unverified one exists). Not built here — needs its
own design. Not urgent only because Twilio is still unfunded (no live inbound
calls yet).

## 2026-07-21 — MCP server_url must carry a trailing slash (prod + harness)

**Decision:** Point every OpenAI Realtime `mcp` tool `server_url` at `/mcp/`
(trailing slash), not `/mcp`, in both the production SIP path
(`voice_openai.py`) and the local test harness (`voice_test_server.py`).

**Why:** `app.mount("/mcp", mcp_asgi)` makes Starlette 307-redirect `/mcp` →
`/mcp/`. Root-caused from the QA Fly logs (`fly logs -a
kairo-booking-engine-qa`): during a harness call, OpenAI's Realtime MCP client
POSTed to bare `/mcp`, got `307`, and **never followed the redirect** — three
tool attempts in one call all showed as `307` with no subsequent `/mcp/ 200`
or `/voice/tools/*`. So the tool never executed; the model narrated "verifico
subito le disponibilità…" and then hung waiting for data that never came
(looked like "MCP idle + never returns to the customer"). Direct probes to
`/mcp/` return `200 ok:true`, confirming the redirect — not auth, not the DB —
was the sole failure. Note this contradicts an earlier assumption (recorded
then disproven here) that OpenAI follows the 307; it does not for the tool-call
POST body. Production had the identical bug on line 68 of `voice_openai.py`;
fixed there too even though real SIP calls aren't live yet (Twilio unfunded).

**Also:** hardened the harness event log (`voice_test_static/index.html`) to
surface MCP handshake failures (`mcp_list_tools.failed`) and a catch-all
firehose (`DEBUG_EVENTS`) of every unhandled Realtime event, so a silently
non-firing tool is visible next time instead of looking like "idle".

## 2026-07-18 — CI/CD: ephemeral Neon branches, seed-data bug fix, Lambda removal

**Decision:** Replace the local-Postgres-container + `pg_dump --schema-only`
CI mechanism with real, throwaway Neon branches (copy-on-write children of
`production`) across all three DB-touching workflows — `ci.yml` (PR checks),
`deploy-qa.yml` (QA release), `deploy-fly-prod.yml` (prod release) — matching
the pattern already in use by the `webapp` repo, which shares this same Neon
project. QA and prod releases now validate migrations + `tests/live_db/`
against a disposable branch *before* ever touching the real QA branch or
production. Also deleted the superseded AWS Lambda deploy path and fixed the
seed-script bug that had silently blocked every CI/QA/prod run since
2026-07-16.

**Why this was needed:** every CI, QA, and prod-release run had been failing
since the Telnyx→Twilio merge, always at the same step:
`ERROR: there is no unique or exclusion constraint matching the ON CONFLICT
specification` on `booking_engine/db/sql/02_seed_data.sql:75`. Root-caused by
querying the real Neon schema directly (not guessing): the local bootstrap
schema (`01_schema.sql`) declares `UNIQUE (staff_id, day_of_week)` on
`staff_schedules`, but the real `business_app_core.staff_schedules` table has
only ever had a plain non-unique index on those columns — the two schemas
had silently diverged, and CI's `db-tests` job was seeding fake fixture data
into a `pg_dump`-cloned copy of the *real* schema, which the seed script was
never written against. Per this file's own 2026-07-16 entry ("shared schemas
are ground truth... revise the schema only if truly impossible"), the fix
is on the query side, not the schema side: `ON CONFLICT (staff_id,
day_of_week) DO NOTHING` → a `WHERE NOT EXISTS (...)` guard, which is
constraint-independent and works identically against both schemas.

**Why ephemeral branches, not just a bug patch:** the failure exposed a
deeper gap — CI was testing against a bare schema clone with fake seed data,
never the real, current shape of the shared `business_app_core` database
that `webapp` and `marketing-engine` also write to. Fixing only the one
broken query would have left that gap open for the next schema drift.
Ephemeral, copy-on-write Neon branches (create → migrate → seed → test →
delete, per run) give CI real prod-shaped data at effectively no cost
(copy-on-write) and no risk (every write lands on a disposable branch,
production is only ever read). This mirrors `webapp/ci.yml`'s existing
PR-time pattern, extended here to also gate the push-triggered QA/prod
release workflows, not just PRs.

**Lambda removal:** `docs/DEPLOY_READINESS_BRIEF.md` had already recorded
"Deploy to Fly.io (decided)" superseding an earlier AWS Lambda plan, but the
Lambda deploy path (`deploy.yml`, `lambda_handler.py`, a Lambda-only
`Dockerfile`, `deploy-booking.sh`, its test, the `mangum` dependency) was
never actually deleted — found and removed as part of this work, since
`deploy.yml` would otherwise have kept firing on every push to `main`
alongside the real Fly deploy.

**Issues caught by review before merge (subagent-driven development, spec +
code-quality review per task):**
- Ephemeral-branch delete failures were originally swallowed with `|| true`
  — silently orphaning branches in the shared Neon project on any transient
  API failure. Changed to emit a visible `::warning::` while still not
  failing the job.
- `deploy-qa.yml`'s ephemeral branch name initially omitted
  `github.run_attempt` (unlike `ci.yml`'s), which would have collided with
  itself on a re-run of a failed workflow. Fixed and backported to
  `deploy-fly-prod.yml` before it shipped with the same gap.
- The Lambda→Fly.io README/deploy-guide rewrite initially overclaimed "both
  services deploy via GitHub Actions" (only Booking Engine does; Voice
  Gateway is manual-only) and implied `CONTROL_PLANE_SECRET` was already
  configured via GitHub Actions secrets when it's actually a Fly app secret
  (`fly secrets set`, separate from CI) — left uncorrected, either could have
  caused a real production 401 or a false assumption about deploy coverage.

**Implementation notes:**
- Spec: `docs/superpowers/specs/2026-07-17-neon-ephemeral-branch-cicd-design.md`.
  Plan: `docs/superpowers/plans/2026-07-17-neon-ephemeral-branch-cicd.md`.
- Built in worktree `.worktrees/neon-ephemeral-branch-cicd`, branch
  `feat/neon-ephemeral-branch-cicd`, PR #4 into `QA`.
- The seed-data fix and the ephemeral-branch CI itself were both verified
  against the real, live Neon project (`kairo`, `falling-bread-89568725`)
  before merge — the seed fix via a throwaway MCP-created branch, tested
  twice for idempotency; the full `ci.yml` flow via two real PR CI runs
  (both green), each provisioning and cleanly deleting a real ephemeral
  branch.

**Still needed:** manually remove now-unused GitHub repo secrets —
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`CONTROL_PLANE_SECRET`, `DEMO_SHOP_ID` (Lambda-only) and
`CI_SCHEMA_SOURCE_URL` (only used by the removed `pg_dump` mechanism) — same
manual, out-of-band cleanup category as the old `TELNYX_*` secrets.

## 2026-07-17 — Live tool-dispatch + security test coverage

**Decision:** Add `tests/live_db/test_tool_dispatch_{reads,writes,security}.py`
— the first tests to exercise the *entire* real dispatch chain
(`execute_tool()` → `/voice/tools/{name}` route → `safety_layer`
authz/constraints → `queries.py`) against real Neon-shaped data, calling
`execute_tool()` directly (the exact function OpenAI's MCP path calls) with
no mocking. 24 new tests: 5 read-tool, 7 write-tool, 12 security (token
integrity, cross-shop authz, phone-mismatch authz, lead-time/past-slot
constraints, unknown-tool-name rejection, malformed/missing-field input,
and the two independently-violable FK scenarios — nonexistent `customer_id`
and nonexistent `staff_id` — each asserted separately).

**Why this gap existed:** `tests/voice_gateway/test_voice_tools_*.py` covers
the route handlers but mocks every DB call; `tests/live_db/*.py` covers the
query layer but calls it directly, below the authz layer. Nothing exercised
`safety_layer`'s authz/constraint logic against a real row or the real
schema — a regression there (e.g. `modify_booking` silently dropping its
phone check) would have passed every existing test. Verified this class of
regression actually gets caught: manually removed the phone-mismatch check
in `booking_authz.py` and confirmed (via a monkeypatch-driven simulation of
the full dispatch path, since the QA branch currently lacks seed data —
see below) that execution then proceeds past the point it should have been
rejected, then restored the check.

**A real production bug was found and fixed along the way, not just a test
gap:** while strengthening a security test for malformed input, discovered
that `create_booking` with a syntactically valid but nonexistent
`customer_id` crashed `execute_tool()` itself with an unhandled
`asyncpg.exceptions.ForeignKeyViolationError`, instead of returning a clean
error — because `insert_booking_locked`
(`booking_engine/db/voice_tool_queries.py`) only caught `SlotConflictError`,
never a customer/staff FK violation from the underlying raw INSERT in
`booking_engine/db/queries.py::create_appointment`. Fixed by catching
`asyncpg.exceptions.ForeignKeyViolationError` alongside the existing
`SlotConflictError` catch, distinguishing `invalid_staff` vs
`invalid_customer` via the exception's `constraint_name` (since both
columns are independently-violable FKs and only `service_id` was already
pre-validated before this call). A stale or hallucinated `customer_id`
reaching this path in production — plausible, since OpenAI supplies tool
arguments — would have crashed a live call's tool invocation before this
fix. Checked the sibling `webapp` repo's own booking-creation code
(`src/lib/db/repositories/appointments.repo.ts`) for the same bug class:
it does NOT share this crash risk — its callers wrap the insert in a
generic catch-all that degrades to a clean 500 (less precise error
messaging, but no crash), and its agent-facing path additionally
pre-validates `customer_id`/`staff_id`/`service_id` against the shop before
ever attempting the insert.

**Two things discovered while writing these tests, worth knowing:**
- `modify_booking`/`cancel_booking` authorize off the **call row's stored
  `shop_id`** (read back via `get_call()`), not the `X-Shop-Id` header the
  MCP dispatch layer sends — cross-shop tests have to insert the call
  itself under the wrong shop, a header alone doesn't reach this check.
- `update_customer_from_call` (`booking_engine/api/routes/voice_tools_identity.py`)
  has **no shop-ownership check at all** — any valid call token can update
  any customer row's `email`/`tags` regardless of which shop the call
  belongs to. Not fixed here (out of scope for a test-coverage plan —
  changing production authz logic is a different, riskier kind of change
  than the narrow FK-crash fix above, which was pure error-handling, not a
  policy decision); flagged as a fast-follow.

**Also flagged, not actioned:** `voice_gateway/` (the old 5-tool, no-authz
agent implementation) is dead code — neither `fly.toml` nor `fly.qa.toml`
build it, only `booking_engine/Dockerfile.fly`. It's still imported by ~6
test files (`tests/voice_gateway/test_call_lifecycle.py`,
`test_db.py`, `test_booking_client.py`, `test_openai_classifier.py`,
`test_realtime_lifecycle.py`, `tests/live_db/test_voice_gateway_persistence.py`),
and the CI workflow files that reference it are the ones another concurrent
effort (the ephemeral-branch-CI spec) is already modifying — deleting it is
a separate follow-up, not part of this work.

**Still needed before this closes the loop end-to-end:** the ephemeral-branch
CI pipeline (separate, in-flight effort — as of this writing, memory records
it's fixed and PR #4 is open into QA, not yet merged) needs to run these
tests in CI. As of this writing the QA Neon branch itself still lacks the
`02_seed_data.sql` fixture rows these tests (and the pre-existing
`tests/live_db/*.py` suite) depend on — every new test currently fails with
`ForeignKeyViolationError` on `shop_id` when run against the QA branch
directly, confirmed not a code defect (verified the failure mode is
exclusively that one exception class). Run locally against the QA branch
via `TEST_DATABASE_URL` once seeded, or wait for the concurrent effort's PR
to merge.
- Spec: `docs/superpowers/specs/2026-07-17-live-tool-dispatch-security-tests-design.md`.
  Plan: `docs/superpowers/plans/2026-07-17-live-tool-dispatch-security-tests.md`.

## 2026-07-16 — Telephony provider: Telnyx → Twilio

**Decision:** Provision Twilio **Estonia (EE) Mobile** numbers as the
Kairo-owned DID for Path 1 (forward) and Path 2 (new) telephony onboarding,
replacing Telnyx Italy local numbers. One Kairo-entity regulatory Bundle,
created once and reused across every shop — no per-salon KYC.

**Why leave Telnyx:** lower onboarding friction was the goal. Turned out
Twilio's Italy catalog has no local/geographic number type at all — only
Mobile at $30/mo — so "provision a mobile number via Twilio" wasn't a
preference, it's the only thing Twilio sells for Italy.

**Why Estonia, not Italy:** the constraint that actually matters for
call-forwarding cost is "within the EU," not "within Italy" — the EU's
intra-member-state calling price cap (capping IT→EU-country calls, vs.
uncapped international rates) applies to any EU member, not just Italy.
Once decoupled from "must be Italian," compared IT/EE/FI/IE/AT/NL/DK:

| Country | Type | Price/mo | KYC address requirement |
|---|---|---|---|
| Italy | Mobile (only option) | $30.00 | anywhere in the world |
| **Estonia** | **Mobile** | **$3.00** | **anywhere in the world** |
| Estonia | Local | $1.00 | anywhere in the world (but not SMS-capable) |
| Finland | Mobile (only option) | $5.00 | must be Finnish (contradicts "no docs" framing) |
| Ireland / Austria / Netherlands | Local | $1-1.60 | must be in-country — new KYC burden, not less |
| Denmark | any type | — | must be Danish for every type — dead end |

Estonia Mobile wins: same friction as Italy Mobile (Kairo's existing Italian
entity docs work directly, no new foreign presence needed), 10x cheaper.

**Why Mobile over Local within Estonia:** Estonia's KYC is identical for
both types (no friction difference), so the deciding factor became SMS
capability — Local numbers aren't SMS-capable in Estonia (true of most EU
geographic ranges), and the plan is to eventually send booking-confirmation
/ opted-in marketing SMS from this number (see below). Local would have
foreclosed that for a $2/mo saving.

**Future SMS/WhatsApp capability (not built yet, deliberately deferred):**
- **Phase 1** (whenever built): send from the raw Twilio number, no
  Alphanumeric Sender ID, salon's name in the message signature/body. Works
  immediately — Italy's Alphanumeric Sender ID pre-registration requirement
  only applies to alphanumeric senders, not plain numeric long codes. Fine
  at small volume (confirmations + ≤10-message opted-in batches) — Italian
  carrier A2P filtering targets bulk/burst campaign patterns, not this.
- **Phase 2** (future, opt-in per shop): a salon can proactively request its
  own registered Alphanumeric Sender ID (its business name as sender).
  Deliberately *not* the default — Twilio confirmed Italy requires
  per-string document registration + carrier vetting for Alphanumeric
  Sender ID, so making every shop's own name the sender would reintroduce
  the per-salon KYC problem this whole design avoids for phone numbers.
- WhatsApp Business messaging is unaffected by any of this — any real
  number can become a WhatsApp sender (Twilio supports voice-OTP
  verification for non-SMS-capable numbers specifically for this case) —
  and is out of scope as its own future feature (Meta business
  verification + template approval, different problem entirely).

**Regulatory bundle model differs from Telnyx's shape:** Telnyx: purchase
first, number goes `pending_review`, webhook flips it active/rejected.
Twilio: the Bundle must be approved *before* a purchase can succeed at all.
Since the Bundle is reused across every DID, that async wait happens once,
at ops setup — not per shop. Every purchase after that is synchronous
(succeeds now or fails outright), so the Telnyx-style async webhook has no
steady-state equivalent and was deleted rather than adapted.

**Implementation notes:**
- The original Twilio client + TwiML webhook existed in this repo before a
  2026-06-06 swap to Telnyx (recoverable from git history at `df9c53a^`) —
  restored and adapted rather than rewritten from scratch.
- Added Twilio `X-Twilio-Signature` request verification — a gap that
  existed under Telnyx too and was never fixed; closed as part of this
  migration rather than carried forward.
- Code review during implementation caught three bugs beyond the original
  plan, all fixed before merge: dead code (`update_telephony_activation`,
  whose only caller was the deleted Telnyx webhook); the provisioned
  `voice_url` was missing its `/api/v1` mount prefix (a bug that predated
  this migration too — every provisioned number's webhook would have 404'd
  in production, so no inbound call would ever have reached the routing
  logic); and `TWILIO_ADDRESS_SID` was read by the provisioning code but
  never wired through CI/deploy-script env vars.
- Spec: `docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md`.
  Plan: `docs/superpowers/plans/2026-07-16-telnyx-to-twilio-migration.md`.
- Merged into `feat/voice-forwarding-overflow`; local `QA` branch reset to
  match and pushed (force-with-lease) to `origin/QA`.

**Still needed before this is live:** fund/verify the Twilio account, create
and get approval for the one Kairo-entity regulatory Bundle in the Twilio
Console (manual, out-of-band, not automatable from this repo), add the
`TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_BUNDLE_SID` /
`TWILIO_ADDRESS_SID` GitHub repo secrets (replacing the old `TELNYX_*` ones).
