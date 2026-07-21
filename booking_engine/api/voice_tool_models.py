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
    price_cents: int | None = None


class StaffOut(BaseModel):
    staff_id: UUID
    name: str


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


class ModifyBookingIn(BaseModel):
    appointment_id: UUID
    new_slot_start: datetime | None = None
    new_service_id: UUID | None = None
    verification_passed: bool | None = None  # ignored; server authorizes by caller phone


class CancelBookingIn(BaseModel):
    appointment_id: UUID
    verification_passed: bool | None = None  # ignored; server authorizes by caller phone


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
    include_price: bool = False


class GetStaffForServiceIn(BaseModel):
    service_id: UUID


# Identity (request model for lookup_customer)
class LookupCustomerIn(BaseModel):
    phone: str