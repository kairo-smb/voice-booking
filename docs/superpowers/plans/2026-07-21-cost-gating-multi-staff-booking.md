# Cost Gating + Multi-Staff Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate `get_services` price data behind an explicit customer ask, and generalize `check_availability`/`create_booking` to support ordered, multi-service, multi-staff bookings with a 20-minute max gap between legs.

**Architecture:** Two independent slices sharing the same tool-schema files. Cost gating is a route-level filter (no DB change). Multi-staff booking adds new, additive functions to the ground-truth query layer (`get_available_slot_chains`, `create_appointment_chain`) rather than modifying the existing single-service `get_available_slots`/`create_appointment` — those stay untouched and keep serving their other callers (`booking_engine/api/routes/availability.py`, `appointments.py`) unmodified. The voice-tool wrapper layer (`voice_tool_queries.py`) picks the single-leg or multi-leg path based on how many services are requested.

**Tech Stack:** FastAPI + Pydantic (tool routes), asyncpg (raw SQL, no ORM), pytest + pytest-asyncio (`asyncio_mode = auto`), httpx `ASGITransport` for route tests.

**Spec:** `docs/superpowers/specs/2026-07-21-cost-gating-multi-staff-booking-design.md`

---

## Before you start

Run the existing suite once to get a clean baseline (excludes `tests/live_db/*`, which are skipped without `DATABASE_URL` and can't run in this environment):

```bash
pytest tests/ --ignore=tests/live_db -q
```

Expected: all pass. If not, stop and investigate before starting — don't build on a red baseline.

---

### Task 1: Gate `get_services` price behind `include_price`

**Files:**
- Modify: `booking_engine/api/voice_tool_models.py`
- Modify: `booking_engine/api/routes/voice_tools_catalog.py`
- Test: `tests/voice_gateway/test_voice_tools_catalog.py`

- [ ] **Step 1: Write the failing tests**

Replace `test_get_services_returns_filtered` in `tests/voice_gateway/test_voice_tools_catalog.py` with:

```python
@pytest.mark.asyncio
async def test_get_services_omits_price_by_default():
    sid = uuid4()
    fake = [{"id": sid, "name": "Taglio donna", "duration_min": 30, "price_cents": 2500}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/tools/get_services",
                             headers=AUTH, json={"filter": "taglio"})
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["service_id"] == str(sid)
    assert body["data"][0]["price_cents"] is None


@pytest.mark.asyncio
async def test_get_services_include_price_true_returns_price():
    sid = uuid4()
    fake = [{"id": sid, "name": "Taglio donna", "duration_min": 30, "price_cents": 2500}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/tools/get_services", headers=AUTH,
                             json={"filter": "taglio", "include_price": True})
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["price_cents"] == 2500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/voice_gateway/test_voice_tools_catalog.py -v`
Expected: `test_get_services_omits_price_by_default` FAILs (price_cents is 2500, not None); `test_get_services_include_price_true_returns_price` FAILs with a 422 (unknown field `include_price` not yet accepted... actually Pydantic ignores unknown fields by default, so this one fails on the assertion `price_cents == 2500` returning the always-present price instead — either way, both fail against current behavior).

- [ ] **Step 3: Update `voice_tool_models.py`**

In `booking_engine/api/voice_tool_models.py`, replace:

```python
class ServiceOut(BaseModel):
    service_id: UUID
    name: str
    duration_min: int
    price_cents: int
```

with:

```python
class ServiceOut(BaseModel):
    service_id: UUID
    name: str
    duration_min: int
    price_cents: int | None = None
```

And replace the `GetServicesIn` request model near the bottom of the file:

```python
class GetServicesIn(BaseModel):
    filter: str | None = None
```

with:

```python
class GetServicesIn(BaseModel):
    filter: str | None = None
    include_price: bool = False
```

- [ ] **Step 4: Update the route**

In `booking_engine/api/routes/voice_tools_catalog.py`, replace the `get_services` handler body:

```python
@router.post("/get_services")
async def get_services(
    body: GetServicesIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[ServiceOut]]:
    rows = await list_services(shop_id=x_shop_id, filter_q=body.filter)
    out = [ServiceOut(service_id=r["id"], name=r["name"],
                      duration_min=r["duration_min"],
                      price_cents=r["price_cents"] if body.include_price else None)
           for r in rows]
    return Envelope[list[ServiceOut]](ok=True, data=out)
```

(Only the `price_cents=...` line changes — the query layer, `list_services`, is untouched; it always computes price_cents cheaply in SQL, the route just decides whether to forward it.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_voice_tools_catalog.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add booking_engine/api/voice_tool_models.py booking_engine/api/routes/voice_tools_catalog.py tests/voice_gateway/test_voice_tools_catalog.py
git commit -m "feat(voice-tools): gate get_services price behind include_price"
```

---

### Task 2: `safety_layer` — `get_services` schema + "don't volunteer price" rule

**Files:**
- Modify: `booking_engine/services/safety_layer.py`
- Test: `tests/voice_gateway/test_safety_layer.py`

- [ ] **Step 1: Write the failing tests**

In `tests/voice_gateway/test_safety_layer.py`, change the import line to also pull `_TOOL_SCHEMAS`:

```python
from booking_engine.services.safety_layer import (
    SAFETY_PROMPT,
    DEFAULT_TOOL_ALLOWLIST,
    _TOOL_SCHEMAS,
    tool_descriptions,
)
```

Add these two tests at the end of the file:

```python
def test_safety_prompt_mentions_price_gating_rule():
    text = SAFETY_PROMPT.lower()
    assert "include_price" in text


def test_get_services_schema_has_include_price_param():
    schema = _TOOL_SCHEMAS["get_services"]["parameters"]
    assert schema["properties"]["include_price"]["type"] == "boolean"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/voice_gateway/test_safety_layer.py -v`
Expected: both new tests FAIL (`include_price` not yet in the prompt or schema)

- [ ] **Step 3: Update the tool schema**

In `booking_engine/services/safety_layer.py`, replace the `get_services` entry in `_TOOL_SCHEMAS`:

```python
    "get_services": {
        "name": "get_services",
        "description": "Lista dei servizi del salone, opzionalmente filtrati per nome.",
        "parameters": {
            "type": "object",
            "properties": {"filter": {"type": "string"}},
        },
    },
```

with:

```python
    "get_services": {
        "name": "get_services",
        "description": (
            "Lista dei servizi del salone, opzionalmente filtrati per nome. "
            "Il prezzo NON è incluso a meno che include_price non sia true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {"type": "string"},
                "include_price": {
                    "type": "boolean",
                    "description": (
                        "Imposta a true SOLO se il cliente ha chiesto "
                        "esplicitamente il prezzo o il costo."
                    ),
                },
            },
        },
    },
```

- [ ] **Step 4: Add the prompt rule**

In `SAFETY_PROMPT`, right after the existing line:

```
- Non trattare prezzi al di fuori di quelli forniti dagli strumenti. Non \
contrattare sconti non già configurati.
```

add:

```
- PREZZI: chiama get_services con include_price=true SOLO se il cliente \
chiede esplicitamente il prezzo o il costo di un servizio. Altrimenti non \
menzionare mai il prezzo di tua iniziativa.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_safety_layer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add booking_engine/services/safety_layer.py tests/voice_gateway/test_safety_layer.py
git commit -m "feat(voice-tools): tell the agent to only fetch price when asked"
```

---

### Task 3: `MAX_GAP_MINUTES` constant + `gap_within_limit` helper

**Files:**
- Modify: `booking_engine/services/booking_constraints.py`
- Test: `tests/voice_gateway/test_booking_constraints.py`

- [ ] **Step 1: Write the failing tests**

In `tests/voice_gateway/test_booking_constraints.py`, change the import to:

```python
from booking_engine.services.booking_constraints import (
    MAX_GAP_MINUTES, gap_within_limit, slot_in_past, within_lead_time,
)
```

Add at the end of the file:

```python
def test_gap_within_limit_true_for_back_to_back():
    end = NOW
    assert gap_within_limit(end, end) is True


def test_gap_within_limit_true_at_exactly_max_gap():
    end = NOW
    assert gap_within_limit(end, end + timedelta(minutes=MAX_GAP_MINUTES)) is True


def test_gap_within_limit_false_beyond_max_gap():
    end = NOW
    assert gap_within_limit(end, end + timedelta(minutes=MAX_GAP_MINUTES + 1)) is False


def test_gap_within_limit_false_when_next_starts_before_prev_ends():
    end = NOW
    assert gap_within_limit(end, end - timedelta(minutes=1)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/voice_gateway/test_booking_constraints.py -v`
Expected: FAIL with `ImportError: cannot import name 'MAX_GAP_MINUTES'`

- [ ] **Step 3: Implement**

In `booking_engine/services/booking_constraints.py`, add after `within_lead_time`:

```python
MAX_GAP_MINUTES = 20


def gap_within_limit(prev_end: datetime, next_start: datetime) -> bool:
    """True when next_start is at or after prev_end, and no more than
    MAX_GAP_MINUTES later — the max idle time allowed between two
    consecutive services in a multi-service booking."""
    return prev_end <= next_start <= prev_end + timedelta(minutes=MAX_GAP_MINUTES)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_booking_constraints.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/booking_constraints.py tests/voice_gateway/test_booking_constraints.py
git commit -m "feat(voice-tools): add MAX_GAP_MINUTES gap constraint helper"
```

---

### Task 4: `queries.py` — `get_available_slot_chains` (multi-leg chain search)

**Files:**
- Modify: `booking_engine/db/queries.py`
- Test: `tests/booking_engine/test_queries.py`

This adds a new, additive function — `get_available_slots` (used by `availability.py` and the existing single-service voice path) is untouched.

- [ ] **Step 1: Write the failing tests**

In `tests/booking_engine/test_queries.py`, update the import block to add `get_available_slot_chains`:

```python
from booking_engine.db.queries import (
    SlotConflictError,
    cancel_appointment,
    create_appointment,
    create_customer,
    find_customers_by_name_and_phone,
    find_customers_by_phone,
    get_available_slot_chains,
    get_shop,
    get_staff_services,
    list_appointments,
    list_services,
    list_staff,
    reschedule_appointment,
)
```

Add two new UUID constants near the existing ones (`SHOP`, `STAFF`, `SVC`, ...):

```python
STAFF2 = UUID("11111111-0000-0000-0000-000000000002")
SVC2 = UUID("aaaa0001-0000-0000-0000-000000000002")
```

Add a new test class:

```python
class TestGetAvailableSlotChains:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_single_day_two_legs_different_staff(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}, {"id": SVC2, "duration_minutes": 30}],  # durations
            [{"staff_id": STAFF, "staff_name": "Ana"}],  # eligible leg0
            [{"staff_id": STAFF2, "staff_name": "Bob"}],  # eligible leg1
            [],  # existing appointments
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff0 day windows
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff1 day windows
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day, max_results=1,
        )
        assert len(result) == 1
        legs = result[0]["legs"]
        assert legs[0]["staff_id"] == STAFF and legs[1]["staff_id"] == STAFF2
        assert legs[0]["slot_end"] == legs[1]["slot_start"]  # back-to-back, 0-minute gap

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_no_eligible_staff_for_second_leg_returns_empty(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}, {"id": SVC2, "duration_minutes": 30}],
            [{"staff_id": STAFF, "staff_name": "Ana"}],
            [],  # no one eligible for leg1
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day,
        )
        assert result == []

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_unknown_service_in_chain_returns_empty(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}],  # only one of two services found/active
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day,
        )
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/booking_engine/test_queries.py -k TestGetAvailableSlotChains -v`
Expected: FAIL with `ImportError: cannot import name 'get_available_slot_chains'`

- [ ] **Step 3: Implement**

In `booking_engine/db/queries.py`, add this import at the top alongside the existing ones:

```python
from booking_engine.services.booking_constraints import gap_within_limit
```

Then add the following functions after `get_available_slots` and before `create_appointment`:

```python
async def _eligible_staff_for_leg(
    shop_id: UUID, service_id: UUID, staff_id: UUID | None,
) -> list[dict]:
    if staff_id:
        return await execute(
            "SELECT st.id AS staff_id, st.full_name AS staff_name "
            "FROM business_app_core.staff st "
            "JOIN business_app_core.staff_services ss ON ss.staff_id = st.id "
            "WHERE st.shop_id = $1 AND st.is_active = true AND st.id = $2 AND ss.service_id = $3",
            shop_id, staff_id, service_id,
        )
    return await execute(
        "SELECT st.id AS staff_id, st.full_name AS staff_name "
        "FROM business_app_core.staff st "
        "JOIN business_app_core.staff_services ss ON ss.staff_id = st.id "
        "WHERE st.shop_id = $1 AND st.is_active = true AND ss.service_id = $2",
        shop_id, service_id,
    )


