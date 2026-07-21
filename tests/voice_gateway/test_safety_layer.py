"""Tests for the Layer 3 safety prompt and tool descriptions."""
from booking_engine.services.safety_layer import (
    SAFETY_PROMPT,
    DEFAULT_TOOL_ALLOWLIST,
    _TOOL_SCHEMAS,
    tool_descriptions,
)


def test_safety_prompt_mentions_key_rules():
    text = SAFETY_PROMPT.lower()
    assert "medic" in text  # no medical advice
    assert "prezz" in text or "pric" in text  # no price negotiation
    assert "umano" in text or "salone" in text  # escalation
    assert "ruolo" in text  # role-lock / anti prompt-injection
    assert "ambito" in text  # scope limiting
    assert "privacy" in text  # no PII of other customers
    assert "inventare" in text  # no hallucination


def test_default_allowlist_contains_12_tools():
    assert len(DEFAULT_TOOL_ALLOWLIST) == 12
    assert "create_booking" in DEFAULT_TOOL_ALLOWLIST
    assert "escalate_to_merchant" in DEFAULT_TOOL_ALLOWLIST


def test_tool_descriptions_filtered_by_allowlist():
    descs = tool_descriptions(allowlist=["lookup_customer", "create_booking"])
    names = {d["name"] for d in descs}
    assert names == {"lookup_customer", "create_booking"}


def test_safety_prompt_mentions_price_gating_rule():
    text = SAFETY_PROMPT.lower()
    assert "include_price" in text


def test_get_services_schema_has_include_price_param():
    schema = _TOOL_SCHEMAS["get_services"]["parameters"]
    assert schema["properties"]["include_price"]["type"] == "boolean"


def test_safety_prompt_mentions_multi_service_ordering_rule():
    text = SAFETY_PROMPT.lower()
    assert "servizi multipli" in text


def test_check_availability_schema_requires_services_list():
    schema = _TOOL_SCHEMAS["check_availability"]["parameters"]
    assert schema["required"] == ["services"]
    assert schema["properties"]["services"]["type"] == "array"


def test_create_booking_schema_requires_legs_list():
    schema = _TOOL_SCHEMAS["create_booking"]["parameters"]
    assert schema["required"] == ["customer_id", "legs"]