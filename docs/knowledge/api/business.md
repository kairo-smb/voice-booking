# Business API

Plain CRUD REST endpoints — the original pre-voice booking API. **No authentication on any route below** — this is a real, current gap, not a doc omission (verified: none of `shops.py`/`customers.py`/`services.py`/`availability.py`/`appointments.py` declare a `Depends()` auth check).

> **Maintenance rule:** an endpoint added/removed/changed here updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Method | Path | File | Notes |
|---|---|---|---|
| `GET` | `/api/v1/shops/{shop_id}` | `shops.py` | 404 via `ErrorResponse` model if not found |
| `GET` | `/api/v1/shops/{shop_id}/services` | `services.py` | |
| `GET` | `/api/v1/shops/{shop_id}/staff` | `services.py` | |
| `GET` | `/api/v1/shops/{shop_id}/staff/{staff_id}/services` | `services.py` | |
| `GET` | `/api/v1/shops/{shop_id}/customers` | `customers.py` | |
| `POST` | `/api/v1/shops/{shop_id}/customers` | `customers.py` | 201 on success |
| `GET` | `/api/v1/shops/{shop_id}/availability` | `availability.py` | see [Voice Agent Logic](../voice-agent-logic.md) for the underlying slot-search algorithm (`get_available_slots`/`get_available_slot_chains` in `booking_engine/db/queries.py`) |
| `POST` | `/api/v1/shops/{shop_id}/appointments` | `appointments.py` | 201, or 409 (`ErrorResponse`) on slot conflict |
| `GET` | `/api/v1/shops/{shop_id}/appointments` | `appointments.py` | |
| `PATCH` | `/api/v1/shops/{shop_id}/appointments/{appointment_id}/cancel` | `appointments.py` | 409 on conflict |
| `PATCH` | `/api/v1/shops/{shop_id}/appointments/{appointment_id}/reschedule` | `appointments.py` | 404 or 409 |

Exact request/response field types: the Pydantic models in `booking_engine/api/models.py`, or FastAPI's own generated schema at `/docs` (Swagger) / `/openapi.json` on a running instance — that's the live, always-current reference for shapes; this page is the narrative/auth layer on top of it.
