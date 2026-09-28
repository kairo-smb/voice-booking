"""Pydantic models shared by the agent-facing routes.

`Envelope[T]` is the `{ok, data, error}` shape `/sessions/*` and
`/voice/events/*` answer in — the same shape the voice tools used, which is why
marketing-engine's customer agents can relay it unchanged. The tools themselves
moved to marketing-engine on 2026-09-28 (AGENTS.md); what is left here is what
the remaining routes and `identity_resolver` still use.
"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field


T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None
    error: str | None = None


class CustomerSummary(BaseModel):
    customer_id: UUID
    first_name: str
    last_name: str | None
    last_visit_at: datetime | None
    preferred_staff_id: UUID | None
    notes_tags: list[str] = Field(default_factory=list)
    verified: bool


class EscalateIn(BaseModel):
    reason: str
    callback_window: str | None = None
    customer_message: str