async def _staff_day_windows(staff_id: UUID, day: date) -> list[tuple[datetime, datetime]]:
    scheds = await execute(
        "SELECT start_time, end_time FROM business_app_core.staff_schedules "
        "WHERE staff_id = $1 AND day_of_week = $2",
        staff_id, day.weekday(),
    )
    windows = []
    for sched in scheds:
        st_parts = str(sched["start_time"]).split(":")
        et_parts = str(sched["end_time"]).split(":")
        windows.append((
            datetime.combine(day, time(int(st_parts[0]), int(st_parts[1])), tzinfo=_ROME),
            datetime.combine(day, time(int(et_parts[0]), int(et_parts[1])), tzinfo=_ROME),
        ))
    return windows


def _overlaps_existing(
    slot_start: datetime, slot_end: datetime, staff_id: UUID, existing: list[dict],
) -> bool:
    for appt in existing:
        if appt["staff_id"] != staff_id:
            continue
        a_start = appt["start_time"] if isinstance(appt["start_time"], datetime) else datetime.fromisoformat(str(appt["start_time"]))
        a_end = appt["end_time"] if isinstance(appt["end_time"], datetime) else datetime.fromisoformat(str(appt["end_time"]))
        if a_start.tzinfo is None:
            a_start = a_start.replace(tzinfo=_ROME)
            a_end = a_end.replace(tzinfo=_ROME)
        if slot_start < a_end and slot_end > a_start:
            return True
    return False


