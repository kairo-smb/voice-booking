"""Pydantic models for Booking Engine API requests and responses."""
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    error: str
    message: str


class ShopResponse(BaseModel):
    id: UUID
    name: str
    phone_number: str | None = None
    address: str | None = None
    welcome_message: str | None = None
    tone_instructions: str | None = None
    personality: str | None = None
    special_instructions: str | None = None
    voice: str = "alloy"
    language: str = "it"
    is_active: bool


class StaffResponse(BaseModel):
    id: UUID
    full_name: str
    role: str | None = None
    bio: str | None = None


class ServiceResponse(BaseModel):
    id: UUID
    service_name: str
    description: str | None = None
    duration_minutes: int
    price_eur: Decimal | None = None
    category: str | None = None


class CustomerResponse(BaseModel):
    id: UUID
    full_name: str
    preferred_staff_id: UUID | None = None
    notes: str | None = None


class CreateCustomerRequest(BaseModel):
    full_name: str
    phone_number: str | None = None


class AvailableSlotResponse(BaseModel):
    staff_id: UUID
    staff_name: str
    slot_start: datetime
    slot_end: datetime


class AvailabilityResponse(BaseModel):
    slots: list[AvailableSlotResponse]
    suggestions: list[AvailableSlotResponse] | None = None


class AppointmentServiceDetail(BaseModel):
    service_id: UUID
    service_name: str | None = None
    duration_minutes: int
    price_eur: Decimal | None = None


class AppointmentResponse(BaseModel):
    id: UUID
    customer_id: UUID
    staff_id: UUID
    staff_name: str | None = None
    start_time: datetime
    end_time: datetime
    status: str
    services: list[AppointmentServiceDetail] = []
    notes: str | None = None


class CreateAppointmentRequest(BaseModel):
    customer_id: UUID
    service_ids: list[UUID] = Field(min_length=1)
    staff_id: UUID
    start_time: datetime
    notes: str | None = None


class RescheduleRequest(BaseModel):
    new_start_time: datetime
    new_staff_id: UUID | None = None
