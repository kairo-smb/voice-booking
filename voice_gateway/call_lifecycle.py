"""CallSession - owns the persistence lifecycle for one inbound phone call."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from voice_gateway.db import execute, execute_one, execute_void  # noqa: F401


CustomerMatch = str  # 'existing'|'created'|'unmatched'|'ambiguous'


class CallSession:
    """Tracks the lifecycle of a single phone call from start to hangup."""

    def __init__(
        self,
        *,
        shop_id: UUID,
        caller_number: str,
        twilio_call_sid: str | None,
    ) -> None:
        self.shop_id = shop_id
        self.caller_number = caller_number
        self.twilio_call_sid = twilio_call_sid
        self.id: UUID | None = None
        self.customer_id: UUID | None = None
        self.customer_match: CustomerMatch = "unmatched"
        self.started_at: datetime | None = None
        self.appointment_id: UUID | None = None
        self.transcript: list[dict[str, str]] = []

    async def start(self) -> None:
        """Insert the calls row and resolve caller -> customer."""
        matches = await execute(
            "SELECT c.id FROM business_app_core.customers c "
            "JOIN business_app_core.phone_contacts pc ON c.id = pc.customer_id "
            "WHERE c.shop_id = $1 AND pc.phone_number = $2",
            self.shop_id, self.caller_number,
        )
        if len(matches) == 0:
            self.customer_match = "unmatched"
        elif len(matches) == 1:
            self.customer_id = matches[0]["id"]
            self.customer_match = "existing"
        else:
            self.customer_match = "ambiguous"

        self.id = uuid4()
        self.started_at = datetime.now(timezone.utc)
        await execute_void(
            "INSERT INTO voice_agent.calls "
            "(id, shop_id, twilio_call_sid, caller_number, customer_id, "
            " customer_match, started_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            self.id, self.shop_id, self.twilio_call_sid, self.caller_number,
            self.customer_id, self.customer_match, self.started_at,
        )

    async def attach_new_customer(self, customer_id: UUID) -> None:
        """Called when the AI creates a customer mid-call."""
        self.customer_id = customer_id
        self.customer_match = "created"
        await execute_void(
            "UPDATE voice_agent.calls SET customer_id = $1, customer_match = 'created' "
            "WHERE id = $2",
            customer_id, self.id,
        )

    async def append_turn(self, *, role: str, text: str, at: datetime) -> None:
        if self.id is None:
            return
        turn_index = len(self.transcript)
        self.transcript.append({"role": role, "text": text})
        await execute_void(
            "INSERT INTO voice_agent.call_transcripts "
            "(call_id, turn_index, role, text, at) VALUES ($1, $2, $3, $4, $5)",
            self.id, turn_index, role, text, at,
        )

    async def log_event(self, type_: str, payload: dict[str, Any]) -> None:
        if self.id is None:
            return
        import json
        await execute_void(
            "INSERT INTO voice_agent.call_events (call_id, type, payload) "
            "VALUES ($1, $2, $3::jsonb)",
            self.id, type_, json.dumps(payload),
        )

    def set_appointment(self, appointment_id: UUID) -> None:
        self.appointment_id = appointment_id

    async def finalize(
        self,
        *,
        classifier: Callable[..., Awaitable[dict[str, str]]],
        api_key: str,
        model: str,
    ) -> None:
        """Hangup: run classifier, write outcome + ended_at + duration_seconds."""
        if self.id is None or self.started_at is None:
            return
        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - self.started_at).total_seconds())
        result = await classifier(
            api_key=api_key, model=model,
            transcript=self.transcript,
            booked_appointment_id=str(self.appointment_id) if self.appointment_id else None,
        )
        await execute_void(
            "UPDATE voice_agent.calls SET "
            "  ended_at = $1, duration_seconds = $2, "
            "  outcome = $3, outcome_reason = $4, summary = $5, "
            "  appointment_id = $6 "
            "WHERE id = $7",
            ended_at, duration,
            result["outcome"], result["outcome_reason"], result["summary"],
            self.appointment_id, self.id,
        )
