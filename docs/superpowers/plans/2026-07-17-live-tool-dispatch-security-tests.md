# Live Tool-Dispatch + Security Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Every task requires a real Neon connection string in `TEST_DATABASE_URL` (preferred) or `DATABASE_URL`, pointed at the QA branch or an ephemeral clone — never production (a `_PROD_HOST_FRAGMENT` guard in `tests/live_db/conftest.py` already refuses to run against prod and the whole suite skips instead).**

**Goal:** Close the one layer of the live, deployed voice agent (booking_engine's SIP/MCP path) that has zero live-database coverage today — the full dispatch chain `execute_tool()` → route handler → `safety_layer` authz/constraints → `queries.py` — with real reads, real writes, and real security-rejection paths proven against actual Neon-shaped data.

**Architecture:** Add three new test files under `tests/live_db/` (the directory CI already runs as `pytest tests/live_db/ -q`) that call `execute_tool()` — the exact function OpenAI's real MCP path calls — against a bare FastAPI app fixture holding only the four `voice_tools_*` routers, with the real connection pool from the existing `db_connection` autouse fixture and zero mocking of `verify_call_token`, `safety_layer`, or `queries.py`.

**Tech Stack:** pytest + pytest-asyncio (`asyncio_mode = auto`, no decorators needed), FastAPI `ASGITransport` (via `execute_tool()`'s own `app=` path, matching the existing test-client pattern in `tests/booking_engine/test_routes/conftest.py`), asyncpg (via the existing `booking_engine.db.connection` pool). No new dependencies.

---

### Task 1: Extend `tests/live_db/conftest.py` with dispatch fixtures

**Files:**
- Modify: `tests/live_db/conftest.py`

Four new fixtures: `tool_app` (a bare FastAPI app with only the tool routers, no lifespan — the module-level `db_connection` autouse fixture already owns the pool), `settings` (a plain `Settings()`, used for `openai_tool_secret`), and two cleanup fixtures (`cleanup_call_ids`, `cleanup_memo_ids`) following the exact pattern of the existing `cleanup_customer_ids`/`cleanup_appointment_ids`.

- [ ] **Step 1: Add the fixtures**

Open `tests/live_db/conftest.py`. Add this block at the end of the file (after the existing `cleanup_appointment_ids` fixture, keep everything above unchanged):

```python
@pytest.fixture
def tool_app():
    """Bare FastAPI app exposing only the voice tool routes exercised via
    execute_tool() — mirrors the real dispatch path with no lifespan (the
    autouse db_connection fixture above already owns the connection pool).
    """
    from fastapi import FastAPI

    from booking_engine.api.routes import (
        voice_tools_booking, voice_tools_catalog, voice_tools_identity,
        voice_tools_lifecycle,
    )

    app = FastAPI()
    app.include_router(voice_tools_catalog.router)
    app.include_router(voice_tools_booking.router)
    app.include_router(voice_tools_identity.router)
    app.include_router(voice_tools_lifecycle.router)
    return app


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
async def cleanup_call_ids():
    """Collect voice_agent.calls IDs created during tests for cleanup."""
    ids: list = []
    yield ids
    for cid in ids:
        uid = UUID(cid) if isinstance(cid, str) else cid
        try:
            await connection.execute_void(
                "UPDATE business_app_core.appointments SET voice_call_id = NULL "
                "WHERE voice_call_id = $1", uid,
            )
            await connection.execute_void(
                "DELETE FROM voice_agent.auth_events WHERE call_id = $1", uid,
            )
            await connection.execute_void(
                "DELETE FROM voice_agent.callback_memos WHERE call_id = $1", uid,
            )
            await connection.execute_void(
                "DELETE FROM voice_agent.calls WHERE id = $1", uid,
            )
        except Exception as e:
            logger.warning("Cleanup failed for call %s: %s", cid, e)


@pytest.fixture
async def cleanup_memo_ids():
    """Collect voice_agent.callback_memos IDs created during tests for cleanup."""
    ids: list = []
    yield ids
    for mid in ids:
        uid = UUID(mid) if isinstance(mid, str) else mid
        try:
            await connection.execute_void(
                "DELETE FROM voice_agent.callback_memos WHERE id = $1", uid,
            )
        except Exception as e:
            logger.warning("Cleanup failed for memo %s: %s", mid, e)
```

- [ ] **Step 2: Sanity-check the module still imports**

```bash
export PYTHONPATH=. && python3 -c "import tests.live_db.conftest; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add tests/live_db/conftest.py
git commit -m "test(live-db): add tool-dispatch fixtures (tool_app, settings, call/memo cleanup)"
```

---

### Task 2: `tests/live_db/test_tool_dispatch_reads.py` — read-tool coverage

**Files:**
- Create: `tests/live_db/test_tool_dispatch_reads.py`

One test per read tool (`lookup_customer`, `get_services`, `get_staff_for_service`, `check_availability`, `get_booking`), each calling `execute_tool()` — the real dispatch function — against real seeded data (`SHOP_ID`, `STAFF_MIRCO`, `SVC_TAGLIO_UOMO`, `CUSTOMER_MARIA`, `PHONE_MARIA` from `tests/live_db/conftest.py`).

- [ ] **Step 1: Write the tests**

Create `tests/live_db/test_tool_dispatch_reads.py`:

```python
"""Live DB tests — read tools via the real dispatch path (execute_tool() →
route handler → queries), against real Neon-shaped data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from booking_engine.db.queries import create_appointment
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import execute_tool
from tests.live_db.conftest import (
    CUSTOMER_MARIA, PHONE_MARIA, SHOP_ID, STAFF_MIRCO, SVC_TAGLIO_UOMO,
)


def _next_weekday() -> date:
    """A date 45+ days out that's Mon-Sat — isolated from the other live_db
    suites' own booking windows (they use +0..+33 days) so availability and
    booking assertions here never collide with theirs."""
    d = date.today() + timedelta(days=45)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def _token(shop_id, call_id, settings) -> str:
    return mint_call_token(shop_id=shop_id, call_id=call_id, secret=settings.openai_tool_secret)


async def test_lookup_customer_returns_seeded_customer(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(
        shop_id=SHOP_ID, caller_phone=PHONE_MARIA, matched_customer_id=CUSTOMER_MARIA,
    )
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "lookup_customer", {"phone": PHONE_MARIA},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert any(c["customer_id"] == str(CUSTOMER_MARIA) for c in resp["data"])


async def test_get_services_returns_seeded_service(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "get_services", {}, token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert any(s["service_id"] == str(SVC_TAGLIO_UOMO) for s in resp["data"])


async def test_get_staff_for_service_returns_seeded_staff(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "get_staff_for_service", {"service_id": str(SVC_TAGLIO_UOMO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert any(s["staff_id"] == str(STAFF_MIRCO) for s in resp["data"])


async def test_check_availability_returns_slots(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    preferred = datetime.combine(_next_weekday(), datetime.min.time(), tzinfo=timezone.utc)

    resp = await execute_tool(
        "check_availability",
        {"service_id": str(SVC_TAGLIO_UOMO), "preferred_when": preferred.isoformat(),
         "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert len(resp["data"]) > 0
    assert resp["data"][0]["staff_id"] == str(STAFF_MIRCO)


async def test_get_booking_returns_customers_next_appointment(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_appointment_ids,
):
    start = datetime.combine(_next_weekday(), datetime.min.time().replace(hour=11),
                             tzinfo=timezone.utc)
    appt = await create_appointment(
        SHOP_ID, CUSTOMER_MARIA, STAFF_MIRCO, [SVC_TAGLIO_UOMO], start,
    )
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(
        shop_id=SHOP_ID, caller_phone=PHONE_MARIA, matched_customer_id=CUSTOMER_MARIA,
    )
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "get_booking", {"customer_id": str(CUSTOMER_MARIA)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert resp["data"]["id"] == str(appt["id"])
```

- [ ] **Step 2: Run against the QA branch**

```bash
export PYTHONPATH=. && export TEST_DATABASE_URL=<QA branch connection string>
pytest tests/live_db/test_tool_dispatch_reads.py -v
```
Expected: `5 passed`. If `openai_tool_secret` is empty in your environment, `require_tool_token` raises a 500 and every test fails with that instead — set `OPENAI_TOOL_SECRET` (matches whatever the QA Fly app is deployed with; any non-empty value works locally since both the token mint and the route dependency read the same env var in-process).

- [ ] **Step 3: Commit**

```bash
git add tests/live_db/test_tool_dispatch_reads.py
git commit -m "test(live-db): add read-tool dispatch coverage against real Neon data"
```

---

### Task 3: `tests/live_db/test_tool_dispatch_writes.py` — write-tool coverage

**Files:**
- Create: `tests/live_db/test_tool_dispatch_writes.py`

One test per write tool (`create_customer_from_call`, `update_customer_from_call`, `create_booking`, `modify_booking`, `cancel_booking`, `mark_outcome`, `escalate_to_merchant`). Each asserts on a follow-up direct read of the row, not just the tool's return value.

- [ ] **Step 1: Write the tests**

Create `tests/live_db/test_tool_dispatch_writes.py`:

```python
"""Live DB tests — write tools via the real dispatch path (execute_tool() →
route handler → safety_layer authz/constraints → queries), against real
Neon-shaped data. Every assertion re-reads the row directly, not just the
tool's return value.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from booking_engine.db import connection
from booking_engine.db.queries import create_appointment, create_customer
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import execute_tool
from tests.live_db.conftest import SHOP_ID, STAFF_MIRCO, SVC_TAGLIO_UOMO


def _next_weekday(offset_days: int) -> date:
    """A Mon-Sat date `offset_days` out. Callers below space their offsets
    (50/51/52) apart so their created appointments never collide with each
    other, with test_tool_dispatch_reads.py's +45 day window, or with the
    other live_db suites' own +0..+33 day windows."""
    d = date.today() + timedelta(days=offset_days)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def _slot(offset_days: int, hour: int = 11) -> datetime:
    return datetime.combine(_next_weekday(offset_days), datetime.min.time().replace(hour=hour),
                            tzinfo=timezone.utc)


def _token(shop_id, call_id, settings) -> str:
    return mint_call_token(shop_id=shop_id, call_id=call_id, secret=settings.openai_tool_secret)


async def test_create_customer_from_call_persists_row(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone="+39 333 9990001",
                                matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "create_customer_from_call",
        {"phone": "+39 333 9990001", "first_name": "Dispatch", "last_name": "Test",
         "phone_source": "caller_id"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    new_id = resp["data"]["customer_id"]
    cleanup_customer_ids.append(new_id)

    row = await connection.execute_one(
        "SELECT full_name, phone_verified FROM business_app_core.customers WHERE id = $1",
        new_id,
    )
    assert row["full_name"] == "Dispatch Test"
    assert row["phone_verified"] is True

    call_row = await connection.execute_one(
        "SELECT created_customer_id FROM voice_agent.calls WHERE id = $1", call_id,
    )
    assert str(call_row["created_customer_id"]) == str(new_id)


async def test_update_customer_from_call_persists_change(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch UpdateTest")
    cleanup_customer_ids.append(customer["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "update_customer_from_call",
        {"customer_id": str(customer["id"]), "field": "email",
         "value": "dispatch-test@example.com"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT email FROM business_app_core.customers WHERE id = $1", customer["id"],
    )
    assert row["email"] == "dispatch-test@example.com"


async def test_create_booking_persists_appointment(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch CreateBooking")
    cleanup_customer_ids.append(customer["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    start = _slot(50)

    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]), "service_id": str(SVC_TAGLIO_UOMO),
         "slot_start": start.isoformat(), "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    appt_id = resp["data"]["appointment_id"]
    cleanup_appointment_ids.append(appt_id)
    # Break the calls -> appointment soft-link now so cleanup_appointment_ids'
    # DELETE below (which runs before cleanup_call_ids in fixture teardown)
    # can't be blocked by a lingering reference from voice_agent.calls.
    await connection.execute_void(
        "UPDATE voice_agent.calls SET created_booking_id = NULL WHERE id = $1", call_id,
    )

    row = await connection.execute_one(
        "SELECT status, voice_call_id FROM business_app_core.appointments WHERE id = $1",
        appt_id,
    )
    assert row["status"] == "scheduled"
    assert str(row["voice_call_id"]) == str(call_id)


async def test_modify_booking_authorized_changes_slot(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990002"
    customer = await create_customer(SHOP_ID, "Dispatch ModifyTest", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(51)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    new_start = start + timedelta(hours=2)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]), "new_slot_start": new_start.isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT start_time FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert abs((row["start_time"] - new_start).total_seconds()) < 1


async def test_cancel_booking_authorized_cancels(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990003"
    customer = await create_customer(SHOP_ID, "Dispatch CancelTest", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(52)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "cancel_booking", {"appointment_id": str(appt["id"])},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT status FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert row["status"] == "cancelled"


async def test_mark_outcome_persists_on_call_row(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "mark_outcome",
        {"outcome": "info", "summary": "Dispatch test summary"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT outcome, summary FROM voice_agent.calls WHERE id = $1", call_id,
    )
    assert row["outcome"] == "info"
    assert row["summary"] == "Dispatch test summary"


async def test_escalate_to_merchant_creates_memo(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_memo_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone="+39 333 9990004",
                                matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "escalate_to_merchant",
        {"reason": "vuole parlare con il salone",
         "customer_message": "Richiamatemi per favore"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    memo_id = resp["data"]["memo_id"]
    cleanup_memo_ids.append(memo_id)

    row = await connection.execute_one(
        "SELECT reason, caller_phone FROM voice_agent.callback_memos WHERE id = $1", memo_id,
    )
    assert "vuole parlare con il salone" in row["reason"]
    assert row["caller_phone"] == "+39 333 9990004"

    call_row = await connection.execute_one(
        "SELECT outcome FROM voice_agent.calls WHERE id = $1", call_id,
    )
    assert call_row["outcome"] == "escalated"
```

- [ ] **Step 2: Run against the QA branch**

```bash
export PYTHONPATH=. && export TEST_DATABASE_URL=<QA branch connection string>
pytest tests/live_db/test_tool_dispatch_writes.py -v
```
Expected: `7 passed`

- [ ] **Step 3: Commit**

```bash
git add tests/live_db/test_tool_dispatch_writes.py
git commit -m "test(live-db): add write-tool dispatch coverage against real Neon data"
```

---

### Task 4: `tests/live_db/test_tool_dispatch_security.py` — the layer with zero coverage today

**Files:**
- Create: `tests/live_db/test_tool_dispatch_security.py`

Token integrity, cross-shop authz, phone-mismatch authz, lead-time/past-slot constraints, malformed input, and unknown-tool-name rejection — all against real rows, not mocks. Two facts this file depends on, confirmed by reading the route code directly:
- `modify_booking`/`cancel_booking` authorize using the **call row's stored `shop_id`** (read back via `get_call()`), not the `X-Shop-Id` header — so the cross-shop tests must insert the call itself under the "wrong" shop, not just mint a token claiming a different shop.
- `create_customer_from_call`'s identity match (`lookup_customer`) reads `customers.phone_normalized`, but appointment-ownership authz (`modify_booking`/`cancel_booking`) reads `phone_contacts` — two different columns for two different features. The phone-mismatch test below uses `create_customer(..., phone_number=...)`, which populates `phone_contacts` (confirmed in `booking_engine/db/queries.py`), matching what `get_appointment_owner()` actually reads.

- [ ] **Step 1: Write the tests**

Create `tests/live_db/test_tool_dispatch_security.py`:

```python
"""Live DB tests — security-critical paths of the real dispatch chain:
token integrity, cross-shop authz, phone-mismatch authz, and constraint
enforcement, all proven against real rows (not mocks).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from booking_engine.db import connection
from booking_engine.db.queries import create_appointment, create_customer
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import execute_tool
from tests.live_db.conftest import SHOP_ID, SHOP_ID_2, STAFF_MIRCO, SVC_TAGLIO_UOMO


def _next_weekday(offset_days: int) -> date:
    d = date.today() + timedelta(days=offset_days)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def _slot(offset_days: int, hour: int = 11) -> datetime:
    """A Mon-Sat slot far enough out to be outside any cancellation lead time.
    Offsets 60/61/62 are spaced apart from each other and from the other
    live_db suites' +0..+52 day windows."""
    return datetime.combine(_next_weekday(offset_days), datetime.min.time().replace(hour=hour),
                            tzinfo=timezone.utc)


def _token(shop_id, call_id, settings) -> str:
    return mint_call_token(shop_id=shop_id, call_id=call_id, secret=settings.openai_tool_secret)


async def test_tampered_token_rejected(db_connection, tool_app, settings, cleanup_call_ids):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    tampered = token[:-1] + ("x" if token[-1] != "x" else "y")

    resp = await execute_tool(
        "get_services", {}, token=tampered, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "unauthorized"}


async def test_token_signed_with_wrong_secret_rejected(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    wrong_token = mint_call_token(shop_id=SHOP_ID, call_id=call_id, secret="not-the-real-secret")

    resp = await execute_tool(
        "get_services", {}, token=wrong_token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "unauthorized"}


async def test_unknown_tool_name_rejected(db_connection, tool_app, settings, cleanup_call_ids):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "drop_all_tables", {}, token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "unknown_tool"}


async def test_modify_booking_rejects_call_from_different_shop(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990010"
    customer = await create_customer(SHOP_ID, "Dispatch CrossShopModify", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(60)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    # The call is recorded against SHOP_ID_2 — a different shop than the appointment.
    call_id = await insert_call(shop_id=SHOP_ID_2, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID_2, call_id, settings)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]),
         "new_slot_start": (start + timedelta(hours=2)).isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "wrong_shop"}
    row = await connection.execute_one(
        "SELECT start_time FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert abs((row["start_time"] - start).total_seconds()) < 1


async def test_cancel_booking_rejects_call_from_different_shop(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990011"
    customer = await create_customer(SHOP_ID, "Dispatch CrossShopCancel", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(61)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID_2, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID_2, call_id, settings)

    resp = await execute_tool(
        "cancel_booking", {"appointment_id": str(appt["id"])},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "wrong_shop"}
    row = await connection.execute_one(
        "SELECT status FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert row["status"] == "scheduled"


async def test_modify_booking_rejects_phone_mismatch(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    owner_phone = "+39 333 9990012"
    caller_phone = "+39 333 9990013"  # a different number calling in
    customer = await create_customer(SHOP_ID, "Dispatch PhoneMismatch", owner_phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(62)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=caller_phone,
                                matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]),
         "new_slot_start": (start + timedelta(hours=2)).isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "phone_mismatch"}


async def test_modify_booking_rejects_within_lead_time(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990014"
    customer = await create_customer(SHOP_ID, "Dispatch LeadTimeModify", phone)
    cleanup_customer_ids.append(customer["id"])
    # 1 hour out — inside the default 2-hour cancellation lead time.
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]),
         "new_slot_start": (start + timedelta(hours=3)).isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "reschedule_too_close"}


async def test_cancel_booking_rejects_within_lead_time(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990015"
    customer = await create_customer(SHOP_ID, "Dispatch LeadTimeCancel", phone)
    cleanup_customer_ids.append(customer["id"])
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "cancel_booking", {"appointment_id": str(appt["id"])},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "cancel_too_close"}


async def test_create_booking_rejects_slot_in_past(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch PastSlot")
    cleanup_customer_ids.append(customer["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    past = datetime.now(timezone.utc) - timedelta(days=1)

    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]), "service_id": str(SVC_TAGLIO_UOMO),
         "slot_start": past.isoformat(), "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "slot_in_past"}


async def test_create_booking_missing_required_field_returns_clean_error(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "create_booking",
        {"customer_id": "not-a-real-uuid", "service_id": str(SVC_TAGLIO_UOMO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    # FastAPI's own pydantic validation returns a 422 with a `detail` list,
    # not the tool's usual ok/error envelope — execute_tool() passes that
    # JSON through unchanged (r.json() succeeds for a 422). What matters
    # here is that it degrades to *some* clean JSON dict instead of a raw
    # traceback — OpenAI-generated tool arguments are untrusted input.
    assert resp.get("ok") is not True
    assert "Traceback" not in str(resp)
```

- [ ] **Step 2: Run against the QA branch**

```bash
export PYTHONPATH=. && export TEST_DATABASE_URL=<QA branch connection string>
pytest tests/live_db/test_tool_dispatch_security.py -v
```
Expected: `10 passed`

- [ ] **Step 3: Commit**

```bash
git add tests/live_db/test_tool_dispatch_security.py
git commit -m "test(live-db): add security dispatch coverage — token integrity, cross-shop and phone-mismatch authz, constraints"
```

---

### Task 5: Prove the security tests actually catch a regression

**Files:** none (verification only — no code changes committed from this task unless a real bug is found, see Step 4)

This is the check the design spec commits to: deliberately break one real security control, confirm the new test that guards it fails, then restore it. If any of these steps *doesn't* fail as expected, that's a real bug in `booking_engine/services/booking_authz.py` or `booking_engine/services/booking_constraints.py` worth fixing before this plan is considered done — not a plan step to skip.

- [ ] **Step 1: Break phone-mismatch enforcement**

Open `booking_engine/services/booking_authz.py`. Temporarily comment out the phone check (lines 30-32):

```python
    caller_digits = digits_only(caller_number)
    if not caller_digits:
        return False, "anonymous_caller"
    owner_digits = {digits_only(p) for p in owner.get("phones", [])}
    # if caller_digits not in owner_digits:
    #     return False, "phone_mismatch"
    return True, "ok"
```

- [ ] **Step 2: Confirm the corresponding test fails**

```bash
export PYTHONPATH=. && export TEST_DATABASE_URL=<QA branch connection string>
pytest tests/live_db/test_tool_dispatch_security.py::test_modify_booking_rejects_phone_mismatch -v
```
Expected: `FAILED` — the test caught the regression. If it still passes, stop and investigate before continuing; the test isn't actually verifying what it claims to.

- [ ] **Step 3: Revert the break**

```bash
git checkout -- booking_engine/services/booking_authz.py
```

- [ ] **Step 4: Confirm the full suite is green again**

```bash
pytest tests/live_db/ -v
```
Expected: all tests pass, including `test_modify_booking_rejects_phone_mismatch`.

---

### Task 6: Record the decision and run the full suite one last time

**Files:**
- Modify: `CLAUDE.md`

Per this repo's convention (`CLAUDE.md` is an append-only decision log — newest entry on top, never rewrite prior entries), add a short entry recording the coverage gap that was closed, the two dispatch-layer facts discovered while writing the tests (`modify_booking`/`cancel_booking` authorize off the call row's shop_id, not the header; `update_customer_from_call` has no shop-ownership check at all), and the deliberately out-of-scope finding (`voice_gateway/` dead code).

- [ ] **Step 1: Add the CLAUDE.md entry**

Open `CLAUDE.md`. Insert this new section directly below the `# Project History` header and its intro paragraph, above the existing `## 2026-07-16 — Telephony provider: Telnyx → Twilio` entry (newest on top):

```markdown
## 2026-07-17 — Live tool-dispatch + security test coverage

**Decision:** Add `tests/live_db/test_tool_dispatch_{reads,writes,security}.py`
— the first tests to exercise the *entire* real dispatch chain
(`execute_tool()` → `/voice/tools/{name}` route → `safety_layer`
authz/constraints → `queries.py`) against real Neon-shaped data, calling
`execute_tool()` directly (the exact function OpenAI's MCP path calls) with
no mocking.

**Why this gap existed:** `tests/voice_gateway/test_voice_tools_*.py` covers
the route handlers but mocks every DB call; `tests/live_db/*.py` covers the
query layer but calls it directly, below the authz layer. Nothing exercised
`safety_layer`'s authz/constraint logic against a real row or the real
schema — a regression there (e.g. `modify_booking` silently dropping its
phone check) would have passed every existing test.

**Two things discovered while writing these tests, worth knowing:**
- `modify_booking`/`cancel_booking` authorize off the **call row's stored
  `shop_id`** (read back via `get_call()`), not the `X-Shop-Id` header the
  MCP dispatch layer sends — cross-shop tests have to insert the call
  itself under the wrong shop, a header alone doesn't reach this check.
- `update_customer_from_call` (`booking_engine/api/routes/voice_tools_identity.py`)
  has **no shop-ownership check at all** — any valid call token can update
  any customer row's `email`/`tags` regardless of which shop the call
  belongs to. Not fixed here (out of scope for a test-coverage plan —
  changing production authz logic is a different, riskier kind of change);
  flagged as a fast-follow.

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
CI pipeline (separate, in-flight effort) needs to point `pytest tests/live_db/`
at a real Neon target for these tests to run in CI at all — until then, run
them locally against the QA branch via `TEST_DATABASE_URL`.
- Spec: `docs/superpowers/specs/2026-07-17-live-tool-dispatch-security-tests-design.md`.
  Plan: `docs/superpowers/plans/2026-07-17-live-tool-dispatch-security-tests.md`.
```

- [ ] **Step 2: Run the entire live_db suite one more time**

```bash
export PYTHONPATH=. && export TEST_DATABASE_URL=<QA branch connection string>
pytest tests/live_db/ -v
```
Expected: all tests pass (existing suite + the 22 new tests from Tasks 2-4).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record live tool-dispatch + security test coverage decision"
```

---

## Self-Review Notes

- **Spec coverage:** Architecture (Task 1's `tool_app` fixture matches the spec's rejected-alternatives reasoning — calls `execute_tool()`, not raw HTTP, not bare Python functions), Components (`test_tool_dispatch_reads.py`/`writes.py`/`security.py` map 1:1 to the spec's three named files and their per-tool test lists), Data Flow (every test mints a real token, calls `execute_tool()`, and — for writes — re-reads the row directly, matching the spec's step 4), Safety/Scope (no CI files touched, `voice_gateway` explicitly flagged not actioned, in both the spec and now `CLAUDE.md`), Testing section (Task 5 is exactly the spec's "deliberately break one thing, confirm the test fails" commitment) — all covered.
- **Placeholder scan:** no TBD/TODO; every step has literal code, exact commands, and expected output. The only bracketed placeholder is `<QA branch connection string>` in run commands, which is inherently environment-specific (a real secret), not a deferred design decision.
- **Type/name consistency:** `_token(shop_id, call_id, settings)` helper signature and usage match across all three test files. Fixture names (`tool_app`, `settings`, `cleanup_call_ids`, `cleanup_memo_ids`) match between Task 1's definitions and Tasks 2-4's usage. `SHOP_ID_2` is already defined in `tests/live_db/conftest.py` (verified by reading the file) — no new constant needed. Day offsets (45 for reads, 50/51/52 for writes, 60/61/62 for security) don't overlap with each other or with the existing suite's 0-33 day range.
