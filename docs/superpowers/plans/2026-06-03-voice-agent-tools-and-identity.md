# Voice Agent — Tools, Identity & Session Webhooks (Plan B of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the agent intelligence layer on top of Plan A's foundation: 12 HTTPS tool endpoints, OpenAI session lifecycle webhooks, the 3-layer system prompt assembly, identity resolution (matching callers to existing customers, creating new ones during the call), authorization with verification questions for destructive actions, and callback memo creation.

**Architecture:** All work lands in `voice-booking` repo, Booking Engine layer. Tools and webhooks are HTTPS endpoints called by OpenAI Realtime during a live call. Each tool returns a stable `{ok, data, error}` envelope. The system prompt is assembled at session.started time from three layers (safety rules, caller context, merchant personality, compliance, tools).

**Tech Stack:** Same as Plan A — Python 3.11, FastAPI, asyncpg, Pydantic v2, libphonenumber.

**Source spec:** `webapp/docs/superpowers/specs/2026-06-03-voice-agent-realtime-integration-design.md`
**Depends on:** Plan A (`2026-06-03-voice-agent-platform.md`) — schema, telephony rows, push client, token meter.

---

## File Structure

### New files

- `booking_engine/api/voice_tool_models.py` — Pydantic models shared across tool routes
- `booking_engine/api/routes/voice_tools_identity.py` — `lookup_customer`, `create_customer_from_call`, `update_customer_from_call`
- `booking_engine/api/routes/voice_tools_catalog.py` — `get_services`, `get_staff_for_service`
- `booking_engine/api/routes/voice_tools_booking.py` — `check_availability`, `create_booking`, `get_booking`, `modify_booking`, `cancel_booking`
- `booking_engine/api/routes/voice_tools_lifecycle.py` — `mark_outcome`, `escalate_to_merchant`
- `booking_engine/api/routes/voice_events.py` — `session.started`, `session.turn`, `session.ended` webhooks
- `booking_engine/services/identity_resolver.py` — phone match logic with edge-case handling
- `booking_engine/services/prompt_assembler.py` — 3-layer system prompt builder
- `booking_engine/services/safety_layer.py` — Layer 3 hardcoded rules and tool descriptions
- `booking_engine/db/voice_tool_queries.py` — DB access for tools (customers, services, appointments, memos)
- `booking_engine/db/voice_calls_queries.py` — DB access for `voice_agent.calls` + `call_turns` + `auth_events`
- `tests/voice_gateway/test_identity_resolver.py`
- `tests/voice_gateway/test_prompt_assembler.py`
- `tests/voice_gateway/test_voice_tools_identity.py`
- `tests/voice_gateway/test_voice_tools_catalog.py`
- `tests/voice_gateway/test_voice_tools_booking.py`
- `tests/voice_gateway/test_voice_tools_lifecycle.py`
- `tests/voice_gateway/test_voice_events.py`

### Modified files

- `booking_engine/app.py` — register the 5 new tool routers + events router
- `booking_engine/api/deps.py` — add `require_tool_token` (bearer auth for OpenAI → us)

---

### Task 1: Tool auth dependency + shared envelope models

**Files:**
- Modify: `booking_engine/api/deps.py`
- Create: `booking_engine/api/voice_tool_models.py`
- Modify: `booking_engine/config.py`

- [ ] **Step 1: Add OpenAI tool secret to Settings**

In `booking_engine/config.py`:

```python
openai_tool_secret: str = ""  # bearer token OpenAI sends with tool/event webhooks
```

- [ ] **Step 2: Add `require_tool_token` dependency**

Append to `booking_engine/api/deps.py`:

```python
async def require_tool_token(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> bool:
    """Bearer-auth dependency for tool + event webhooks (OpenAI → us)."""
    expected = settings.openai_tool_secret
    if not expected:
        raise HTTPException(500, "Tool secret not configured")
    header = request.headers.get("Authorization", "")
    if header != f"Bearer {expected}":
        raise HTTPException(401, "Invalid tool token")
    return True
```

- [ ] **Step 3: Create the shared envelope and tool model module**

Create `booking_engine/api/voice_tool_models.py`:

```python
"""Pydantic models shared across voice tool routes.

Every tool returns Envelope[T]; OpenAI sees ok/data/error and routes accordingly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field


T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None
    error: str | None = None


# Identity
class CustomerSummary(BaseModel):
    customer_id: UUID
    first_name: str
    last_name: str | None
    last_visit_at: datetime | None
    preferred_staff_id: UUID | None
    notes_tags: list[str] = Field(default_factory=list)
    verified: bool


class CreateCustomerIn(BaseModel):
    phone: str
    first_name: str
    last_name: str | None = None
    phone_source: Literal["caller_id", "stated"]


class CreatedCustomerOut(BaseModel):
    customer_id: UUID


class UpdateCustomerIn(BaseModel):
    customer_id: UUID
    field: Literal["last_name", "email", "notes_tags"]
    value: str


# Catalog
class ServiceOut(BaseModel):
    service_id: UUID
    name: str
    duration_min: int
    price_cents: int


class StaffOut(BaseModel):
    staff_id: UUID
    name: str


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


class ModifyBookingIn(BaseModel):
    appointment_id: UUID
    new_slot_start: datetime | None = None
    new_service_id: UUID | None = None
    verification_passed: bool


class CancelBookingIn(BaseModel):
    appointment_id: UUID
    verification_passed: bool


# Lifecycle
class MarkOutcomeIn(BaseModel):
    outcome: Literal[
        "booked", "rescheduled", "cancelled", "info",
        "abandoned", "escalated", "failed",
    ]
    summary: str
    callback_window: str | None = None


class EscalateIn(BaseModel):
    reason: str
    callback_window: str | None = None
    customer_message: str
```

- [ ] **Step 4: Commit**

```
git add booking_engine/api/deps.py booking_engine/api/voice_tool_models.py booking_engine/config.py
git commit -m "feat(voice): tool auth dep + shared envelope/model module"
```

---

### Task 2: Identity resolver service

**Files:**
- Create: `booking_engine/services/identity_resolver.py`
- Create: `tests/voice_gateway/test_identity_resolver.py`
- Create: `booking_engine/db/voice_tool_queries.py` (partial — customers section)

- [ ] **Step 1: Write failing tests for identity resolver**

Create `tests/voice_gateway/test_identity_resolver.py`:

```python
"""Tests for the identity resolver — handles all caller-vs-customer edge cases."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.identity_resolver import (
    resolve_caller,
    ResolutionResult,
)


@pytest.mark.asyncio
async def test_resolve_no_match_returns_empty():
    shop_id = uuid4()
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=[])):
        result = await resolve_caller(shop_id=shop_id, caller_phone="+393201234567")
    assert isinstance(result, ResolutionResult)
    assert result.matches == []
    assert result.unique_match is None


@pytest.mark.asyncio
async def test_resolve_single_match_returns_unique():
    shop_id = uuid4()
    cid = uuid4()
    fake = [{
        "id": cid, "first_name": "Maria", "last_name": "Rossi",
        "last_visit_at": None, "preferred_staff_id": None,
        "notes_tags": [], "verified": True,
    }]
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=fake)):
        result = await resolve_caller(shop_id=shop_id, caller_phone="+393201234567")
    assert len(result.matches) == 1
    assert result.unique_match is not None
    assert result.unique_match.customer_id == cid


@pytest.mark.asyncio
async def test_resolve_multiple_matches_marks_ambiguous():
    shop_id = uuid4()
    fake = [
        {"id": uuid4(), "first_name": "Maria", "last_name": "Rossi",
         "last_visit_at": None, "preferred_staff_id": None,
         "notes_tags": [], "verified": True},
        {"id": uuid4(), "first_name": "Giulia", "last_name": "Rossi",
         "last_visit_at": None, "preferred_staff_id": None,
         "notes_tags": [], "verified": True},
    ]
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=fake)):
        result = await resolve_caller(shop_id=shop_id, caller_phone="+393201234567")
    assert len(result.matches) == 2
    assert result.unique_match is None  # ambiguous


@pytest.mark.asyncio
async def test_resolve_anonymous_caller_returns_anonymous():
    shop_id = uuid4()
    result = await resolve_caller(shop_id=shop_id, caller_phone=None)
    assert result.is_anonymous is True
    assert result.matches == []


@pytest.mark.asyncio
async def test_resolve_normalizes_input_phone():
    shop_id = uuid4()
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=[])) as q:
        await resolve_caller(shop_id=shop_id, caller_phone="320-1234567")
    q.assert_awaited_once()
    # Second positional/kwarg should be normalized digits
    args, kwargs = q.await_args
    assert "3201234567" in str(args) or "3201234567" in str(kwargs)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_identity_resolver.py -v
```

