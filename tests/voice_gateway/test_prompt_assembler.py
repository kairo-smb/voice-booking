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