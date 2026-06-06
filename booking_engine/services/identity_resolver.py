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