- [ ] **Step 3: Implement DB query for customer phone lookup**

Create `booking_engine/db/voice_tool_queries.py`:

```python
"""DB access for voice agent tools — customers, services, appointments."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db import connection


async def find_customers_by_phone(*, shop_id: UUID, phone_digits: str) -> list[dict]:
    """Find customers whose phone normalizes to the same digits."""
    if not phone_digits:
        return []
    return await connection.execute(
        """
        SELECT id, first_name, last_name, last_visit_at,
               preferred_staff_id, notes_tags, verified
        FROM business_app_core.customers
        WHERE shop_id = $1 AND phone_normalized = $2
        LIMIT 5
        """,
        shop_id, phone_digits,
    )


async def insert_customer_from_call(
    *,
    shop_id: UUID,
    phone: str,
    first_name: str,
    last_name: str | None,
    phone_verified: bool,
    created_by_call_id: UUID,
) -> UUID:
    row = await connection.execute_one(
        """
        INSERT INTO business_app_core.customers
            (shop_id, phone, first_name, last_name,
             source, created_by_call_id, verified, phone_verified,
             notes_tags, created_at)
        VALUES
            ($1, $2, $3, $4,
             'voice_agent', $5, false, $6,
             ARRAY['nuovo da chiamata vocale']::text[], now())
        RETURNING id
        """,
        shop_id, phone, first_name, last_name,
        created_by_call_id, phone_verified,
    )
    return row["id"]


async def update_customer_field(
    *, customer_id: UUID, field: str, value: str
) -> bool:
    allowed = {"last_name", "email", "notes_tags"}
    if field not in allowed:
        return False
    if field == "notes_tags":
        # append a tag rather than replace
        sql = (
            "UPDATE business_app_core.customers "
            "SET notes_tags = array_append(coalesce(notes_tags, ARRAY[]::text[]), $2), "
            "    updated_at = now() "
            "WHERE id = $1"
        )
    else:
        sql = (
            f"UPDATE business_app_core.customers "
            f"SET {field} = $2, updated_at = now() WHERE id = $1"
        )
    await connection.execute_void(sql, customer_id, value)
    return True
```

- [ ] **Step 4: Implement identity resolver**

Create `booking_engine/services/identity_resolver.py`:

```python
"""Caller identity resolution from phone number.

Handles all Phase-1 cases from the spec: clean match, multiple matches,
no match, anonymous CLI. Caller wrap the DB result in a typed structure
that downstream code (prompt assembler, tools) consumes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from booking_engine.api.voice_tool_models import CustomerSummary
from booking_engine.db.voice_tool_queries import find_customers_by_phone
from booking_engine.services.phone_normalize import digits_only


@dataclass
class ResolutionResult:
    is_anonymous: bool = False
    caller_phone_e164: str | None = None
    matches: list[CustomerSummary] = field(default_factory=list)

    @property
    def unique_match(self) -> CustomerSummary | None:
        return self.matches[0] if len(self.matches) == 1 else None


def _row_to_summary(row: dict) -> CustomerSummary:
    return CustomerSummary(
        customer_id=row["id"],
        first_name=row["first_name"] or "",
        last_name=row.get("last_name"),
        last_visit_at=row.get("last_visit_at"),
        preferred_staff_id=row.get("preferred_staff_id"),
        notes_tags=row.get("notes_tags") or [],
        verified=row.get("verified", True),
    )


async def resolve_caller(
    *, shop_id: UUID, caller_phone: str | None
) -> ResolutionResult:
    if not caller_phone:
        return ResolutionResult(is_anonymous=True)
    phone_digits = digits_only(caller_phone)
    rows = await find_customers_by_phone(shop_id=shop_id, phone_digits=phone_digits)
    return ResolutionResult(
        is_anonymous=False,
        caller_phone_e164=caller_phone,
        matches=[_row_to_summary(r) for r in rows],
    )
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/voice_gateway/test_identity_resolver.py -v
```

Expected: 5 passing.

- [ ] **Step 6: Commit**

```
git add booking_engine/services/identity_resolver.py booking_engine/db/voice_tool_queries.py tests/voice_gateway/test_identity_resolver.py
git commit -m "feat(voice): identity resolver with phone normalization and edge-case handling"
```

---

### Task 3: Layer 3 safety + tool descriptions module

**Files:**
- Create: `booking_engine/services/safety_layer.py`
- Create: `tests/voice_gateway/test_safety_layer.py`

- [ ] **Step 1: Write failing test**

Create `tests/voice_gateway/test_safety_layer.py`:

```python
"""Tests for the Layer 3 safety prompt and tool descriptions."""
from booking_engine.services.safety_layer import (
    SAFETY_PROMPT,
    DEFAULT_TOOL_ALLOWLIST,
    tool_descriptions,
)


def test_safety_prompt_mentions_key_rules():
    text = SAFETY_PROMPT.lower()
    assert "medic" in text  # no medical advice
    assert "prezz" in text or "pric" in text  # no price negotiation
    assert "umano" in text or "salone" in text  # escalation


def test_default_allowlist_contains_12_tools():
    assert len(DEFAULT_TOOL_ALLOWLIST) == 12
    assert "create_booking" in DEFAULT_TOOL_ALLOWLIST
    assert "escalate_to_merchant" in DEFAULT_TOOL_ALLOWLIST


def test_tool_descriptions_filtered_by_allowlist():
    descs = tool_descriptions(allowlist=["lookup_customer", "create_booking"])
    names = {d["name"] for d in descs}
    assert names == {"lookup_customer", "create_booking"}
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_safety_layer.py -v
```

- [ ] **Step 3: Implement safety layer**

Create `booking_engine/services/safety_layer.py`:

