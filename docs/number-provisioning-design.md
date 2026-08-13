# Self-service number provisioning — design draft

**Status:** draft, not approved, nothing built.
**Date:** 2026-08-13. Supersedes the first cut of the same day (shared-bundle,
one-button model), which the Twilio ISV rules below invalidate.

> Working document. When this ships the decisions land in `CLAUDE.md` and the
> shipped reality in `docs/knowledge/*.md` (both repos); delete this then.

Spans **webapp** (gate, form, UI) and **voice-booking** (Twilio, storage).

---

## 1. Goal

A salon with a paid subscription requests an Estonian mobile number from Inbox,
supplies the one document Twilio requires, and gets a number once Twilio
approves. Used today for SMS, then WhatsApp; voice later.

**Decided 2026-08-13:** number only (WhatsApp sender registration is its own
increment) · auto-assign the number, no picker · `setup_path` always `'new'`,
forwarding asked at the voice upgrade · **exactly one number per shop, ever** ·
**one regulatory bundle per salon**.

## 2. The regulatory model changed — this is the load-bearing decision

The 2026-07-16 entry chose "one Kairo-entity bundle reused across every shop —
no per-salon KYC". **That is not permissible for reselling.** Twilio's ISV
guidance is explicit: *"Each customer needs their own bundle. Do not reuse your
business information in customer bundles. End-User records must reflect the
actual end-user (your customer), not you. Twilio audits this."*

So the owner is creating a separate provider bundle by hand, and each salon
gets its own bundle built from a form.

### 2.1 What Estonia actually requires — queried, not assumed

`GET /v2/RegulatoryCompliance/Regulations?IsoCountry=EE&NumberType=mobile`
returns exactly one regulation:

- **`RN26dca8d0e541a6c8fce4abd46e518506` — "Estonia: Mobile - Business"**
- `end_user_type: business` — **there is no individual option**; a sole trader
  without a company registration cannot get a number at all.

| Requirement | `requirement_name` | Fields |
|---|---|---|
| End-User, type `business` | `business_info` | `business_name` |
| Supporting document, type `commercial_registrar_excerpt` | `business_name_info` | `business_name` + the file |

That is the whole list: **a business name and one document.** No address, no
VAT number, no personal ID — lighter than the 2026-07-16 research suggested.

**The document is the friction.** "Extract from the commercial register" is an
Italian **visura camerale**. Every salon must obtain and upload one, and Twilio
review takes days. This is the per-salon KYC the earlier design avoided; it is
the price of being the provider, and it sits between "salon subscribes" and
"salon has a number".

**Do not hardcode this table.** Twilio's docs say regulations change and must be
read from the Regulations resource. The implementation queries it at request
time and stores the regulation SID on the request row, so a change surfaces as
an evaluation failure with Twilio's own wording rather than a silent mismatch.

### 2.2 Let Twilio validate, don't reimplement Estonia

`POST /Bundles/{sid}/Evaluations` is **synchronous** and returns field-level
violations. The flow evaluates before submitting and shows Twilio's own
`friendly_name` + `description` back to the salon. We never encode Estonian
rules in our validators — only "is this field non-empty".

## 3. Two existing defects this work must fix

### 3.1 The upsert orphans purchased numbers — the "max 1" bug

`upsert_telephony` ends in `ON CONFLICT (shop_id) DO UPDATE SET kairo_number =
EXCLUDED.kairo_number, ...`. A second provision **buys a second number and
overwrites the row**; the first stays owned and billed by Twilio (~$3/mo) with
nothing referencing it. The primary key guarantees one *row*, not one
*purchase* — that distinction is the bug, and self-service makes it a click
away.

**Fix, in order — the ordering is the fix:**
1. Check `get_telephony(shop_id)` before buying; if present, return it and never
   call Twilio. Idempotent, handles the double-click.
2. Insert-only: `ON CONFLICT (shop_id) DO NOTHING RETURNING *`. `None` means we
   lost a race.
3. On a lost race, `release_number(sid)` the number just bought and return the
   winner. Without step 3 the race leaks exactly the number the fix is about.

`upsert_telephony` stays for legitimate updates (activation status); only the
provisioning path becomes insert-only.

### 3.2 A webapp route that 404s

`src/app/api/v1/hair-salon/voice/numbers/provision/submit/route.ts` POSTs to
`${VOICE_AGENT_API_URL}/voice/numbers/provision/submit`. No `/submit` endpoint
exists — it was the Telnyx-era document upload, deleted in the 2026-07-16
migration. Delete the route; §6 replaces it.