async def _iter_leg0_candidates(
    eligible: list[dict], duration: int, start_date: date, end_date: date, existing: list[dict],
):
    """Yield (staff, slot_start, slot_end) candidates for the first leg of a
    chain, in the same day/staff/30-min-step order as get_available_slots."""
    current = start_date
    while current <= end_date:
        for staff in eligible:
            for w_start, w_end in await _staff_day_windows(staff["staff_id"], current):
                slot_start = w_start
                while slot_start + timedelta(minutes=duration) <= w_end:
                    slot_end = slot_start + timedelta(minutes=duration)
                    if not _overlaps_existing(slot_start, slot_end, staff["staff_id"], existing):
                        yield staff, slot_start, slot_end
                    slot_start += timedelta(minutes=30)
        current += timedelta(days=1)


async def _try_extend_chain(
    legs: list[dict],
    remaining_services: list[dict],
    remaining_eligible: list[list[dict]],
    duration_by_id: dict[UUID, int],
    existing: list[dict],
) -> list[dict] | None:
    """Recursively append `remaining_services` to `legs`, first-fit: the
    first staff+window combination that satisfies MAX_GAP_MINUTES and has no
    conflict wins. Backtracks (tries the next staff/window) only if a later
    leg can't be completed.

    ponytail: only searches the same calendar day as the previous leg's end
    for the next leg — a chain ending near midnight won't roll into the next
    day. Salon hours don't reach midnight in practice, so not handled.
    """
    if not remaining_services:
        return legs
    prev_end = legs[-1]["slot_end"]
    service = remaining_services[0]
    duration = duration_by_id[service["service_id"]]
    day = prev_end.date()
    for staff in remaining_eligible[0]:
        for w_start, w_end in await _staff_day_windows(staff["staff_id"], day):
            leg_start = max(prev_end, w_start)
            if not gap_within_limit(prev_end, leg_start):
                continue
            leg_end = leg_start + timedelta(minutes=duration)
            if leg_end > w_end:
                continue
            if _overlaps_existing(leg_start, leg_end, staff["staff_id"], existing):
                continue
            candidate = legs + [{
                "service_id": service["service_id"], "staff_id": staff["staff_id"],
                "staff_name": staff["staff_name"], "slot_start": leg_start, "slot_end": leg_end,
            }]
            result = await _try_extend_chain(
                candidate, remaining_services[1:], remaining_eligible[1:],
                duration_by_id, existing,
            )
            if result is not None:
                return result
    return None


