"""Pydantic models for voice control-plane endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


Outcome = Literal[
    "booked", "rescheduled", "cancelled", "info",
    "abandoned", "escalated", "failed",
]
CustomerMatch = Literal["existing", "created", "unmatched", "ambiguous"]
Voice = Literal["alloy", "echo", "shimmer", "ash", "ballad", "coral", "sage", "verse"]
Language = Literal["it", "en", "es"]


class VoiceConfigResponse(BaseModel):
    welcome_message: str | None = None
    tone_instructions: str | None = None
    personality: str | None = None
    special_instructions: str | None = None
    voice: Voice
    language: Language
    is_active: bool


class VoiceConfigUpdateRequest(BaseModel):
    welcome_message: str | None = None
    tone_instructions: str | None = None
    personality: str | None = None
    special_instructions: str | None = None
    voice: Voice | None = None
    language: Language | None = None
    is_active: bool | None = None


class CallSummary(BaseModel):
    id: UUID
    caller_number: str
    customer_id: UUID | None = None
    customer_match: CustomerMatch
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    outcome: Outcome | None = None
    summary: str | None = None
    appointment_id: UUID | None = None


class TranscriptTurn(BaseModel):
    turn_index: int
    role: Literal["caller", "assistant", "system"]
    text: str
    at: datetime


class CallEvent(BaseModel):
    at: datetime
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CallDetail(BaseModel):
    call: CallSummary
    transcript: list[TranscriptTurn]
    events: list[CallEvent]
    service_brief: dict[str, Any] | None = None


class LinkCustomerRequest(BaseModel):
    customer_id: UUID


class VolumeBlock(BaseModel):
    total: int
    by_day: list[dict[str, Any]]
    avg_duration_sec: int
    failure_rate: float


class OutcomesBlock(BaseModel):
    booked: int
    rescheduled: int
    cancelled: int
    info: int
    abandoned: int
    escalated: int
    failed: int
    conversion_rate: float


class DemandBlock(BaseModel):
    top_services: list[dict[str, Any]]
    top_staff: list[dict[str, Any]]
    by_hour: list[dict[str, Any]]
    by_dow: list[dict[str, Any]]
    after_hours_pct: float


class VoiceAnalyticsResponse(BaseModel):
    volume: dict[str, Any] | VolumeBlock
    outcomes: dict[str, Any] | OutcomesBlock
    demand: dict[str, Any] | DemandBlock