```python
"""Layer 3 — hardcoded safety rules and tool descriptions.

Merchants cannot view or modify these. They are prepended to every session prompt.
The tool descriptions are JSON schemas OpenAI uses to advertise available tools.
"""
from __future__ import annotations

from typing import Any


SAFETY_PROMPT = """\
REGOLE NON NEGOZIABILI (in italiano):
- Non dare mai consigli medici, diagnostici o farmaceutici. Se il chiamante \
chiede, indirizzalo al medico o al farmacista.
- Non trattare prezzi al di fuori di quelli forniti dagli strumenti. Non \
contrattare sconti non già configurati.
- Non promettere risultati estetici specifici ("ti farò sembrare 10 anni più giovane").
- Se il chiamante chiede di parlare con una persona, usa lo strumento \
escalate_to_merchant e termina educatamente la chiamata.
- Se il chiamante è aggressivo, ripetutamente offensivo o usa linguaggio \
inappropriato, termina cordialmente la chiamata.
- Conferma sempre i dettagli di una prenotazione a voce prima di chiamare lo \
strumento create_booking.
- Prima di modificare o cancellare una prenotazione esistente, devi:
  1. Confermare l'identità del chiamante con UNA domanda verifica (es. orario \
     della prenotazione, servizio prenotato, nome completo).
  2. Passare verification_passed=true SOLO se la risposta è corretta.
- Parla sempre in italiano salvo richiesta esplicita del chiamante.
- Mantieni le risposte concise. Una o due frasi per turno.
"""


DEFAULT_TOOL_ALLOWLIST = [
    "lookup_customer",
    "create_customer_from_call",
    "update_customer_from_call",
    "get_services",
    "get_staff_for_service",
    "check_availability",
    "create_booking",
    "get_booking",
    "modify_booking",
    "cancel_booking",
    "mark_outcome",
    "escalate_to_merchant",
]


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "lookup_customer": {
        "name": "lookup_customer",
        "description": "Trova clienti per numero di telefono normalizzato. Restituisce 0-5 risultati.",
        "parameters": {
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"],
        },
    },
    "create_customer_from_call": {
        "name": "create_customer_from_call",
        "description": "Crea un nuovo cliente dopo aver raccolto nome e telefono.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "phone_source": {"type": "string", "enum": ["caller_id", "stated"]},
            },
            "required": ["phone", "first_name", "phone_source"],
        },
    },
    "update_customer_from_call": {
        "name": "update_customer_from_call",
        "description": "Aggiorna un campo del cliente (last_name, email, notes_tags).",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "field": {"type": "string", "enum": ["last_name", "email", "notes_tags"]},
                "value": {"type": "string"},
            },
            "required": ["customer_id", "field", "value"],
        },
    },
    "get_services": {
        "name": "get_services",
        "description": "Lista dei servizi del salone, opzionalmente filtrati per nome.",
        "parameters": {
            "type": "object",
            "properties": {"filter": {"type": "string"}},
        },
    },
    "get_staff_for_service": {
        "name": "get_staff_for_service",
        "description": "Personale qualificato per un servizio.",
        "parameters": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
        },
    },
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
    "get_booking": {
        "name": "get_booking",
        "description": "Recupera la prossima prenotazione di un cliente, opzionalmente filtrando per data approssimativa.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "fuzzy_when": {"type": "string"},
            },
            "required": ["customer_id"],
        },
    },
    "modify_booking": {
        "name": "modify_booking",
        "description": "Modifica una prenotazione. Richiede verification_passed=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "new_slot_start": {"type": "string"},
                "new_service_id": {"type": "string"},
                "verification_passed": {"type": "boolean"},
            },
            "required": ["appointment_id", "verification_passed"],
        },
    },
    "cancel_booking": {
        "name": "cancel_booking",
        "description": "Cancella una prenotazione. Richiede verification_passed=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "verification_passed": {"type": "boolean"},
            },
            "required": ["appointment_id", "verification_passed"],
        },
    },
    "mark_outcome": {
        "name": "mark_outcome",
        "description": "Marca l'esito della chiamata prima di chiudere.",
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": [
                    "booked", "rescheduled", "cancelled", "info",
                    "abandoned", "escalated", "failed",
                ]},
                "summary": {"type": "string"},
                "callback_window": {"type": "string"},
            },
            "required": ["outcome", "summary"],
        },
    },
    "escalate_to_merchant": {
        "name": "escalate_to_merchant",
        "description": "Crea un memo per il salone che richiama il cliente.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "callback_window": {"type": "string"},
                "customer_message": {"type": "string"},
            },
            "required": ["reason", "customer_message"],
        },
    },
}


def tool_descriptions(*, allowlist: list[str]) -> list[dict[str, Any]]:
    return [_TOOL_SCHEMAS[name] for name in allowlist if name in _TOOL_SCHEMAS]
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/voice_gateway/test_safety_layer.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Commit**

```
git add booking_engine/services/safety_layer.py tests/voice_gateway/test_safety_layer.py
git commit -m "feat(voice): Layer 3 safety prompt + 12 tool descriptions"
```

---

### Task 4: Prompt assembler (3-layer composition)

**Files:**
- Create: `booking_engine/services/prompt_assembler.py`
- Create: `tests/voice_gateway/test_prompt_assembler.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_prompt_assembler.py`:

```python
"""Tests for the 3-layer system-prompt assembler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from booking_engine.api.voice_tool_models import CustomerSummary
from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.prompt_assembler import assemble_session_prompt


def _config(**overrides):
    base = {
        "display_name": "Salone Lucia",
        "greeting_after_disclosure": "Sono Aria, come posso aiutarla?",
        "tone_preset": "warm",
        "voice_preset": "warm_female",
        "answer_mode": "overflow",
        "services_to_mention": [],
    }
    base.update(overrides)
    return base


def _policy():
    return {
        "disclosure_text": "Salve, assistente AI...",
        "recording_consent_prompt": "Posso aiutarla?",
    }


def test_assemble_includes_safety_rules():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "REGOLE NON NEGOZIABILI" in out.prompt


def test_assemble_includes_disclosure():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "assistente AI" in out.prompt


def test_assemble_personalizes_for_known_caller():
    cid = uuid4()
    match = CustomerSummary(
        customer_id=cid, first_name="Maria", last_name="Rossi",
        last_visit_at=datetime.now(timezone.utc) - timedelta(days=14),
        preferred_staff_id=None, notes_tags=["preferisce shampoo idratante"],
        verified=True,
    )
    resolution = ResolutionResult(is_anonymous=False, matches=[match])
    out = assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "Maria" in out.prompt
    assert "Saluta" in out.prompt or "saluta" in out.prompt


def test_assemble_anonymous_uses_neutral_greeting():
    resolution = ResolutionResult(is_anonymous=True, matches=[])
    out = assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "anonimo" in out.prompt.lower() or "non ho il suo numero" in out.prompt.lower()


def test_assemble_returns_tool_descriptions():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    names = {t["name"] for t in out.tools}
    assert "lookup_customer" in names
    assert "create_booking" in names
    assert len(out.tools) == 12


def test_assemble_returns_voice_preset():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = assemble_session_prompt(
        config=_config(voice_preset="neutral_male"), policy=_policy(),
        resolution=resolution,
    )
    assert out.voice == "neutral_male"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_prompt_assembler.py -v
```

- [ ] **Step 3: Implement the assembler**

Create `booking_engine/services/prompt_assembler.py`:

```python
"""3-layer system-prompt assembler.

Composes the session prompt sent to OpenAI on session.started:
  Layer 3 (safety, immutable) → caller context → Layer 1 (personality)
  → Layer 2 (disclosure) → tool descriptions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.safety_layer import (
    DEFAULT_TOOL_ALLOWLIST,
    SAFETY_PROMPT,
    tool_descriptions,
)


@dataclass
class AssembledPrompt:
    prompt: str
    tools: list[dict[str, Any]]
    voice: str


_TONE_INJECT = {
    "warm": "Tono caloroso, accogliente, mai distante.",
    "professional": "Tono professionale e diretto, senza fronzoli.",
    "casual": "Tono informale e amichevole, come parlare a un amico.",
}


def _caller_context(resolution: ResolutionResult) -> str:
    if resolution.is_anonymous:
        return (
            "Il chiamante ha il numero anonimo. Per prenotare avrai bisogno del "
            "nome e di un numero di telefono pronunciato dal chiamante. "
            "Saluta in modo neutro."
        )
    if resolution.unique_match:
        m = resolution.unique_match
        parts = [f"Il cliente è {m.first_name}" + (f" {m.last_name}" if m.last_name else "") + "."]
        if m.last_visit_at:
            days = (datetime.now(timezone.utc) - m.last_visit_at).days
            parts.append(f"Ultima visita: {days} giorni fa.")
        if m.notes_tags:
            parts.append(f"Note: {', '.join(m.notes_tags)}.")
        parts.append("Saluta per nome e chiedi come puoi aiutarla.")
        return " ".join(parts)
    if len(resolution.matches) > 1:
        names = ", ".join(
            f"{m.first_name} {m.last_name or ''}".strip() for m in resolution.matches
        )
        return (
            f"Il numero è collegato a più clienti: {names}. "
            "Chiedi al chiamante per chi è la prenotazione prima di procedere."
        )
    return (
        "Il chiamante non è ancora un cliente del salone. "
        "Saluta in modo neutro, chiedi il nome e con cosa puoi aiutarlo. "
        "Crea il record cliente solo quando hai un nome confermato."
    )


def assemble_session_prompt(
    *,
    config: dict[str, Any],
    policy: dict[str, Any],
    resolution: ResolutionResult,
    allowlist: list[str] | None = None,
) -> AssembledPrompt:
    allowlist = allowlist or DEFAULT_TOOL_ALLOWLIST
    tone_text = _TONE_INJECT.get(config.get("tone_preset", "warm"), _TONE_INJECT["warm"])

    parts = [
        SAFETY_PROMPT,
        "",
        "CONTESTO CHIAMANTE:",
        _caller_context(resolution),
        "",
        f"SEI L'ASSISTENTE DI: {config.get('display_name', '')}.",
        f"FRASE DI BENVENUTO: \"{config.get('greeting_after_disclosure', '')}\"",
        tone_text,
        "",
        "DISCLOSURE OBBLIGATORIA (dilla all'inizio della conversazione):",
        policy.get("disclosure_text", ""),
    ]

    return AssembledPrompt(
        prompt="\n".join(parts),
        tools=tool_descriptions(allowlist=allowlist),
        voice=config.get("voice_preset", "warm_female"),
    )
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/voice_gateway/test_prompt_assembler.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Commit**

```
git add booking_engine/services/prompt_assembler.py tests/voice_gateway/test_prompt_assembler.py
git commit -m "feat(voice): 3-layer system-prompt assembler"
```