## 4. The subscription gate

**"Tier above gratis" is `shops.plan_id IS NOT NULL`.** There is no `gratis`
plan row — free is the *absence* of a plan.
`billing/webhook/process-event.ts` sets `plan_id` on checkout and nulls it (with
`plan_subscription_id`) on cancellation, so the column already means "currently
paying".

One predicate in `src/lib/billing/subscription-gate.ts`:
`hasActiveSubscription(shopId): Promise<boolean>`. Enforced server-side on the
request route; the UI mirrors it for presentation only.

This is the **first** subscription gate in the webapp — gating today is by
vertical bundle and role. Keep it to this one predicate; do not introduce a
tier concept until something needs base-vs-pro.

Testing note: **all 23 shops on QA have `plan_id = NULL`**, so the gate denies
everyone until a plan is assigned to a test shop.

## 5. Data model

`shop_telephony.kairo_number` is `NOT NULL`, so the bundle lifecycle cannot live
there — a request exists long before a number does. New table, and
`shop_telephony` keeps its current meaning: *this shop has a working number*.

```sql
CREATE TABLE IF NOT EXISTS voice_agent.number_requests (
  shop_id            uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  status             text NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','evaluating','pending_review',
                                       'approved','rejected','provisioned')),
  regulation_sid     text,
  bundle_sid         text,
  end_user_sid       text,
  document_sid       text,
  business_name      text,
  contact_email      text,
  evaluation_errors  jsonb,     -- Twilio's own violations, shown verbatim
  rejection_reason   text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  submitted_at       timestamptz,
  reviewed_at        timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now()
);
```

PK on `shop_id` — one open request per shop, matching "one number per shop".

**Health semaphore** on `shop_telephony` (the number must exist to be healthy):

```sql
ALTER TABLE voice_agent.shop_telephony
  ADD COLUMN IF NOT EXISTS health_status   text NOT NULL DEFAULT 'unknown'
    CHECK (health_status IN ('unknown','green','red')),
  ADD COLUMN IF NOT EXISTS health_detail   text,
  ADD COLUMN IF NOT EXISTS health_checked_at timestamptz;
```

## 6. Flow

```
Inbox → Configurazione → Canali
  no plan            → upgrade prompt
  plan, no request   → [ Richiedi numero ] → form
  request pending    → status + Twilio's violations if any
  provisioned        → number + green/red semaphore
      │
      ▼  POST /api/v1/hair-salon/voice/numbers/request     (webapp, session auth)
         getShopId → hasActiveSubscription → 402 if not
         multipart: business_name + contact_email + the document file
      │
      ▼  POST /api/v1/voice/numbers/request                (voice-booking, control-plane)
         query Regulations(EE, mobile) → regulation SID
         create End-User (business, business_name)
         upload SupportingDocument (commercial_registrar_excerpt + file)
         create Bundle (regulation, EE, business, email)
         2 × ItemAssignment
         POST Evaluations  ─ noncompliant → store violations, status='draft', return them
                           └ compliant    → Status=pending-review, status='pending_review'
      │
      ▼  /messaging/tick (hourly cron)
         pending_review requests → poll bundle status
           twilio-approved → status='approved' → purchase number → shop_telephony
           twilio-rejected → status='rejected' + reason
         provisioned shops → health check → green / red
```

### 6.1 Purchase happens on approval, not on request

The salon never waits on a synchronous purchase. The cron sees an approved
bundle, buys the number against **that salon's** `bundle_sid`, and inserts
`shop_telephony` via the insert-only path in §3.1.

### 6.2 `voice_url` at purchase time

Keep setting it even though voice ships later: `shop_config.answer_mode` gates
whether the agent answers, so a provisioned number does not start taking calls
by itself, and it saves a second Twilio round-trip at the voice upgrade.
Implementation must confirm a shop with **no** `shop_config` row degrades safely
rather than answering.

## 7. The health semaphore

Runs in the same hourly tick. For each shop with a number, one Twilio read
(`incoming_phone_numbers(sid).fetch()`), then:

| Condition | Result |
|---|---|
| number fetches, belongs to the account, `voice_url`/`sms_url` point at us | **green** |
| Twilio 404 — number released, deleted or moved | **red**, detail `number_missing` |
| webhook URLs wrong | **red**, detail `webhook_drift` |
| Twilio unreachable / 5xx | leave previous status, record the attempt |

That last row matters: a Twilio outage must not paint every salon red. Only a
definite negative answer flips to red; an inconclusive one leaves the last known
state and updates `health_checked_at`.

