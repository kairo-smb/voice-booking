# WhatsApp template engagement revamp — design + cross-repo plan (2026-09-02)

**Status:** working design. Cross-repo (voice-booking, webapp, marketing-engine).
Nothing here is live on Meta yet (no template body has ever been approved — verified
with the owner), so the marketing templates are **rewritten in place, keys unchanged**.
On ship, this working doc is superseded by the CLAUDE.md entry + `docs/knowledge/*`
updates; delete it then.

## Goal

The three LLM-driven marketing templates read "asettici" (impersonal, seller-voiced:
`ti scriviamo da … per proporti …`). Make them warm and human: **signed by the
stylist of the customer's last visit**, tuned to that visit, low-pressure
("nudging delicato"), and — for rebook — never about money. Keep a soft, shared CTA
so Meta approves bodies more easily. Add one new UTILITY template for a future
"receipt" feature.

## 1. Copy decisions (voice-booking, `whatsapp_templates.py`)

Keys kept. Only promo_v1 / winback_v1 / rebook_v1 bodies change; promo_manual_v1 /
feedback_v2 / reminder_v6 untouched. New `receipt_v1` added.

Shared facts across the three marketing templates, resolved per customer from the
**last visit (appointment)**, not from payments:

| slot | promo_v1 | winback_v1 | rebook_v1 |
|---|---|---|---|
| {{1}} | name | name | name |
| {{2}} | stylist (last visit, primary staff) | same | same |
| {{3}} | salon display name | salon display name | salon display name |
| {{4}} | **LLM** observation | gap ("tre mesi") | cadence ("ogni sei settimane") |
| {{5}} | — | **LLM** proposal | **LLM** proposal |

Generated slot stays exactly one per template (`generated_slot` 4 / 5 / 5). Shared
soft CTA in the fixed body: **«Se ti va, scrivimi pure.»**

- **promo_v1** (observation replaces the offer):
  `Ciao {{1}}, sono {{2}} di {{3}}. {{4}} Se ti va, scrivimi pure.`
  {{4}} guidance: check-in observation anchored on a service the customer really
  had (grounding supplied), never a price, no urgency, no booking ask (the CTA is
  fixed), max one natural question. Example: *«Sono passate circa tre settimane dal
  tuo colore: com'è la ricrescita?»*
- **winback_v1** (unchanged intent, re-registered):
  `Ciao {{1}}, sono {{2}} di {{3}}. Non ci vediamo da {{4}}, quindi ti propongo {{5}}. Se ti va, scrivimi pure.`
  Price allowed (winback is the explicit come-back offer). {{5}} guidance as today.
- **rebook_v1** (money forbidden):
  `Ciao {{1}}, sono {{2}} di {{3}}. Di solito passi da noi ogni {{4}}, quindi potrebbe essere il momento giusto per {{5}}. Se ti va, scrivimi pure.`
  {{5}} guidance: **only service names, never an amount** (owner rule). Sample
  drops the price. Guard test pins it.
- **receipt_v1** (UTILITY, no generated slot, future feature):
  `Ciao {{1}}, ecco i dettagli della tua visita del {{2}}: {{3}}. Totale: {{4}} €. Grazie e a presto!`
  {{1}} name · {{2}} visit date · {{3}} itemised single-line (`Taglio 25,00 € · Piega 18,00 €`)
  · {{4}} total. Facts only → UTILITY economics (€0.0341, no consent, no cooldown).
  Enters `CATALOGUE` → pushed to every WABA on the next sweep (owner approved);
  inert until a sender references it.

The `intent` field values (`promo`/`winback`/`rebook`) are **unchanged** so
marketing-engine routing keeps working; the *semantics* of the promo slot move from
"offer" to "observation" and live in `guidance` + the engine prompt.

## 2. Stylist fact — exists, one gap closed (webapp)

`getCustomerHistory` (`webapp/src/lib/db/repositories/appointments.repo.ts:651`)
already returns per-customer past visits with the **primary staff name**
(`a.staff_id` join). Owner decision: **last-visit stylist is enough** — no
per-service staff plumbing.

Webapp must expose it at the two points that assemble per-recipient template
variables, and (for bulk, up to 2000 recipients) as a **batch** last-visit-per-customer
query, not N single calls. The single win-back/offer flows already fetch visit data;
they gain the staff name from the same query.

Coherence: the three marketing templates now anchor on the **last visit** for all
message facts (stylist + recency). Win-back currently derives `days_since` from
**payments** (`getVisitCadence`, `MAX(closed_at)`) while grounding also uses
`customer_service_profile.last_received_at`. Decision: anchor message facts on the
last *appointment*; keep the *audience* definition (retention risk) payment-based.
`RetentionCustomer`/offer grounding gains the last-visit staff name and an explicit
last-visit date, so the LLM observation, the gap and the signature can't contradict
each other.

