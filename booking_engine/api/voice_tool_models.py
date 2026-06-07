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


# Catalog (request models)
class GetServicesIn(BaseModel):
    filter: str | None = None


class GetStaffForServiceIn(BaseModel):
    service_id: UUID


# Identity (request model for lookup_customer)
class LookupCustomerIn(BaseModel):
    phone: str