UI: a dot next to the number in Configurazione → Canali, with the detail as
hover text. Red also raises the existing `NumberActivationBanner`.

## 8. Form and prefill

Two fields and one file. Prefill everything we already know:

| Field | Prefilled from |
|---|---|
| Business name | `shops.name` |
| Contact email | the owner's user record |
| Commercial register extract (visura camerale) | — upload, cannot be prefilled |

`shops` also has `vat_number` and full address columns. **Not collected** — the
Estonia regulation does not ask for them, and sending fields a regulation does
not request is how bundles fail evaluation. If Twilio's requirements change, the
dynamic query in §2.1 surfaces it.

The file is streamed through webapp → voice-booking → Twilio without landing in
our database. We store only the resulting `document_sid`.

### 8.1 The waiting experience

The compliance path buys a multi-day gap between "salon pays" and "salon has a
number". That gap is the product problem; leaving it unexplained turns a
correct system into one that looks broken.

**States the salon sees**, each with what it must answer — *what happened, what
now, when*:

| State | Message | Action shown |
|---|---|---|
| `draft`, no submission | What the number is for, what the document is, that review takes a few days | the form |
| `draft` + `evaluation_errors` | Twilio's own violations, verbatim | fix and resubmit |
| `pending_review` | "Richiesta inviata il {submitted_at}. Verifica in corso." + **expected by {date}** | none — say so explicitly |
| `pending_review`, past the window | "Sta richiedendo più tempo del previsto. Nessuna azione da parte tua." | contact support |
| `rejected` | Twilio's `rejection_reason`, verbatim | resubmit with a corrected document |
| `approved` / provisioning | "Approvata. Stiamo attivando il numero." | none |
| `provisioned` | the number + the green/red semaphore | — |

**Expected-by date: `submitted_at` + 3 business days.** Twilio's regulatory
review is documented as 1–3 business days; the deleted Telnyx-era route in this
repo carried the same figure. Show the conservative end, not the optimistic
one — a promise that slips is worse than a slower promise kept. The constant
lives in one place so it can be corrected once real review times are observed.

**Past the window, stop promising.** Do not keep showing a date that has
already passed; switch to the overdue copy above. The state is derived from
`submitted_at`, not stored, so it needs no extra column and cannot go stale.

**No notification is sent today, deliberately.**
`booking_engine/clients/push_notifications.py::send_push` is a **stub** that
only logs — Plan C was to wire it to the webapp and never did. The service
still emits `number_request_approved` / `number_request_rejected` events for
consistency with the existing call-lifecycle and balance-alert emitters, so
they become real the day that stub is wired. Until then the salon learns the
outcome by opening Inbox, and `NumberActivationBanner` (which already renders
on Inbox load) surfaces it without them navigating to Configurazione.

Do not build email or notification infrastructure for this. It is one banner on
a page the owner already visits daily.

## 9. Testing

- **Unit, mocked Twilio:** gate predicate; second request returns the first;
  second provision does not purchase; lost race releases the number; evaluation
  violations map to the response; health check states including the
  Twilio-unreachable case.
- **Live DB:** two requests leave one `number_requests` row; two provisions
  leave one `shop_telephony` row.
- **Real Twilio (credentials available, read-only):** query the regulation and
  assert the requirement shape still matches §2.1 — this is the test that
  catches a regulation change.
- **Real Twilio, one deliberate manual run:** create a real bundle for a test
  shop and submit it. **A number purchase costs ~$3/mo and is not free to
  undo** — that step stays manual and owner-triggered, never automated in CI.

## 10. Out of scope

WhatsApp sender registration · the voice/forwarding upgrade · number release on
subscription lapse (§11) · any tier distinction above "has a plan" · bundle
updates when a regulation changes (Twilio's Bundle Copies flow).

## 11. Open question

**What happens when a subscription is cancelled?** `process-event.ts` nulls
`plan_id`, but nothing touches `shop_telephony`. The number keeps costing $3/mo
and the shop keeps a number it no longer pays for. A product decision — grace
period, release, or bill separately — not a provisioning defect, but it becomes
real the first time someone churns.

## 12. Documentation obligation

Adds endpoints, a table and a hard constraint in both repos: voice-booking
`docs/knowledge/{architecture,database,providers,api}.md`; webapp
`docs/knowledge/{features,providers,architecture,decisions}.md`. Also a
`CLAUDE.md` entry recording that the one-bundle-for-all model from 2026-07-16 is
superseded, and why.