async def get_available_slot_chains(
    shop_id: UUID,
    services: list[dict],
    start_date: date,
    end_date: date,
    max_results: int = 5,
) -> list[dict]:
    """Find up to `max_results` chains of consecutive slots across ordered
    `services` (each `{"service_id", "staff_id"}`, staff_id optional — None
    means auto-assign). Legs run in the given order; the gap between one
    leg's end and the next leg's start must satisfy gap_within_limit. Each
    chain: {"slot_start", "slot_end", "legs": [...]}.
    """
    if not services:
        return []

    svc_ids = [s["service_id"] for s in services]
    svc_rows = await execute(
        "SELECT id, duration_minutes FROM business_app_core.services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        svc_ids,
    )
    duration_by_id = {r["id"]: r["duration_minutes"] for r in svc_rows}
    if len(duration_by_id) != len(svc_ids):
        return []

    eligible_by_leg: list[list[dict]] = []
    for leg in services:
        staff = await _eligible_staff_for_leg(shop_id, leg["service_id"], leg.get("staff_id"))
        if not staff:
            return []
        eligible_by_leg.append(staff)

    all_staff_ids = list({s["staff_id"] for staff in eligible_by_leg for s in staff})
    from_ts = datetime.combine(start_date, time(0, 0), tzinfo=_ROME)
    to_ts = datetime.combine(end_date, time(23, 59), tzinfo=_ROME)
    existing = await execute(
        "SELECT staff_id, start_time, end_time FROM business_app_core.appointments "
        "WHERE staff_id = ANY($1::uuid[]) AND status NOT IN ('cancelled', 'no_show') "
        "AND start_time < $2 AND end_time > $3",
        all_staff_ids, to_ts, from_ts,
    )

    first = services[0]
    chains: list[dict] = []
    async for staff0, leg0_start, leg0_end in _iter_leg0_candidates(
        eligible_by_leg[0], duration_by_id[first["service_id"]], start_date, end_date, existing,
    ):
        legs = [{
            "service_id": first["service_id"], "staff_id": staff0["staff_id"],
            "staff_name": staff0["staff_name"], "slot_start": leg0_start, "slot_end": leg0_end,
        }]
        completed = await _try_extend_chain(
            legs, services[1:], eligible_by_leg[1:], duration_by_id, existing,
        )
        if completed is not None:
            chains.append({
                "slot_start": completed[0]["slot_start"],
                "slot_end": completed[-1]["slot_end"],
                "legs": completed,
            })
            if len(chains) >= max_results:
                break
    return chains
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/booking_engine/test_queries.py -k TestGetAvailableSlotChains -v`
Expected: PASS

- [ ] **Step 5: Run the full unit suite to confirm no regression**

Run: `pytest tests/booking_engine/test_queries.py -v`
Expected: all PASS (existing `TestCreateAppointment` etc. untouched)

- [ ] **Step 6: Commit**

```bash
git add booking_engine/db/queries.py tests/booking_engine/test_queries.py
git commit -m "feat(booking): add get_available_slot_chains for multi-staff chained bookings"
```

---

### Task 5: `queries.py` — `create_appointment_chain`

**Files:**
- Modify: `booking_engine/db/queries.py`
- Test: `tests/booking_engine/test_queries.py`

- [ ] **Step 1: Write the failing tests**

Add `create_appointment_chain` to the import block in `tests/booking_engine/test_queries.py` (alongside `create_appointment`, `get_available_slot_chains`, etc.).

Add a new test class:

```python
class TestCreateAppointmentChain:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_success(self, mock_exec, mock_one, mock_void):
        leg1_start = datetime(2026, 5, 5, 9, 0, tzinfo=_ROME)
        leg2_start = datetime(2026, 5, 5, 9, 30, tzinfo=_ROME)
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30, "price_eur": Decimal("35.00")},
             {"id": SVC2, "duration_minutes": 30, "price_eur": Decimal("20.00")}],  # durations
            [],  # leg1 overlap check
            [],  # leg2 overlap check
        ]
        mock_one.return_value = {"id": APPT, "status": "scheduled"}
        legs = [
            {"service_id": SVC, "staff_id": STAFF, "slot_start": leg1_start},
            {"service_id": SVC2, "staff_id": STAFF2, "slot_start": leg2_start},
        ]
        result = await create_appointment_chain(SHOP, CUSTOMER, legs)
        assert result["status"] == "scheduled"
        assert mock_void.await_count == 3  # 1 appointment insert + 2 appointment_services inserts

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_conflict_on_second_leg(self, mock_exec):
        leg1_start = datetime(2026, 5, 5, 9, 0, tzinfo=_ROME)
        leg2_start = datetime(2026, 5, 5, 9, 30, tzinfo=_ROME)
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30, "price_eur": Decimal("35.00")},
             {"id": SVC2, "duration_minutes": 30, "price_eur": Decimal("20.00")}],
            [],  # leg1 overlap check: clear
            [{"id": "existing"}],  # leg2 overlap check: conflict
        ]
        legs = [
            {"service_id": SVC, "staff_id": STAFF, "slot_start": leg1_start},
            {"service_id": SVC2, "staff_id": STAFF2, "slot_start": leg2_start},
        ]
        with pytest.raises(SlotConflictError):
            await create_appointment_chain(SHOP, CUSTOMER, legs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/booking_engine/test_queries.py -k TestCreateAppointmentChain -v`
Expected: FAIL with `ImportError: cannot import name 'create_appointment_chain'`

- [ ] **Step 3: Implement**

In `booking_engine/db/queries.py`, add after `create_appointment`:

```python
async def create_appointment_chain(
    shop_id: UUID,
    customer_id: UUID,
    legs: list[dict],
    notes: str | None = None,
) -> dict:
    """Create one appointment spanning `legs` (ordered
    `{"service_id", "staff_id", "slot_start"}`, normally copied verbatim
    from a get_available_slot_chains result), plus one appointment_services
    row per leg with its own staff_id/start_time/duration/price — matching
    the schema's per-service staff assignment. Raises SlotConflictError if
    any leg's staff is no longer free.
    """
    svc_ids = [leg["service_id"] for leg in legs]
    svc_rows = await execute(
        "SELECT id, duration_minutes, price_eur FROM business_app_core.services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        svc_ids,
    )
    duration_by_id = {r["id"]: r["duration_minutes"] for r in svc_rows}
    price_by_id = {r["id"]: r["price_eur"] for r in svc_rows}

    resolved = []
    for leg in legs:
        duration = duration_by_id[leg["service_id"]]
        resolved.append({
            **leg,
            "slot_end": leg["slot_start"] + timedelta(minutes=duration),
            "duration_minutes": duration,
            "price_eur": price_by_id[leg["service_id"]],
        })

    for leg in resolved:
        overlap = await execute(
            "SELECT id FROM business_app_core.appointments "
            "WHERE staff_id = $1 AND status NOT IN ('cancelled', 'no_show') "
            "AND start_time < $2 AND end_time > $3",
            leg["staff_id"], leg["slot_end"], leg["slot_start"],
        )
        if overlap:
            raise SlotConflictError("Time slot conflicts with existing appointment")

    appt_id = uuid4()
    first, last = resolved[0], resolved[-1]
    await execute_void(
        "INSERT INTO business_app_core.appointments "
        "(id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        appt_id, shop_id, customer_id, first["staff_id"],
        first["slot_start"], last["slot_end"], notes,
    )
    for leg in resolved:
        await execute_void(
            "INSERT INTO business_app_core.appointment_services "
            "(appointment_id, service_id, staff_id, start_time, duration_minutes, price_eur) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            appt_id, leg["service_id"], leg["staff_id"], leg["slot_start"],
            leg["duration_minutes"], float(leg["price_eur"]) if leg["price_eur"] else None,
        )

    return await execute_one(
        "SELECT * FROM business_app_core.appointments WHERE id = $1", appt_id,
    )
```

Note: the existing single-leg `create_appointment` still leaves `appointment_services.start_time` NULL (a pre-existing gap, unrelated to this change — not being fixed here). `create_appointment_chain` populates it correctly for every leg since the schema supports it and multi-leg bookings need it to reconstruct per-leg times.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/booking_engine/test_queries.py -k TestCreateAppointmentChain -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/queries.py tests/booking_engine/test_queries.py
git commit -m "feat(booking): add create_appointment_chain for multi-staff bookings"
```

---

### Task 6: Generalize `check_availability`/`create_booking` to multi-service legs

**Files:**
- Modify: `booking_engine/api/voice_tool_models.py`
- Modify: `booking_engine/db/voice_tool_queries.py`
- Modify: `booking_engine/api/routes/voice_tools_booking.py`
- Test: `tests/voice_gateway/test_voice_tools_booking.py`

- [ ] **Step 1: Write the failing tests**

In `tests/voice_gateway/test_voice_tools_booking.py`, replace `test_check_availability_returns_slots` with:

```python
@pytest.mark.asyncio
async def test_check_availability_returns_chains():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    slot_start = now + timedelta(days=1, hours=10)
    slot_end = slot_start + timedelta(minutes=30)
    sid = uuid4()
    fake = [{"slot_start": slot_start, "slot_end": slot_end,
             "legs": [{"service_id": sid, "staff_id": uuid4(), "staff_name": "Giulia",
                       "slot_start": slot_start, "slot_end": slot_end}]}]
    with patch("booking_engine.api.routes.voice_tools_booking.find_availability",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/check_availability",
                headers=AUTH,
                json={"services": [{"service_id": str(sid)}], "max_results": 5},
            )
    body = r.json()
    assert body["ok"] is True
    assert len(body["data"]) == 1
    assert body["data"][0]["legs"][0]["staff_name"] == "Giulia"


@pytest.mark.asyncio
async def test_check_availability_multi_service_returns_chain_with_two_legs():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    leg1_start = now + timedelta(days=1, hours=13)
    leg1_end = leg1_start + timedelta(minutes=90)
    leg2_start = leg1_end
    leg2_end = leg2_start + timedelta(minutes=30)
    svc1, svc2 = uuid4(), uuid4()
    staff1, staff2 = uuid4(), uuid4()
    fake = [{
        "slot_start": leg1_start, "slot_end": leg2_end,
        "legs": [
            {"service_id": svc1, "staff_id": staff1, "staff_name": "Marco",
             "slot_start": leg1_start, "slot_end": leg1_end},
            {"service_id": svc2, "staff_id": staff2, "staff_name": "Giulia",
             "slot_start": leg2_start, "slot_end": leg2_end},
        ],
    }]
    with patch("booking_engine.api.routes.voice_tools_booking.find_availability",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/check_availability",
                headers=AUTH,
                json={"services": [{"service_id": str(svc1)},
                                   {"service_id": str(svc2), "staff_id": str(staff2)}]},
            )
    body = r.json()
    assert body["ok"] is True
    legs = body["data"][0]["legs"]
    assert len(legs) == 2
    assert legs[0]["staff_name"] == "Marco" and legs[1]["staff_name"] == "Giulia"
    assert legs[0]["slot_end"] == legs[1]["slot_start"]
```

Replace `test_create_booking_inserts_and_attaches_to_call`, `test_create_booking_slot_taken_returns_error`, `test_create_booking_rejects_unknown_service`, `test_create_booking_rejects_past_slot` with:

```python
@pytest.mark.asyncio
async def test_create_booking_inserts_and_attaches_to_call():
    appt_id = uuid4()
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    svc, staff = uuid4(), uuid4()
    with patch("booking_engine.api.routes.voice_tools_booking.service_belongs_to_shop",
               new=AsyncMock(return_value=True)), \
         patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=AsyncMock(return_value={
                   "id": appt_id, "slot_start": future,
                   "slot_end": future + timedelta(minutes=30),
                   "staff_id": staff,
                   "confirmation_status": "confirmed",
                   "legs": [{"service_id": svc, "staff_id": staff,
                            "slot_start": future, "slot_end": future + timedelta(minutes=30)}],
               })), \
         patch("booking_engine.api.routes.voice_tools_booking.attach_booking_to_call",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()),
                      "legs": [{"service_id": str(svc), "staff_id": str(staff),
                               "slot_start": future.isoformat()}]},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["appointment_id"] == str(appt_id)
    assert body["data"]["legs"][0]["staff_id"] == str(staff)


@pytest.mark.asyncio
async def test_create_booking_multi_service_passes_all_legs_through():
    appt_id = uuid4()
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    svc1, svc2, staff1, staff2 = uuid4(), uuid4(), uuid4(), uuid4()
    future2 = future + timedelta(minutes=90)
    insert = AsyncMock(return_value={
        "id": appt_id, "slot_start": future, "slot_end": future2 + timedelta(minutes=30),
        "staff_id": staff1, "confirmation_status": "confirmed",
        "legs": [
            {"service_id": svc1, "staff_id": staff1, "slot_start": future, "slot_end": future2},
            {"service_id": svc2, "staff_id": staff2, "slot_start": future2,
             "slot_end": future2 + timedelta(minutes=30)},
        ],
    })
    with patch("booking_engine.api.routes.voice_tools_booking.service_belongs_to_shop",
               new=AsyncMock(return_value=True)), \
         patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=insert), \
         patch("booking_engine.api.routes.voice_tools_booking.attach_booking_to_call",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()), "legs": [
                    {"service_id": str(svc1), "staff_id": str(staff1),
                     "slot_start": future.isoformat()},
                    {"service_id": str(svc2), "staff_id": str(staff2),
                     "slot_start": future2.isoformat()},
                ]},
            )
    body = r.json()
    assert body["ok"] is True
    assert len(body["data"]["legs"]) == 2
    _, kwargs = insert.call_args
    assert len(kwargs["legs"]) == 2


@pytest.mark.asyncio
async def test_create_booking_slot_taken_returns_error():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with patch("booking_engine.api.routes.voice_tools_booking.service_belongs_to_shop",
               new=AsyncMock(return_value=True)), \
         patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=AsyncMock(side_effect=RuntimeError("slot_taken"))):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()),
                      "legs": [{"service_id": str(uuid4()), "staff_id": str(uuid4()),
                               "slot_start": future.isoformat()}]},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "slot_taken"


@pytest.mark.asyncio
async def test_create_booking_rejects_unknown_service():
    insert = AsyncMock()
    with patch("booking_engine.api.routes.voice_tools_booking.service_belongs_to_shop",
               new=AsyncMock(return_value=False)), \
         patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=insert):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()),
                      "legs": [{"service_id": str(uuid4()), "staff_id": str(uuid4()),
                               "slot_start": (datetime.now(timezone.utc)
                                              + timedelta(days=1)).isoformat()}]},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unknown_service"
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_booking_rejects_past_slot():
    insert = AsyncMock()
    with patch("booking_engine.api.routes.voice_tools_booking.service_belongs_to_shop",
               new=AsyncMock(return_value=True)), \
         patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=insert):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()),
                      "legs": [{"service_id": str(uuid4()), "staff_id": str(uuid4()),
                               "slot_start": (datetime.now(timezone.utc)
                                              - timedelta(hours=1)).isoformat()}]},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "slot_in_past"
    insert.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/voice_gateway/test_voice_tools_booking.py -v`
Expected: FAIL — 422s on the new `services`/`legs` request shapes (current models still expect `service_id`/`staff_id`).

- [ ] **Step 3: Update `voice_tool_models.py`**

Replace the `# Availability + booking` section:

```python
# Availability + booking
class AvailabilitySlot(BaseModel):
    slot_start: datetime
    slot_end: datetime
    staff_id: UUID
    staff_name: str


class CreateBookingIn(BaseModel):
    customer_id: UUID
    service_id: UUID
    slot_start: datetime
    staff_id: UUID


class BookingOut(BaseModel):
    appointment_id: UUID
    confirmation_status: Literal[
        "confirmed", "pending_sms_confirmation", "verification_failed"
    ]
    slot_start: datetime
    slot_end: datetime
    staff_id: UUID
```

with:

```python
# Availability + booking
class BookingServiceIn(BaseModel):
    """One requested leg of a (possibly multi-service) booking, in the
    order the services should be performed."""
    service_id: UUID
    staff_id: UUID | None = None  # None = auto-assign an eligible, available staff member


class CheckAvailabilityIn(BaseModel):
    services: list[BookingServiceIn] = Field(..., min_length=1)
    preferred_when: datetime | None = None
    max_results: int = 5


class AvailabilityLeg(BaseModel):
    service_id: UUID
    staff_id: UUID
    staff_name: str
    slot_start: datetime
    slot_end: datetime


class AvailabilityChain(BaseModel):
    slot_start: datetime
    slot_end: datetime
    legs: list[AvailabilityLeg]


class CreateBookingLeg(BaseModel):
    service_id: UUID
    staff_id: UUID
    slot_start: datetime


class CreateBookingIn(BaseModel):
    customer_id: UUID
    legs: list[CreateBookingLeg] = Field(..., min_length=1)


class BookingLegOut(BaseModel):
    service_id: UUID
    staff_id: UUID
    slot_start: datetime
    slot_end: datetime


class BookingOut(BaseModel):
    appointment_id: UUID
    confirmation_status: Literal[
        "confirmed", "pending_sms_confirmation", "verification_failed"
    ]
    slot_start: datetime
    slot_end: datetime
    legs: list[BookingLegOut]
```

- [ ] **Step 4: Update `voice_tool_queries.py`**

Replace `find_availability`:

```python
async def find_availability(
    *, shop_id: UUID, services: list[dict],
    preferred_when: datetime | None,
    max_results: int,
) -> list[dict]:
    """Open slots for the ordered `services` list — delegates to the
    ground-truth booking layer. A single-service request reuses the
    existing single-staff slot search unchanged; multiple services go
    through the chain search (different staff per leg, ordered, gapped).
    """
    from datetime import datetime, timedelta

    from booking_engine.db import queries

    start_date = (preferred_when or datetime.utcnow()).date()
    end_date = start_date + timedelta(days=14)

    if len(services) == 1:
        leg = services[0]
        slots = await queries.get_available_slots(
            shop_id=shop_id, service_ids=[leg["service_id"]],
            start_date=start_date, end_date=end_date, staff_id=leg.get("staff_id"),
        )
        return [
            {
                "slot_start": s["slot_start"], "slot_end": s["slot_end"],
                "legs": [{
                    "service_id": leg["service_id"], "staff_id": s["staff_id"],
                    "staff_name": s["staff_name"],
                    "slot_start": s["slot_start"], "slot_end": s["slot_end"],
                }],
            }
            for s in slots[:max_results]
        ]

    return await queries.get_available_slot_chains(
        shop_id=shop_id, services=services,
        start_date=start_date, end_date=end_date, max_results=max_results,
    )
```

Replace `insert_booking_locked`:

```python
async def insert_booking_locked(
    *, shop_id: UUID, customer_id: UUID, legs: list[dict],
) -> dict:
    """Create the appointment via the ground-truth layer. Raises on conflict.

    `legs` is the ordered list of {"service_id", "staff_id", "slot_start"} —
    exactly what create_booking receives, normally copied from a chosen
    check_availability chain. A single leg reuses the existing single-staff
    create_appointment path unchanged; multiple legs go through
    create_appointment_chain.

    ponytail: relies on the create_appointment*/_chain overlap check (no
    advisory lock). Add one only if concurrent voice bookings for the same
    staff+slot become a real problem.
    """
    from datetime import timedelta

    from booking_engine.db import queries

    try:
        if len(legs) == 1:
            leg = legs[0]
            row = await queries.create_appointment(
                shop_id=shop_id, customer_id=customer_id, staff_id=leg["staff_id"],
                service_ids=[leg["service_id"]], start_time=leg["slot_start"],
            )
        else:
            row = await queries.create_appointment_chain(
                shop_id=shop_id, customer_id=customer_id, legs=legs,
            )
    except queries.SlotConflictError:
        raise RuntimeError("slot_taken")
    except asyncpg.exceptions.ForeignKeyViolationError as e:
        # Both appointments.staff_id and appointment_services.staff_id FKs
        # are named *_staff_id_fkey; anything else user-supplied and
        # unvalidated before this call is the customer_id FK.
        if "staff" in (e.constraint_name or ""):
            raise RuntimeError("invalid_staff")
        raise RuntimeError("invalid_customer")

    svc_ids = [leg["service_id"] for leg in legs]
    durations = await connection.execute(
        "SELECT id, duration_minutes FROM business_app_core.services "
        "WHERE id = ANY($1::uuid[])",
        svc_ids,
    )
    duration_by_id = {d["id"]: d["duration_minutes"] for d in durations}
    out_legs = [
        {
            "service_id": leg["service_id"], "staff_id": leg["staff_id"],
            "slot_start": leg["slot_start"],
            "slot_end": leg["slot_start"] + timedelta(minutes=duration_by_id[leg["service_id"]]),
        }
        for leg in legs
    ]
    return {
        "id": row["id"],
        "slot_start": out_legs[0]["slot_start"],
        "slot_end": out_legs[-1]["slot_end"],
        "staff_id": row["staff_id"],
        "confirmation_status": row.get("confirmation_status") or "confirmed",
        "legs": out_legs,
    }
```

- [ ] **Step 5: Update `voice_tools_booking.py`**

Update the import from `voice_tool_models` (drop `AvailabilitySlot`, add `AvailabilityChain`/`CheckAvailabilityIn`):

```python
from booking_engine.api.voice_tool_models import (
    AvailabilityChain, BookingOut, CancelBookingIn, CheckAvailabilityIn,
    CreateBookingIn, Envelope, ModifyBookingIn,
)
```

Delete the local `class CheckAvailabilityIn(BaseModel): ...` block entirely (it now comes from `voice_tool_models`).

Replace the `check_availability` and `create_booking` handlers:

```python
@router.post("/check_availability")
async def check_availability(
    body: CheckAvailabilityIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[AvailabilityChain]]:
    services = [{"service_id": s.service_id, "staff_id": s.staff_id} for s in body.services]
    rows = await find_availability(
        shop_id=x_shop_id, services=services,
        preferred_when=body.preferred_when, max_results=body.max_results,
    )
    out = [AvailabilityChain(**r) for r in rows]
    return Envelope[list[AvailabilityChain]](ok=True, data=out)


@router.post("/create_booking")
async def create_booking(
    body: CreateBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[BookingOut]:
    for leg in body.legs:
        if not await service_belongs_to_shop(shop_id=x_shop_id, service_id=leg.service_id):
            return Envelope[BookingOut](ok=False, error="unknown_service")
    now = datetime.now(timezone.utc)
    if any(slot_in_past(leg.slot_start, now) for leg in body.legs):
        return Envelope[BookingOut](ok=False, error="slot_in_past")
    try:
        row = await insert_booking_locked(
            shop_id=x_shop_id, customer_id=body.customer_id,
            legs=[{"service_id": l.service_id, "staff_id": l.staff_id,
                   "slot_start": l.slot_start} for l in body.legs],
        )
    except RuntimeError as e:
        return Envelope[BookingOut](ok=False, error=str(e))
    await attach_booking_to_call(call_id=x_call_id, appointment_id=row["id"])
    return Envelope[BookingOut](
        ok=True,
        data=BookingOut(
            appointment_id=row["id"],
            confirmation_status=row["confirmation_status"],
            slot_start=row["slot_start"], slot_end=row["slot_end"],
            legs=row["legs"],
        ),
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_voice_tools_booking.py -v`
Expected: PASS (all tests in the file, including the unmodified modify/cancel ones)

- [ ] **Step 7: Run the full non-live-db suite**

Run: `pytest tests/ --ignore=tests/live_db -q`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add booking_engine/api/voice_tool_models.py booking_engine/db/voice_tool_queries.py booking_engine/api/routes/voice_tools_booking.py tests/voice_gateway/test_voice_tools_booking.py
git commit -m "feat(voice-tools): generalize check_availability/create_booking to multi-service legs"
```

---

### Task 7: `safety_layer` — multi-service tool schemas + ordering rule

**Files:**
- Modify: `booking_engine/services/safety_layer.py`
- Test: `tests/voice_gateway/test_safety_layer.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/voice_gateway/test_safety_layer.py`:

```python
def test_safety_prompt_mentions_multi_service_ordering_rule():
    text = SAFETY_PROMPT.lower()
    assert "servizi multipli" in text


def test_check_availability_schema_requires_services_list():
    schema = _TOOL_SCHEMAS["check_availability"]["parameters"]
    assert schema["required"] == ["services"]
    assert schema["properties"]["services"]["type"] == "array"


def test_create_booking_schema_requires_legs_list():
    schema = _TOOL_SCHEMAS["create_booking"]["parameters"]
    assert schema["required"] == ["customer_id", "legs"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/voice_gateway/test_safety_layer.py -v`
Expected: FAIL — schemas still have the old `service_id`/`staff_id` shape, prompt has no ordering rule

- [ ] **Step 3: Update the tool schemas**

In `booking_engine/services/safety_layer.py`, replace the `check_availability` entry:

```python
    "check_availability": {
        "name": "check_availability",
        "description": "Slot disponibili per un servizio. Restituisce fino a 5 opzioni.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "preferred_when": {"type": "string", "description": "ISO 8601"},
                "staff_id": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["service_id"],
        },
    },
```

with:

```python
    "check_availability": {
        "name": "check_availability",
        "description": (
            "Trova combinazioni di orari disponibili per uno o più servizi, "
            "nell'ordine in cui vanno eseguiti (es. colore poi piega, con "
            "operatori anche diversi). Restituisce fino a max_results "
            "combinazioni complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "services": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "service_id": {"type": "string"},
                            "staff_id": {
                                "type": "string",
                                "description": (
                                    "Opzionale: solo se il cliente ha chiesto un "
                                    "operatore specifico per questo servizio."
                                ),
                            },
                        },
                        "required": ["service_id"],
                    },
                },
                "preferred_when": {"type": "string", "description": "ISO 8601"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["services"],
        },
    },
