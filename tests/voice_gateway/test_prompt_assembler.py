"""Tests for the 3-layer system-prompt assembler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from booking_engine.api.voice_tool_models import CustomerSummary
from booking_engine.services import prompt_assembler as pa
from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.prompt_assembler import (
    DEFAULT_TONE_INSTRUCTION,
    assemble_session_prompt,
)


def _config(**overrides):
    base = {
        "display_name": "Salone Lucia",
        "greeting_after_disclosure": "Sono Aria, come posso aiutarla?",
        "tone_id": None,
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


@pytest.mark.asyncio
async def test_assemble_includes_safety_rules():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "REGOLE NON NEGOZIABILI" in out.prompt


@pytest.mark.asyncio
async def test_assemble_opens_with_self_introduction_no_disclosure_fluff():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "APERTURA CHIAMATA" in out.prompt
    assert "presentarti" in out.prompt
    assert "assistente AI" not in out.prompt


@pytest.mark.asyncio
async def test_assemble_personalizes_for_known_caller():
    cid = uuid4()
    match = CustomerSummary(
        customer_id=cid, first_name="Maria", last_name="Rossi",
        last_visit_at=datetime.now(timezone.utc) - timedelta(days=14),
        preferred_staff_id=None, notes_tags=["preferisce shampoo idratante"],
        verified=True,
    )
    resolution = ResolutionResult(is_anonymous=False, matches=[match])
    out = await assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "Maria" in out.prompt
    assert "Saluta" in out.prompt or "saluta" in out.prompt


@pytest.mark.asyncio
async def test_assemble_anonymous_uses_neutral_greeting():
    resolution = ResolutionResult(is_anonymous=True, matches=[])
    out = await assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    assert "anonimo" in out.prompt.lower() or "non ho il suo numero" in out.prompt.lower()


@pytest.mark.asyncio
async def test_assemble_returns_tool_descriptions():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(), policy=_policy(), resolution=resolution,
    )
    names = {t["name"] for t in out.tools}
    assert "lookup_customer" in names
    assert "create_booking" in names
    assert len(out.tools) == 12


@pytest.mark.asyncio
async def test_assemble_returns_voice_preset():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(voice_preset="neutral_male"), policy=_policy(),
        resolution=resolution,
    )
    assert out.voice == "neutral_male"


@pytest.mark.asyncio
async def test_assemble_uses_db_tone_instruction(monkeypatch):
    tone_id = uuid4()

    async def fake_get(_id):
        assert _id == tone_id
        return {
            "id": tone_id,
            "name": "professionale",
            "description": "x",
            "system_prompt_instruction": "MARKER_TONE_PROF",
            "is_preset": True,
        }

    monkeypatch.setattr(pa, "get_tone_by_id", fake_get)
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(tone_id=tone_id), policy=_policy(), resolution=resolution,
    )
    assert "MARKER_TONE_PROF" in out.prompt


@pytest.mark.asyncio
async def test_assemble_falls_back_to_default_when_tone_missing(monkeypatch):
    async def fake_get(_id):
        return None

    monkeypatch.setattr(pa, "get_tone_by_id", fake_get)
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(tone_id=uuid4()), policy=_policy(), resolution=resolution,
    )
    assert DEFAULT_TONE_INSTRUCTION in out.prompt


@pytest.mark.asyncio
async def test_assemble_falls_back_to_default_on_db_error(monkeypatch):
    async def boom(_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(pa, "get_tone_by_id", boom)
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(tone_id=uuid4()), policy=_policy(), resolution=resolution,
    )
    assert DEFAULT_TONE_INSTRUCTION in out.prompt


@pytest.mark.asyncio
async def test_overflow_mode_uses_custom_greeting_overflow():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(answer_mode="overflow", greeting_overflow="CIAO_OVERFLOW"),
        policy=_policy(), resolution=resolution,
    )
    assert 'FRASE DI BENVENUTO: "CIAO_OVERFLOW"' in out.prompt


@pytest.mark.asyncio
async def test_overflow_mode_defaults_when_greeting_overflow_empty():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(answer_mode="overflow", greeting_overflow=""),
        policy=_policy(), resolution=resolution,
    )
    # default overflow greeting acknowledges the salon can't pick up right now
    assert "non possiamo rispondere" in out.prompt.lower()


@pytest.mark.asyncio
async def test_always_on_mode_uses_greeting_after_disclosure():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    out = await assemble_session_prompt(
        config=_config(answer_mode="always_on",
                       greeting_after_disclosure="BENV_FULL"),
        policy=_policy(), resolution=resolution,
    )
    assert 'FRASE DI BENVENUTO: "BENV_FULL"' in out.prompt
