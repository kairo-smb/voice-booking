"""Tests for the Layer 3 safety prompt and tool descriptions."""
from booking_engine.services.safety_layer import (
    SAFETY_PROMPT,
    DEFAULT_TOOL_ALLOWLIST,
    tool_descriptions,
)


def test_safety_prompt_mentions_key_rules():
    text = SAFETY_PROMPT.lower()
    assert "medic" in text  # no medical advice
    assert "prezz" in text or "pric" in text  # no price negotiation
    assert "umano" in text or "salone" in text  # escalation


def test_default_allowlist_contains_12_tools():
    assert len(DEFAULT_TOOL_ALLOWLIST) == 12
    assert "create_booking" in DEFAULT_TOOL_ALLOWLIST
    assert "escalate_to_merchant" in DEFAULT_TOOL_ALLOWLIST


def test_tool_descriptions_filtered_by_allowlist():
    descs = tool_descriptions(allowlist=["lookup_customer", "create_booking"])
    names = {d["name"] for d in descs}
    assert names == {"lookup_customer", "create_booking"}