```

Replace the `create_booking` entry:

```python
    "create_booking": {
        "name": "create_booking",
        "description": "Crea una prenotazione confermata.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "service_id": {"type": "string"},
                "slot_start": {"type": "string"},
                "staff_id": {"type": "string"},
            },
            "required": ["customer_id", "service_id", "slot_start", "staff_id"],
        },
    },
```

with:

```python
    "create_booking": {
        "name": "create_booking",
        "description": (
            "Crea una prenotazione confermata con uno o più servizi, usando "
            "esattamente gli orari e gli operatori restituiti da "
            "check_availability."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "legs": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "service_id": {"type": "string"},
                            "staff_id": {"type": "string"},
                            "slot_start": {"type": "string", "description": "ISO 8601"},
                        },
                        "required": ["service_id", "staff_id", "slot_start"],
                    },
                },
            },
            "required": ["customer_id", "legs"],
        },
    },
```

- [ ] **Step 4: Add the ordering rule to `SAFETY_PROMPT`**

Right after the `PREZZI` rule added in Task 2, add:

```
- SERVIZI MULTIPLI: se il cliente prenota più servizi nella stessa visita \
(es. colore e piega, con operatori anche diversi), passali a \
check_availability nell'ordine corretto secondo la prassi comune del \
settore acconciatura (es. colore e altri trattamenti chimici prima di \
piega, taglio o styling), a meno che il cliente non specifichi un ordine \
diverso.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/voice_gateway/test_safety_layer.py tests/voice_gateway/test_mcp_tools.py -v`
Expected: PASS. `test_tool_defs_expose_the_twelve_tools` in `test_mcp_tools.py` should still pass unchanged (tool count/names didn't change, only their parameter schemas).

- [ ] **Step 6: Commit**

```bash
git add booking_engine/services/safety_layer.py tests/voice_gateway/test_safety_layer.py
git commit -m "feat(voice-tools): update check_availability/create_booking schemas + ordering rule"
```

---

### Task 8: Update `tests/live_db/*` to the new wire shape

**Files:**
- Modify: `tests/live_db/test_tool_dispatch_reads.py`
- Modify: `tests/live_db/test_tool_dispatch_writes.py`
- Modify: `tests/live_db/test_tool_dispatch_security.py`

These tests are skipped without `DATABASE_URL` (`pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), ...)`), so they can't be executed in this environment — CLAUDE.md's own history notes this suite is normally run against the QA Neon branch separately. Update them for correctness now so they aren't silently broken next time someone runs them; there is no "run and verify" step for this task, only "edit and confirm the payload shape matches Task 6's models."

- [ ] **Step 1: Update `test_tool_dispatch_reads.py`**

Replace `test_check_availability_returns_slots`:

```python
async def test_check_availability_returns_chain(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    preferred = datetime.combine(_next_weekday(), datetime.min.time(), tzinfo=timezone.utc)

    resp = await execute_tool(
        "check_availability",
        {"services": [{"service_id": str(SVC_TAGLIO_UOMO), "staff_id": str(STAFF_MIRCO)}],
         "preferred_when": preferred.isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert len(resp["data"]) > 0
    assert resp["data"][0]["legs"][0]["staff_id"] == str(STAFF_MIRCO)
```

- [ ] **Step 2: Update `test_tool_dispatch_writes.py`**

Replace the `create_booking` call in `test_create_booking_persists_appointment`:

```python
    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]),
         "legs": [{"service_id": str(SVC_TAGLIO_UOMO), "staff_id": str(STAFF_MIRCO),
                   "slot_start": start.isoformat()}]},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )
```

(Rest of the test body is unchanged.)

- [ ] **Step 3: Update `test_tool_dispatch_security.py`**

Replace the `create_booking` call in `test_create_booking_rejects_slot_in_past`:

```python
    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]),
         "legs": [{"service_id": str(SVC_TAGLIO_UOMO), "staff_id": str(STAFF_MIRCO),
                   "slot_start": past.isoformat()}]},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )
```

Replace the call in `test_create_booking_nonexistent_customer_returns_clean_error`:

```python
    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(nonexistent_customer_id),
         "legs": [{"service_id": str(SVC_TAGLIO_UOMO), "staff_id": str(STAFF_MIRCO),
                   "slot_start": _slot(66).isoformat()}]},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )
```

Replace the call in `test_create_booking_nonexistent_staff_returns_clean_error`:

```python
    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]),
         "legs": [{"service_id": str(SVC_TAGLIO_UOMO), "staff_id": str(nonexistent_staff_id),
                   "slot_start": _slot(66).isoformat()}]},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )
```

Replace the call in `test_create_booking_missing_required_field_returns_clean_error` (still deliberately malformed — a missing `legs` field entirely, which is the multi-service equivalent of the old "missing slot_start/staff_id"):

```python
    resp = await execute_tool(
        "create_booking",
        {"customer_id": "not-a-real-uuid"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )
```

- [ ] **Step 4: Commit**

```bash
git add tests/live_db/test_tool_dispatch_reads.py tests/live_db/test_tool_dispatch_writes.py tests/live_db/test_tool_dispatch_security.py
git commit -m "test(live_db): update tool-dispatch tests to the multi-service legs wire shape"
```

---

### Task 9: Full suite run + wrap-up

**Files:** none (verification only)

- [ ] **Step 1: Run the full non-live-db suite**

Run: `pytest tests/ --ignore=tests/live_db -q`
Expected: all PASS

- [ ] **Step 2: Confirm `tests/live_db/*` still collects cleanly (syntax-only check, no DB needed)**

Run: `pytest tests/live_db --collect-only -q`
Expected: collection succeeds (tests themselves report `skipped` without `DATABASE_URL`, which is fine — this step only catches syntax/import errors introduced in Task 8)

- [ ] **Step 3: Update `CLAUDE.md` with a history entry**

Append a new dated entry at the top of `CLAUDE.md` (following the file's existing newest-first convention) summarizing: cost gated behind `include_price`; `check_availability`/`create_booking` generalized to ordered multi-service/multi-staff legs via new additive `get_available_slot_chains`/`create_appointment_chain` functions (existing single-service `get_available_slots`/`create_appointment` untouched, still used by `availability.py`/`appointments.py` and the single-leg voice path); `MAX_GAP_MINUTES = 20` fixed constant; ordering left to the model's own hairdressing-domain knowledge, no dependency table. Note the still-needed follow-up: run `tests/live_db/*` against the QA Neon branch to confirm the chain algorithm works against real schedules (only unit-tested with mocks here, per this repo's own `TEST_DATABASE_URL`-gated convention).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record cost-gating + multi-staff booking decision in CLAUDE.md"
```