---

### Task 5: Identity tool routes (`lookup_customer`, `create_customer_from_call`, `update_customer_from_call`)

**Files:**
- Create: `booking_engine/api/routes/voice_tools_identity.py`
- Create: `tests/voice_gateway/test_voice_tools_identity.py`
- Modify: `booking_engine/app.py`
- Modify: `booking_engine/db/voice_tool_queries.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_voice_tools_identity.py`:

```python
"""Tests for /voice/tools/* identity endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(uuid4()),
        "X-Call-Id": str(uuid4())}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_lookup_customer_returns_matches():
    cid = uuid4()
    fake = [{"id": cid, "first_name": "Maria", "last_name": "Rossi",
             "last_visit_at": None, "preferred_staff_id": None,
             "notes_tags": [], "verified": True}]
    with patch("booking_engine.api.routes.voice_tools_identity.find_customers_by_phone",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/lookup_customer",
                headers=AUTH, json={"phone": "+393201234567"},
            )
    body = r.json()
    assert body["ok"] is True
    assert len(body["data"]) == 1
    assert body["data"][0]["customer_id"] == str(cid)


@pytest.mark.asyncio
async def test_create_customer_writes_row():
    new_id = uuid4()
    with patch("booking_engine.api.routes.voice_tools_identity.insert_customer_from_call",
               new=AsyncMock(return_value=new_id)), \
         patch("booking_engine.api.routes.voice_tools_identity.attach_customer_to_call",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_customer_from_call",
                headers=AUTH,
                json={"phone": "+393201234567", "first_name": "Marco",
                      "last_name": "Bianchi", "phone_source": "caller_id"},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["customer_id"] == str(new_id)


@pytest.mark.asyncio
async def test_update_customer_rejects_unknown_field():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/voice/tools/update_customer_from_call",
            headers=AUTH,
            json={"customer_id": str(uuid4()), "field": "phone", "value": "X"},
        )
    assert r.status_code == 422  # Pydantic Literal rejects
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_tools_identity.py -v
```

- [ ] **Step 3: Add `attach_customer_to_call` query**

Append to `booking_engine/db/voice_tool_queries.py`:

```python
async def attach_customer_to_call(
    *, call_id: UUID, created_customer_id: UUID | None = None,
    matched_customer_id: UUID | None = None,
) -> None:
    sets = []
    args: list = [call_id]
    if created_customer_id is not None:
        args.append(created_customer_id)
        sets.append(f"created_customer_id = ${len(args)}")
    if matched_customer_id is not None:
        args.append(matched_customer_id)
        sets.append(f"matched_customer_id = ${len(args)}")
    if not sets:
        return
    sql = f"UPDATE voice_agent.calls SET {', '.join(sets)} WHERE id = $1"
    await connection.execute_void(sql, *args)
```

- [ ] **Step 4: Implement the routes**

Create `booking_engine/api/routes/voice_tools_identity.py`:

```python
"""Voice tool endpoints — identity (lookup, create, update)."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    CreateCustomerIn, CreatedCustomerOut, CustomerSummary, Envelope,
    UpdateCustomerIn,
)
from booking_engine.db.voice_tool_queries import (
    attach_customer_to_call, find_customers_by_phone,
    insert_customer_from_call, update_customer_field,
)
from booking_engine.services.phone_normalize import digits_only

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-identity"])


@router.post("/lookup_customer")
async def lookup_customer(
    body: dict,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[CustomerSummary]]:
    phone = body.get("phone", "")
    rows = await find_customers_by_phone(
        shop_id=x_shop_id, phone_digits=digits_only(phone),
    )
    summaries = [CustomerSummary(
        customer_id=r["id"], first_name=r["first_name"] or "",
        last_name=r.get("last_name"), last_visit_at=r.get("last_visit_at"),
        preferred_staff_id=r.get("preferred_staff_id"),
        notes_tags=r.get("notes_tags") or [], verified=r.get("verified", True),
    ) for r in rows]
    return Envelope[list[CustomerSummary]](ok=True, data=summaries)


@router.post("/create_customer_from_call")
async def create_customer(
    body: CreateCustomerIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[CreatedCustomerOut]:
    new_id = await insert_customer_from_call(
        shop_id=x_shop_id, phone=body.phone, first_name=body.first_name,
        last_name=body.last_name,
        phone_verified=(body.phone_source == "caller_id"),
        created_by_call_id=x_call_id,
    )
    await attach_customer_to_call(call_id=x_call_id, created_customer_id=new_id)
    return Envelope[CreatedCustomerOut](
        ok=True, data=CreatedCustomerOut(customer_id=new_id),
    )


@router.post("/update_customer_from_call")
async def update_customer(
    body: UpdateCustomerIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
) -> Envelope[dict]:
    ok = await update_customer_field(
        customer_id=body.customer_id, field=body.field, value=body.value,
    )
    return Envelope[dict](ok=ok, data={"updated": ok})
```

- [ ] **Step 5: Register the router**

In `booking_engine/app.py`:

```python
from booking_engine.api.routes import voice_tools_identity
app.include_router(voice_tools_identity.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_tools_identity.py -v
```