## 3. Tracking + empty-basket gate (webapp + marketing-engine)

Today the three generation flows (single win-back, whatsapp-offer, bulk campaign)
call `deductCredits(rawToUserCredits(llm_cost_usd))` once, **after** the engine
has generated — no audit entry, and a bulk campaign runs hundreds of LLM calls in
one synchronous engine request, then the webapp deducts a single total that can
land on an empty basket (cost already incurred, credits that don't exist).

**Decision: batching + two-phase charge, reusing the existing ledger.**
`ai_run_ledger` (preCharge/chargeActual/reconcile) already covers chat and marketing
runs; generation flows adopt it instead of ad-hoc single `deductCredits`.

- **Single-customer flows** (win-back offer, whatsapp-offer): `preCharge` a reserve
  for the estimate before calling the engine; abort 402 *before* generation if the
  basket can't cover it; `chargeActual` on the engine-reported `llm_cost_usd`;
  release the unused reserve. One audit row per call.
- **Bulk campaigns**: generation becomes **batched** (owner-suggested). The webapp
  drives the campaign in slices (e.g. 25 recipients): for each slice, check the
  reserve against the available balance **before** the engine call, generate only
  that slice, `chargeActual`, repeat. If the balance runs out mid-campaign, stop
  cleanly, leave the remaining recipients un-generated (no charge, resumable), and
  surface the partial state. Nothing runs on an empty basket.
- Engine change: `POST /campaigns/:id/generate` currently loops the whole audience
  server-side; it must accept a slice (recipient subset) and return per-slice cost,
  so the webapp can be the money-gate between slices. Endpoint stays synchronous.
- **Audit**: each charged slice/call writes the ledger row (flow, shop, engine
  surface, `llm_cost_usd`, credits, campaign/recipient refs). This is the "correctly
  tracked by the webapp" requirement — today the flows update the balance but leave
  no record.

Note: the WhatsApp **send** itself never debits credits (salon pays Meta directly;
voice-booking only audits `whatsapp.audit_events`). Only *generation* is a basket
cost. `receipt_v1` has no generated slot → never touches the basket.

## 4. Work split by repo

### voice-booking (this repo)
1. `services/messaging/whatsapp_templates.py`: rewrite the 3 bodies/variables/
   samples/`generated_slot`/`guidance` (rebook: money forbidden), add `receipt_v1`;
   refresh the stale comments that describe promo as an offer frame.
2. Tests (`tests/booking_engine/test_whatsapp.py`): update the two static pins
   (promo render at ~154; winback descriptor `generated_slot==4`/`{{4}}` at ~1462).
   Add guard tests: rebook body+sample+guidance contain no `€`/price; the three
   marketing bodies carry the shared CTA tail. Catalogue-wide sample test already
   iterates `CATALOGUE` → covers `receipt_v1` for free.
3. Docs: `docs/knowledge/providers.md` + `docs/knowledge/api/whatsapp.md` updated in
   the same change (repo rule); CLAUDE.md entry appended on merge.
4. Operator step after merge (not automatable here): owner runs
   `kairo_waba.py push-templates` and gets Meta approval for the new bodies on
   Kairo's WABA; the hourly sweep then propagates them to every customer WABA.

### webapp
5. Batch last-visit lookup incl. primary staff name; wire stylist + last-visit date
   into the fact objects for single win-back/offer and bulk recipients; swap the
   offer path's `days_since` source from payments to last appointment where it feeds
   these message facts.
6. Route generation through `ai_run_ledger` (reserve → engine → charge → release),
   batching bulk campaign generation, with the empty-basket abort. Audit row per
   call/slice.

### marketing-engine
7. `POST /campaigns/:id/generate`: accept a recipient slice; return per-slice cost.
8. Prompt scaffolding: `buildOfferSystem` + the per-recipient context JSON gain the
   staff name and last-visit date; promo-guidance semantics (observation, not offer)
   flow through. Engine already receives `services[]` + `last_received_at` per
   customer → recency grounding exists; the guidance tells it to anchor there.

## 5. Release ordering

Coordination matters because the variable *meanings* of the three keys change but
the keys don't. Deploy **webapp + marketing-engine before/with voice-booking** so no
path ever assembles old-shaped variables against new bodies. Since no body is
live-approved, there is no production traffic to strand; this is purely about not
shipping a transiently inconsistent catalogue. After all three merge, the manual
push+approval step (4) makes it real.

## Open items resolved this session
- Stylist = last-visit primary staff (owner). ✅
- receipt_v1 added to CATALOGUE now (owner). ✅
- Bulk generation batched, not sequential per recipient (owner opened; batch chosen:
  bounds round-trips and is the money-gate granularity that matters).
