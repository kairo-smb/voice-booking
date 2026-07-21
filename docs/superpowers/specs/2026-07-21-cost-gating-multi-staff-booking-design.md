# Cost-on-request + multi-service/multi-staff bookings

Two independent refinements to the voice agent's MCP tools, specced together
because both touch the same tool-schema files (`safety_layer.py`,
`voice_tool_models.py`, `voice_tools_*.py`).

## Section A — cost disclosure gated behind explicit ask

**Problem:** `get_services` always returns `price_cents`. Nothing stops the
model from volunteering price when the customer never asked.

**Change:** `get_services` gains an optional `include_price: bool = False`
param. When false (the default), `price_cents` is omitted from the response
entirely — not hidden by prompt convention, actually absent from the JSON the
model receives. The model must pass `include_price=true` to get it back.

**Files:**
- `booking_engine/api/voice_tool_models.py`: `GetServicesIn.include_price:
  bool = False`; `ServiceOut.price_cents: int | None = None`
- `booking_engine/api/routes/voice_tools_catalog.py`: thread the flag through
- `booking_engine/db/voice_tool_queries.py::list_services`: drop
  `price_cents` from the returned rows when not requested (post-query filter,
  no SQL branching needed)
- `booking_engine/services/safety_layer.py`: update the `get_services` tool
  JSON schema (`_TOOL_SCHEMAS`) with the new param; add one `SAFETY_PROMPT`
  rule — call `get_services` with `include_price=true` only when the customer
  explicitly asks about cost, never volunteer it otherwise

Fully additive to name/duration — only price becomes opt-in.

## Section B — multi-service, multi-staff bookings

**Problem:** a visit can require multiple services performed by different
staff (e.g. colore by staff A, piega by staff B), in a required order, with
bookings that are practically consecutive (max 20 min idle gap between
services). Today `check_availability`/`create_booking` assume one service,
one staff, for the whole appointment.

**Schema confirms this is supported at the DB layer already:**
`business_app_core.appointment_services` has its own `staff_id` and
`start_time` per row ("can differ per service within one appointment" —
`webapp/docs/knowledge/database.md`). The voice-booking Python layer
(`queries.py::get_available_slots`/`create_appointment`) just never uses
that — it hardcodes one staff for the whole appointment and sums durations
blindly. This work brings the voice layer up to what the schema already
allows.

### Wire format (breaking change to `check_availability`/`create_booking`; acceptable — no real calls are live yet, Twilio is unfunded per project history)

Both tools move from a single `service_id`/`staff_id` to an ordered list. A
plain single-service booking is just a one-element list — no separate code
path for "simple" vs "multi" bookings.

`check_availability` request:
```json
{
  "services": [
    {"service_id": "<colore-uuid>", "staff_id": null},
    {"service_id": "<piega-uuid>", "staff_id": "<giulia-uuid>"}
  ],
  "preferred_when": "2026-07-22T13:00:00+02:00",
  "max_results": 5
}
```
- `services`: non-empty ordered list; order = the required execution order.
- `staff_id` per leg is optional: omitted → system auto-assigns an eligible,
  available staff member for that leg; the model only pins a `staff_id` when
  the customer states a preference for that specific service.

`check_availability` response — each candidate is a full chain:
```json
[
  {
    "slot_start": "...", "slot_end": "...",
    "legs": [
      {"service_id": "...", "staff_id": "...", "staff_name": "Marco", "slot_start": "...", "slot_end": "..."},
      {"service_id": "...", "staff_id": "...", "staff_name": "Giulia", "slot_start": "...", "slot_end": "..."}
    ]
  }
]
```

`create_booking` request: `customer_id` + the exact `legs` array copied
verbatim from the chosen `check_availability` candidate — no recomputation,
what was quoted to the customer is what gets booked.

### Chaining algorithm

Extends the existing per-day/per-staff slot generator in
`queries.py::get_available_slots` — does not replace it.

1. Generate leg-1 candidate starts exactly as today (staff working hours,
   30-min steps, minus existing appointments), restricted to the pinned
   `staff_id` if given, else all staff eligible for that service.
2. For each leg-1 candidate, try to extend the chain: leg 2's start must fall
   within `[leg1_end, leg1_end + MAX_GAP_MINUTES]`, with an eligible and
   available staff member (pinned or auto-searched), and so on for every
   subsequent leg in order.
3. A chain is a result only if *every* leg resolves. Collect up to
   `max_results` chains, sorted by proximity to `preferred_when`.

**New constant:** `MAX_GAP_MINUTES = 20` in
`booking_engine/services/booking_constraints.py`, plus a small pure helper
`gap_within_limit(prev_end, next_start) -> bool`, matching the existing style
of `slot_in_past`/`within_lead_time`. Fixed constant, not per-shop
configurable — nothing has asked for it to vary yet.

### Ordering

Enforced exactly as given in the `services` list — the system does not store
"colore before piega" as a rule (no such column/table exists in the schema,
and none is being added). Instead, `SAFETY_PROMPT` gets a rule telling the
model to order multi-service requests using general hairdressing convention
(chemical/color services before cut/styling) unless the customer states a
different order themselves. Domain knowledge lives in the model, not in a
dependency table.

### Storage

One `appointments` row per visit:
- `staff_id` = leg 1's staff (kept for backward-compat with anything still
  reading the top-level column)
- `start_time` = leg 1's start (= whole-chain start)
- `end_time` = last leg's end (= whole-chain end)

Plus one `appointment_services` row per leg, each with its own `staff_id`,
`start_time`, `duration_minutes`, `price_eur` — matches the schema's existing
per-leg columns exactly.

### Out of scope

`modify_booking`/`cancel_booking` stay whole-appointment-level, unchanged —
no per-leg modify. Cancelling a multi-service booking still cancels the
entire visit, which already works with no changes needed.

### Files touched

- `booking_engine/api/voice_tool_models.py` — `CheckAvailabilityIn`,
  `CreateBookingIn`, `AvailabilitySlot`/new `BookingLeg`, `BookingOut`
- `booking_engine/api/routes/voice_tools_booking.py` — `check_availability`,
  `create_booking` route logic
- `booking_engine/db/voice_tool_queries.py` — `find_availability`,
  `insert_booking_locked`
- `booking_engine/db/queries.py` (ground truth) — `get_available_slots`,
  `create_appointment`
- `booking_engine/services/booking_constraints.py` — `MAX_GAP_MINUTES`,
  `gap_within_limit`
- `booking_engine/services/safety_layer.py` — tool JSON schemas + prompt
  rules for both sections
- Existing tests calling these tools with the old single-service shape:
  `tests/voice_gateway/*`, `tests/live_db/test_tool_dispatch_*`