Expected: 3 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_tools_identity.py booking_engine/db/voice_tool_queries.py booking_engine/app.py tests/voice_gateway/test_voice_tools_identity.py
git commit -m "feat(voice): identity tools — lookup, create, update customer"
```

---

### Task 6: Catalog tool routes (`get_services`, `get_staff_for_service`)

**Files:**
- Create: `booking_engine/api/routes/voice_tools_catalog.py`
- Create: `tests/voice_gateway/test_voice_tools_catalog.py`
- Modify: `booking_engine/db/voice_tool_queries.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_voice_tools_catalog.py`:

```python
"""Tests for catalog tool endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(uuid4()),
        "X-Call-Id": str(uuid4())}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_get_services_returns_filtered():
    sid = uuid4()
    fake = [{"id": sid, "name": "Taglio donna", "duration_min": 30,
             "price_cents": 2500}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/tools/get_services",
                             headers=AUTH, json={"filter": "taglio"})
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["service_id"] == str(sid)


@pytest.mark.asyncio
async def test_get_staff_for_service_returns_staff():
    staff_id = uuid4()
    fake = [{"id": staff_id, "name": "Giulia"}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_staff_for_service",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/get_staff_for_service",
                headers=AUTH, json={"service_id": str(uuid4())},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["name"] == "Giulia"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_tools_catalog.py -v
```

- [ ] **Step 3: Add catalog queries**

Append to `booking_engine/db/voice_tool_queries.py`:

```python
async def list_services(*, shop_id: UUID, filter_q: str | None) -> list[dict]:
    if filter_q:
        return await connection.execute(
            """
            SELECT id, name, duration_min, price_cents
            FROM business_app_core.services
            WHERE shop_id = $1 AND active = true
              AND name ILIKE '%' || $2 || '%'
            ORDER BY name
            LIMIT 20
            """,
            shop_id, filter_q,
        )
    return await connection.execute(
        """
        SELECT id, name, duration_min, price_cents
        FROM business_app_core.services
        WHERE shop_id = $1 AND active = true
        ORDER BY name
        """,
        shop_id,
    )


async def list_staff_for_service(*, shop_id: UUID, service_id: UUID) -> list[dict]:
    return await connection.execute(
        """
        SELECT s.id, (s.first_name || ' ' || coalesce(s.last_name,'')) AS name
        FROM business_app_core.staff_users s
        JOIN business_app_core.staff_services ss ON ss.staff_id = s.id
        WHERE s.shop_id = $1 AND ss.service_id = $2 AND s.active = true
        ORDER BY s.first_name
        """,
        shop_id, service_id,
    )
```

- [ ] **Step 4: Implement routes**

Create `booking_engine/api/routes/voice_tools_catalog.py`:

```python
"""Voice tool endpoints — catalog (services, staff)."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import Envelope, ServiceOut, StaffOut
from booking_engine.db.voice_tool_queries import (
    list_services, list_staff_for_service,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-catalog"])


@router.post("/get_services")
async def get_services(
    body: dict,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[ServiceOut]]:
    rows = await list_services(shop_id=x_shop_id, filter_q=body.get("filter"))
    out = [ServiceOut(service_id=r["id"], name=r["name"],
                      duration_min=r["duration_min"], price_cents=r["price_cents"])
           for r in rows]
    return Envelope[list[ServiceOut]](ok=True, data=out)


@router.post("/get_staff_for_service")
async def get_staff_for_service(
    body: dict,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[StaffOut]]:
    service_id = UUID(body["service_id"])
    rows = await list_staff_for_service(shop_id=x_shop_id, service_id=service_id)
    out = [StaffOut(staff_id=r["id"], name=r["name"]) for r in rows]
    return Envelope[list[StaffOut]](ok=True, data=out)
```

- [ ] **Step 5: Register router**

```python
from booking_engine.api.routes import voice_tools_catalog
app.include_router(voice_tools_catalog.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_tools_catalog.py -v
```

Expected: 2 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_tools_catalog.py booking_engine/db/voice_tool_queries.py booking_engine/app.py tests/voice_gateway/test_voice_tools_catalog.py
git commit -m "feat(voice): catalog tools — get_services and get_staff_for_service"
```

---

### Task 7: Booking tool routes — `check_availability` and `create_booking` with advisory lock

**Files:**
- Modify: `booking_engine/api/routes/voice_tools_booking.py` (create)
- Create: `tests/voice_gateway/test_voice_tools_booking.py`
- Modify: `booking_engine/db/voice_tool_queries.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests for check_availability + create_booking**

Create `tests/voice_gateway/test_voice_tools_booking.py`:

```python
"""Tests for booking tool endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(uuid4()),
        "X-Call-Id": str(uuid4())}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_check_availability_returns_slots():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    slot_start = now + timedelta(days=1, hours=10)
    slot_end = slot_start + timedelta(minutes=30)
    fake = [{"slot_start": slot_start, "slot_end": slot_end,
             "staff_id": uuid4(), "staff_name": "Giulia"}]
    with patch("booking_engine.api.routes.voice_tools_booking.find_availability",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/check_availability",
                headers=AUTH,
                json={"service_id": str(uuid4()), "max_results": 5},
            )
    body = r.json()
    assert body["ok"] is True
    assert len(body["data"]) == 1


@pytest.mark.asyncio
async def test_create_booking_inserts_and_attaches_to_call():
    appt_id = uuid4()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=AsyncMock(return_value={
                   "id": appt_id, "slot_start": now,
                   "slot_end": now + timedelta(minutes=30),
                   "staff_id": uuid4(),
                   "confirmation_status": "confirmed",
               })), \
         patch("booking_engine.api.routes.voice_tools_booking.attach_booking_to_call",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()), "service_id": str(uuid4()),
                      "slot_start": now.isoformat(), "staff_id": str(uuid4())},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["appointment_id"] == str(appt_id)


@pytest.mark.asyncio
async def test_create_booking_slot_taken_returns_error():
    with patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=AsyncMock(side_effect=RuntimeError("slot_taken"))):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()), "service_id": str(uuid4()),
                      "slot_start": datetime.now(timezone.utc).isoformat(),
                      "staff_id": str(uuid4())},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "slot_taken"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_tools_booking.py -v
```

- [ ] **Step 3: Add booking queries with advisory lock**

Append to `booking_engine/db/voice_tool_queries.py`:

```python
from datetime import datetime, timedelta


async def find_availability(
    *, shop_id: UUID, service_id: UUID,
    preferred_when: datetime | None,
    staff_id: UUID | None,
    max_results: int,
) -> list[dict]:
    """Return open slots for the service. Naive search — refines later."""
    horizon_start = preferred_when or datetime.utcnow()
    horizon_end = horizon_start + timedelta(days=14)
    return await connection.execute(
        """
        WITH service AS (
            SELECT duration_min FROM business_app_core.services WHERE id = $2
        ),
        candidates AS (
            SELECT sched.staff_id,
                   (sched.day_date + sched.start_time)::timestamptz AS slot_start,
                   (sched.day_date + sched.start_time + ((SELECT duration_min FROM service) || ' minutes')::interval)::timestamptz AS slot_end
            FROM business_app_core.staff_schedules sched
            JOIN business_app_core.staff_services ss
              ON ss.staff_id = sched.staff_id AND ss.service_id = $2
            WHERE sched.shop_id = $1
              AND sched.day_date BETWEEN $3::date AND $4::date
              AND ($5::uuid IS NULL OR sched.staff_id = $5)
        ),
        not_booked AS (
            SELECT c.*, (SELECT first_name || ' ' || coalesce(last_name,'')
                         FROM business_app_core.staff_users WHERE id = c.staff_id) AS staff_name
            FROM candidates c
            WHERE NOT EXISTS (
                SELECT 1 FROM business_app_core.appointments a
                WHERE a.shop_id = $1 AND a.staff_id = c.staff_id
                  AND tstzrange(a.start_at, a.end_at) && tstzrange(c.slot_start, c.slot_end)
                  AND a.status NOT IN ('cancelled')
            )
        )
        SELECT * FROM not_booked ORDER BY slot_start LIMIT $6
        """,
        shop_id, service_id, horizon_start, horizon_end, staff_id, max_results,
    )


async def insert_booking_locked(
    *, shop_id: UUID, customer_id: UUID, service_id: UUID,
    slot_start: datetime, staff_id: UUID,
) -> dict:
    """Insert booking with advisory lock to prevent race conditions. Raises on conflict."""
    pool = connection._get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock per (staff_id, 15-min bucket)
            bucket = int(slot_start.timestamp() // 900)
            lock_key = f"{staff_id}|{bucket}"
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", lock_key,
            )
            # Verify still open
            taken = await conn.fetchval(
                """
                SELECT EXISTS (
                  SELECT 1 FROM business_app_core.appointments
                  WHERE shop_id = $1 AND staff_id = $2
                    AND start_at = $3 AND status NOT IN ('cancelled')
                )
                """,
                shop_id, staff_id, slot_start,
            )
            if taken:
                raise RuntimeError("slot_taken")
            row = await conn.fetchrow(
                """
                INSERT INTO business_app_core.appointments
                    (shop_id, customer_id, staff_id, start_at, end_at,
                     status, source, confirmation_status)
                SELECT $1, $2, $3, $4,
                       $4 + (sv.duration_min || ' minutes')::interval,
                       'confirmed', 'voice_agent', 'confirmed'
                FROM business_app_core.services sv
                WHERE sv.id = $5
                RETURNING id, start_at AS slot_start, end_at AS slot_end,
                          staff_id, confirmation_status
                """,
                shop_id, customer_id, staff_id, slot_start, service_id,
            )
            # Link appointment_services junction
            await conn.execute(
                """
                INSERT INTO business_app_core.appointment_services
                    (appointment_id, service_id)
                VALUES ($1, $2)
                """,
                row["id"], service_id,
            )
            return dict(row)


async def attach_booking_to_call(*, call_id: UUID, appointment_id: UUID) -> None:
    await connection.execute_void(
        "UPDATE voice_agent.calls SET created_booking_id = $2 WHERE id = $1",
        call_id, appointment_id,
    )
    await connection.execute_void(
        "UPDATE business_app_core.appointments SET voice_call_id = $1 WHERE id = $2",
        call_id, appointment_id,
    )
```

- [ ] **Step 4: Implement routes**

Create `booking_engine/api/routes/voice_tools_booking.py`:

```python
"""Voice tool endpoints — availability + booking write/modify/cancel."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    AvailabilitySlot, BookingOut, CancelBookingIn, CreateBookingIn, Envelope,
    ModifyBookingIn,
)
from booking_engine.db.voice_tool_queries import (
    attach_booking_to_call, find_availability, insert_booking_locked,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-booking"])


class CheckAvailabilityIn(BaseModel):
    service_id: UUID
    preferred_when: datetime | None = None
    staff_id: UUID | None = None
    max_results: int = 5


@router.post("/check_availability")
async def check_availability(
    body: CheckAvailabilityIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[AvailabilitySlot]]:
    rows = await find_availability(
        shop_id=x_shop_id, service_id=body.service_id,
        preferred_when=body.preferred_when, staff_id=body.staff_id,
        max_results=body.max_results,
    )
    out = [AvailabilitySlot(**r) for r in rows]
    return Envelope[list[AvailabilitySlot]](ok=True, data=out)


@router.post("/create_booking")
async def create_booking(
    body: CreateBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[BookingOut]:
    try:
        row = await insert_booking_locked(
            shop_id=x_shop_id, customer_id=body.customer_id,
            service_id=body.service_id, slot_start=body.slot_start,
            staff_id=body.staff_id,
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
            staff_id=row["staff_id"],
        ),
    )
```

- [ ] **Step 5: Register router**

```python
from booking_engine.api.routes import voice_tools_booking
app.include_router(voice_tools_booking.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_tools_booking.py -v
```

Expected: 3 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_tools_booking.py booking_engine/db/voice_tool_queries.py booking_engine/app.py tests/voice_gateway/test_voice_tools_booking.py
git commit -m "feat(voice): check_availability and create_booking with advisory lock"
```

---

### Task 8: Booking tools — `get_booking`, `modify_booking`, `cancel_booking` with auth audit

**Files:**
- Modify: `booking_engine/api/routes/voice_tools_booking.py`
- Modify: `booking_engine/db/voice_tool_queries.py`
- Append: `tests/voice_gateway/test_voice_tools_booking.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/voice_gateway/test_voice_tools_booking.py`:

```python
@pytest.mark.asyncio
async def test_modify_booking_requires_verification():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/voice/tools/modify_booking",
            headers=AUTH,
            json={"appointment_id": str(uuid4()),
                  "verification_passed": False,
                  "new_slot_start": datetime.now(timezone.utc).isoformat()},
        )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_cancel_booking_writes_audit_event():
    appt_id = uuid4()
    with patch("booking_engine.api.routes.voice_tools_booking.cancel_appointment",
               new=AsyncMock(return_value=True)), \
         patch("booking_engine.api.routes.voice_tools_booking.log_auth_event",
               new=AsyncMock(return_value=None)) as audit:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/cancel_booking",
                headers=AUTH,
                json={"appointment_id": str(appt_id),
                      "verification_passed": True},
            )
    body = r.json()
    assert body["ok"] is True
    audit.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_tools_booking.py -v
```

- [ ] **Step 3: Add queries**

Append to `booking_engine/db/voice_tool_queries.py`:

```python
async def get_next_booking_for_customer(
    *, shop_id: UUID, customer_id: UUID,
) -> dict | None:
    return await connection.execute_one(
        """
        SELECT a.id, a.start_at, a.end_at, a.staff_id, a.status,
               (SELECT name FROM business_app_core.services s
                JOIN business_app_core.appointment_services aps ON aps.service_id = s.id
                WHERE aps.appointment_id = a.id LIMIT 1) AS service_name
        FROM business_app_core.appointments a
        WHERE a.shop_id = $1 AND a.customer_id = $2
          AND a.start_at > now() AND a.status NOT IN ('cancelled')
        ORDER BY a.start_at
        LIMIT 1
        """,
        shop_id, customer_id,
    )


async def modify_appointment(
    *, appointment_id: UUID, new_slot_start: datetime | None,
    new_service_id: UUID | None,
) -> bool:
    sets = []
    args: list = [appointment_id]
    if new_slot_start is not None:
        args.append(new_slot_start)
        sets.append(f"start_at = ${len(args)}")
        # end_at follows new_slot_start + existing service duration
        sets.append(
            f"end_at = ${len(args)} + (end_at - start_at)"
        )
    if not sets:
        return False
    sql = (
        f"UPDATE business_app_core.appointments "
        f"SET {', '.join(sets)}, updated_at = now() WHERE id = $1"
    )
    await connection.execute_void(sql, *args)
    if new_service_id is not None:
        await connection.execute_void(
            """
            UPDATE business_app_core.appointment_services
            SET service_id = $2 WHERE appointment_id = $1
            """,
            appointment_id, new_service_id,
        )
    return True


async def cancel_appointment(*, appointment_id: UUID) -> bool:
    await connection.execute_void(
        """
        UPDATE business_app_core.appointments
        SET status = 'cancelled', updated_at = now() WHERE id = $1
        """,
        appointment_id,
    )
    return True


async def log_auth_event(
    *, call_id: UUID, customer_id: UUID | None,
    verification_question: str, caller_answer_excerpt: str, passed: bool,
) -> None:
    await connection.execute_void(
        """
        INSERT INTO voice_agent.auth_events
            (call_id, customer_id, verification_question,
             caller_answer_excerpt, passed)
        VALUES ($1, $2, $3, $4, $5)
        """,
        call_id, customer_id, verification_question, caller_answer_excerpt, passed,
    )
```

- [ ] **Step 4: Extend the booking routes**

Append to `booking_engine/api/routes/voice_tools_booking.py`:

```python
from booking_engine.db.voice_tool_queries import (
    cancel_appointment, get_next_booking_for_customer, log_auth_event,
    modify_appointment,
)


class GetBookingIn(BaseModel):
    customer_id: UUID
    fuzzy_when: str | None = None


@router.post("/get_booking")
async def get_booking(
    body: GetBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[dict | None]:
    row = await get_next_booking_for_customer(
        shop_id=x_shop_id, customer_id=body.customer_id,
    )
    return Envelope[dict | None](ok=True, data=row)


@router.post("/modify_booking")
async def modify_booking(
    body: ModifyBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    if not body.verification_passed:
        await log_auth_event(
            call_id=x_call_id, customer_id=None,
            verification_question="modify_booking",
            caller_answer_excerpt="", passed=False,
        )
        return Envelope[dict](ok=False, error="unauthorized")
    ok = await modify_appointment(
        appointment_id=body.appointment_id,
        new_slot_start=body.new_slot_start,
        new_service_id=body.new_service_id,
    )
    await log_auth_event(
        call_id=x_call_id, customer_id=None,
        verification_question="modify_booking",
        caller_answer_excerpt="", passed=True,
    )
    return Envelope[dict](ok=ok, data={"updated": ok})


@router.post("/cancel_booking")
async def cancel_booking(
    body: CancelBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    if not body.verification_passed:
        await log_auth_event(
            call_id=x_call_id, customer_id=None,
            verification_question="cancel_booking",
            caller_answer_excerpt="", passed=False,
        )
        return Envelope[dict](ok=False, error="unauthorized")
    ok = await cancel_appointment(appointment_id=body.appointment_id)
    await log_auth_event(
        call_id=x_call_id, customer_id=None,
        verification_question="cancel_booking",
        caller_answer_excerpt="", passed=True,
    )
    return Envelope[dict](ok=ok, data={"cancelled": ok})
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_tools_booking.py -v
```

Expected: 5 passing total in this file.

- [ ] **Step 6: Commit**

```
git add booking_engine/api/routes/voice_tools_booking.py booking_engine/db/voice_tool_queries.py tests/voice_gateway/test_voice_tools_booking.py
git commit -m "feat(voice): get_booking, modify_booking, cancel_booking with auth audit"
```

---

### Task 9: Lifecycle tools — `mark_outcome` and `escalate_to_merchant` (memo)

**Files:**
- Create: `booking_engine/api/routes/voice_tools_lifecycle.py`
- Create: `tests/voice_gateway/test_voice_tools_lifecycle.py`
- Create: `booking_engine/db/voice_calls_queries.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_voice_tools_lifecycle.py`:

```python
"""Tests for mark_outcome and escalate_to_merchant tool endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(uuid4()),
        "X-Call-Id": str(uuid4())}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_mark_outcome_updates_call_row():
    with patch("booking_engine.api.routes.voice_tools_lifecycle.set_call_outcome",
               new=AsyncMock(return_value=None)) as fn:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/mark_outcome",
                headers=AUTH,
                json={"outcome": "booked",
                      "summary": "Maria ha prenotato venerdì alle 10."},
            )
    assert r.json()["ok"] is True
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalate_creates_memo_and_pushes():
    memo_id = uuid4()
    with patch("booking_engine.api.routes.voice_tools_lifecycle.insert_callback_memo",
               new=AsyncMock(return_value=memo_id)), \
         patch("booking_engine.api.routes.voice_tools_lifecycle.send_push",
               new=AsyncMock(return_value=None)) as push, \
         patch("booking_engine.api.routes.voice_tools_lifecycle.set_call_outcome",
               new=AsyncMock(return_value=None)), \
         patch("booking_engine.api.routes.voice_tools_lifecycle.get_call",
               new=AsyncMock(return_value={
                   "shop_id": uuid4(),
                   "matched_customer_id": uuid4(),
                   "caller_phone": "+393201234567",
               })):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/escalate_to_merchant",
                headers=AUTH,
                json={"reason": "vuole parlare con Giulia",
                      "callback_window": "oggi pomeriggio",
                      "customer_message": "Vorrebbe cambiare data."},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["memo_id"] == str(memo_id)
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_new_memo"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_tools_lifecycle.py -v
```

- [ ] **Step 3: Add call+memo queries**

Create `booking_engine/db/voice_calls_queries.py`:

```python
"""DB access for voice_agent.calls, call_turns, callback_memos."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from booking_engine.db import connection


async def insert_call(
    *, shop_id: UUID, caller_phone: str | None,
    matched_customer_id: UUID | None,
) -> UUID:
    row = await connection.execute_one(
        """
        INSERT INTO voice_agent.calls
            (shop_id, caller_phone, matched_customer_id, started_at)
        VALUES ($1, $2, $3, now())
        RETURNING id
        """,
        shop_id, caller_phone, matched_customer_id,
    )
    return row["id"]


async def get_call(call_id: UUID) -> dict | None:
    return await connection.execute_one(
        "SELECT * FROM voice_agent.calls WHERE id = $1", call_id,
    )


async def set_call_outcome(
    *, call_id: UUID, outcome: str, summary: str,
    callback_window: str | None,
) -> None:
    await connection.execute_void(
        """
        UPDATE voice_agent.calls
        SET outcome = $2, summary = $3, outcome_reason = $4
        WHERE id = $1
        """,
        call_id, outcome, summary, callback_window,
    )


async def finalize_call(
    *, call_id: UUID, ended_at: datetime, duration_seconds: int,
) -> None:
    await connection.execute_void(
        """
        UPDATE voice_agent.calls
        SET ended_at = $2, duration_seconds = $3
        WHERE id = $1
        """,
        call_id, ended_at, duration_seconds,
    )


async def insert_call_turn(
    *, call_id: UUID, role: str, text: str, seq: int,
) -> None:
    await connection.execute_void(
        """
        INSERT INTO voice_agent.call_turns (call_id, role, text, seq)
        VALUES ($1, $2, $3, $4)
        """,
        call_id, role, text, seq,
    )


async def insert_callback_memo(
    *, call_id: UUID, shop_id: UUID, customer_id: UUID | None,
    caller_phone: str | None, reason: str, callback_window: str | None,
) -> UUID:
    row = await connection.execute_one(
        """
        INSERT INTO voice_agent.callback_memos
            (call_id, shop_id, customer_id, caller_phone, reason, callback_window)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        call_id, shop_id, customer_id, caller_phone, reason, callback_window,
    )
    return row["id"]
```

- [ ] **Step 4: Implement routes**

Create `booking_engine/api/routes/voice_tools_lifecycle.py`:

```python
"""Lifecycle tools — mark_outcome and escalate_to_merchant."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    EscalateIn, Envelope, MarkOutcomeIn,
)
from booking_engine.clients.push_notifications import send_push
from booking_engine.db.voice_calls_queries import (
    get_call, insert_callback_memo, set_call_outcome,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-lifecycle"])


@router.post("/mark_outcome")
async def mark_outcome(
    body: MarkOutcomeIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    await set_call_outcome(
        call_id=x_call_id, outcome=body.outcome, summary=body.summary,
        callback_window=body.callback_window,
    )
    return Envelope[dict](ok=True, data={"marked": True})


@router.post("/escalate_to_merchant")
async def escalate_to_merchant(
    body: EscalateIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    call = await get_call(x_call_id)
    if not call:
        return Envelope[dict](ok=False, error="call_not_found")
    memo_id = await insert_callback_memo(
        call_id=x_call_id, shop_id=call["shop_id"],
        customer_id=call.get("matched_customer_id"),
        caller_phone=call.get("caller_phone"),
        reason=f"{body.reason} — {body.customer_message}",
        callback_window=body.callback_window,
    )
    await set_call_outcome(
        call_id=x_call_id, outcome="escalated",
        summary=body.customer_message,
        callback_window=body.callback_window,
    )
    await send_push(
        shop_id=call["shop_id"], event="voice_new_memo",
        payload={"memo_id": str(memo_id), "reason": body.reason,
                 "caller_phone": call.get("caller_phone")},
    )
    return Envelope[dict](ok=True, data={"memo_id": str(memo_id)})
```

- [ ] **Step 5: Register router**

```python
from booking_engine.api.routes import voice_tools_lifecycle
app.include_router(voice_tools_lifecycle.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_tools_lifecycle.py -v
```

Expected: 2 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_tools_lifecycle.py booking_engine/db/voice_calls_queries.py booking_engine/app.py tests/voice_gateway/test_voice_tools_lifecycle.py
git commit -m "feat(voice): lifecycle tools — mark_outcome and escalate_to_merchant memo creation"
```

---

### Task 10: Session event webhooks (session.started, turn, session.ended)

**Files:**
- Create: `booking_engine/api/routes/voice_events.py`
- Create: `tests/voice_gateway/test_voice_events.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_voice_events.py`:

```python
"""Tests for OpenAI session-lifecycle webhooks."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app
from booking_engine.services.identity_resolver import ResolutionResult

AUTH = {"Authorization": "Bearer tool-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_session_started_returns_assembled_prompt():
    shop_id = uuid4()
    call_id = uuid4()
    config = {
        "display_name": "Salone Lucia",
        "greeting_after_disclosure": "Sono Aria",
        "voice_preset": "warm_female", "tone_preset": "warm",
        "answer_mode": "overflow", "services_to_mention": [],
    }
    policy = {"disclosure_text": "Salve, assistente AI...",
              "recording_consent_prompt": "Posso aiutarla?"}
    with patch("booking_engine.api.routes.voice_events.get_config",
               new=AsyncMock(return_value=config)), \
         patch("booking_engine.api.routes.voice_events.get_policy",
               new=AsyncMock(return_value=policy)), \
         patch("booking_engine.api.routes.voice_events.resolve_caller",
               new=AsyncMock(return_value=ResolutionResult(is_anonymous=False, matches=[]))), \
         patch("booking_engine.api.routes.voice_events.insert_call",
               new=AsyncMock(return_value=call_id)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/events/session.started",
                headers={**AUTH, "X-Shop-Id": str(shop_id)},
                json={"caller_phone": "+393201234567",
                      "openai_session_id": "sess_123"},
            )
    body = r.json()
    assert "prompt" in body["data"]
    assert "tools" in body["data"]
    assert body["data"]["call_id"] == str(call_id)


@pytest.mark.asyncio
async def test_session_turn_appends_to_transcript():
    call_id = uuid4()
    with patch("booking_engine.api.routes.voice_events.insert_call_turn",
               new=AsyncMock(return_value=None)) as fn:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                f"/voice/events/session.turn",
                headers={**AUTH, "X-Call-Id": str(call_id)},
                json={"role": "caller", "text": "Ciao!", "seq": 1},
            )
    assert r.json()["ok"] is True
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_ended_finalizes_and_debits():
    call_id = uuid4()
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_events.finalize_call",
               new=AsyncMock(return_value=None)), \
         patch("booking_engine.api.routes.voice_events.get_call",
               new=AsyncMock(return_value={
                   "id": call_id, "shop_id": shop_id, "outcome": "booked",
               })), \
         patch("booking_engine.api.routes.voice_events.record_voice_debit",
               new=AsyncMock(return_value=None)) as debit:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/events/session.ended",
                headers={**AUTH, "X-Call-Id": str(call_id)},
                json={"duration_seconds": 180, "tool_token_cost": 200,
                      "ended_at": datetime.now(timezone.utc).isoformat()},
            )
    assert r.json()["ok"] is True
    debit.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_events.py -v
```

- [ ] **Step 3: Add policy query**

Append to `booking_engine/db/voice_config_queries.py`:

```python
async def get_policy(locale: str = "it-IT") -> dict | None:
    return await connection.execute_one(
        "SELECT * FROM voice_agent.system_policy WHERE locale = $1",
        locale,
    )
```

- [ ] **Step 4: Implement webhooks**

Create `booking_engine/api/routes/voice_events.py`:

```python
"""OpenAI session-lifecycle webhooks.

session.started → identify caller, assemble system prompt, return as session update.
session.turn    → append transcript fragment to voice_agent.call_turns.
session.ended   → finalize call row, debit tokens, trigger post-hoc classifier if needed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import Envelope
from booking_engine.config import Settings, get_settings
from booking_engine.db.voice_calls_queries import (
    finalize_call, get_call, insert_call, insert_call_turn,
)
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.prompt_assembler import assemble_session_prompt
from booking_engine.services.token_meter import record_voice_debit

router = APIRouter(prefix="/voice/events", tags=["voice-events"])


class StartedIn(BaseModel):
    caller_phone: str | None = None
    openai_session_id: str | None = None


class TurnIn(BaseModel):
    role: str  # caller | agent | tool
    text: str
    seq: int


class EndedIn(BaseModel):
    duration_seconds: int
    tool_token_cost: int = 0
    ended_at: datetime


@router.post("/session.started")
async def session_started(
    body: StartedIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[dict]:
    config = await get_config(x_shop_id)
    if not config:
        return Envelope[dict](ok=False, error="shop_config_missing")
    policy = await get_policy()
    if not policy:
        return Envelope[dict](ok=False, error="policy_missing")
    resolution = await resolve_caller(
        shop_id=x_shop_id, caller_phone=body.caller_phone,
    )
    matched_id = (
        resolution.unique_match.customer_id if resolution.unique_match else None
    )
    call_id = await insert_call(
        shop_id=x_shop_id, caller_phone=body.caller_phone,
        matched_customer_id=matched_id,
    )
    assembled = assemble_session_prompt(
        config=config, policy=policy, resolution=resolution,
    )
    return Envelope[dict](ok=True, data={
        "call_id": str(call_id),
        "prompt": assembled.prompt,
        "tools": assembled.tools,
        "voice": assembled.voice,
    })


@router.post("/session.turn")
async def session_turn(
    body: TurnIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    await insert_call_turn(
        call_id=x_call_id, role=body.role, text=body.text, seq=body.seq,
    )
    return Envelope[dict](ok=True, data={"appended": True})


@router.post("/session.ended")
async def session_ended(
    body: EndedIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    await finalize_call(
        call_id=x_call_id, ended_at=body.ended_at,
        duration_seconds=body.duration_seconds,
    )
    call = await get_call(x_call_id)
    if not call:
        return Envelope[dict](ok=False, error="call_not_found")
    await record_voice_debit(
        shop_id=call["shop_id"], call_id=x_call_id,
        duration_seconds=body.duration_seconds,
        tool_token_cost=body.tool_token_cost,
        tokens_per_second=settings.voice_kairo_tokens_per_second,
    )
    return Envelope[dict](ok=True, data={"finalized": True})
```

- [ ] **Step 5: Register router**

```python
from booking_engine.api.routes import voice_events
app.include_router(voice_events.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_events.py -v
```

Expected: 3 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_events.py booking_engine/db/voice_config_queries.py booking_engine/app.py tests/voice_gateway/test_voice_events.py
git commit -m "feat(voice): session lifecycle webhooks (started, turn, ended)"
```

---

### Task 11: Memos query endpoint for webapp consumption

**Files:**
- Create: `booking_engine/api/routes/voice_memos.py`
- Create: `tests/voice_gateway/test_voice_memos_routes.py`
- Modify: `booking_engine/app.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice_gateway/test_voice_memos_routes.py`:

```python
"""Tests for the webapp-facing /voice/memos endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.app import app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_list_pending_memos():
    shop_id = uuid4()
    fake = [{"id": uuid4(), "call_id": uuid4(), "shop_id": shop_id,
             "customer_id": None, "caller_phone": "+393201234567",
             "reason": "Vuole cambiare data", "callback_window": "oggi pomeriggio",
             "status": "pending", "actioned_by": None, "actioned_at": None,
             "created_at": "2026-06-03T14:32:00Z"}]
    with patch("booking_engine.api.routes.voice_memos.list_memos",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/voice/memos/{shop_id}?status=pending", headers=AUTH)
    body = r.json()
    assert body["data"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_action_memo_updates_status():
    memo_id = uuid4()
    staff_id = uuid4()
    with patch("booking_engine.api.routes.voice_memos.update_memo_status",
               new=AsyncMock(return_value=True)):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/voice/memos/{memo_id}",
                headers=AUTH,
                json={"status": "actioned", "actioned_by": str(staff_id)},
            )
    assert r.json()["data"]["updated"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/voice_gateway/test_voice_memos_routes.py -v
```

- [ ] **Step 3: Implement queries**

Append to `booking_engine/db/voice_calls_queries.py`:

```python
async def list_memos(
    *, shop_id: UUID, status: str | None = "pending", limit: int = 50,
) -> list[dict]:
    if status:
        return await connection.execute(
            """
            SELECT * FROM voice_agent.callback_memos
            WHERE shop_id = $1 AND status = $2
            ORDER BY created_at DESC LIMIT $3
            """,
            shop_id, status, limit,
        )
    return await connection.execute(
        """
        SELECT * FROM voice_agent.callback_memos
        WHERE shop_id = $1
        ORDER BY created_at DESC LIMIT $2
        """,
        shop_id, limit,
    )


async def update_memo_status(
    *, memo_id: UUID, status: str, actioned_by: UUID | None,
) -> bool:
    await connection.execute_void(
        """
        UPDATE voice_agent.callback_memos
        SET status = $2,
            actioned_by = $3,
            actioned_at = CASE WHEN $2 IN ('actioned','dismissed') THEN now() ELSE actioned_at END
        WHERE id = $1
        """,
        memo_id, status, actioned_by,
    )
    return True
```

- [ ] **Step 4: Implement routes**

Create `booking_engine/api/routes/voice_memos.py`:

```python
"""Memo endpoints used by webapp to populate Inbox panel."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db.voice_calls_queries import (
    list_memos, update_memo_status,
)

router = APIRouter(prefix="/voice/memos", tags=["voice-memos"])


class MemoPatch(BaseModel):
    status: str
    actioned_by: UUID | None = None


@router.get("/{shop_id}")
async def list_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    status: str | None = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    rows = await list_memos(shop_id=shop_id, status=status, limit=limit)
    return {"data": rows}


@router.patch("/{memo_id}")
async def action_memo(
    memo_id: UUID,
    body: MemoPatch,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    ok = await update_memo_status(
        memo_id=memo_id, status=body.status, actioned_by=body.actioned_by,
    )
    return {"data": {"updated": ok}}
```

- [ ] **Step 5: Register router**

```python
from booking_engine.api.routes import voice_memos
app.include_router(voice_memos.router)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/voice_gateway/test_voice_memos_routes.py -v
```

Expected: 2 passing.

- [ ] **Step 7: Commit**

```
git add booking_engine/api/routes/voice_memos.py booking_engine/db/voice_calls_queries.py booking_engine/app.py tests/voice_gateway/test_voice_memos_routes.py
git commit -m "feat(voice): /voice/memos GET and PATCH endpoints for webapp Inbox panel"
```

---

### Task 12: Full suite + deploy notes

- [ ] **Step 1: Run full test suite**

```
pytest tests/voice_gateway/ -v
```

Expected: ~50+ tests passing across Plans A+B.

- [ ] **Step 2: Append deploy notes**

Append to `docs/DEPLOY_VOICE_AGENT.md`:

```markdown
## Plan B — Tools & identity deploy notes (2026-06-03)

### New env vars on Lambda
- `OPENAI_TOOL_SECRET` — bearer secret shared with OpenAI for tool + event webhook auth

### OpenAI configuration
After deploying, configure OpenAI's SIP routing to point session-lifecycle webhooks to:
- POST {public_base_url}/voice/events/session.started
- POST {public_base_url}/voice/events/session.turn
- POST {public_base_url}/voice/events/session.ended

And tool endpoints (one per tool, all under {public_base_url}/voice/tools/*) with bearer token from OPENAI_TOOL_SECRET.

### No new migrations
Plan B uses tables created in Plan A's migration 04.
```

- [ ] **Step 3: Commit**

```
git add docs/DEPLOY_VOICE_AGENT.md
git commit -m "docs(voice): Plan B deploy notes"
```

---

## Done definition for Plan B

- All 12 tasks committed.
- `pytest tests/voice_gateway/` passes (Plans A+B combined).
- All 12 agent tools have endpoints exposed under `/voice/tools/*` with bearer auth.
- Session lifecycle webhooks under `/voice/events/*` are wired to identity resolver, prompt assembler, transcript appender, and token meter.
- Memo creation on `escalate_to_merchant` triggers push event `voice_new_memo`.
- Modify/cancel booking tools require `verification_passed=true` and write to `voice_agent.auth_events`.

Plan C builds the webapp surfaces consuming these endpoints.
