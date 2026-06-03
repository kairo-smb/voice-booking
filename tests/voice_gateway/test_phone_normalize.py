"""Tests for the phone number normalization helper."""
from __future__ import annotations

import pytest

from booking_engine.services.phone_normalize import normalize_e164, digits_only


@pytest.mark.parametrize("raw,expected", [
    ("+39 320 123 4567", "+393201234567"),
    ("0039 320 1234567", "+393201234567"),
    ("320.123.4567", "+393201234567"),
    ("320-1234567", "+393201234567"),
    ("3201234567", "+393201234567"),
])
def test_normalize_e164_italian_variants(raw, expected):
    assert normalize_e164(raw, default_region="IT") == expected


def test_normalize_e164_returns_none_for_invalid():
    assert normalize_e164("not a phone", default_region="IT") is None
    assert normalize_e164("", default_region="IT") is None
    assert normalize_e164(None, default_region="IT") is None


@pytest.mark.parametrize("raw,expected", [
    ("+39 320 123 4567", "393201234567"),
    ("320-123-4567", "3201234567"),
    ("", ""),
    (None, ""),
])
def test_digits_only(raw, expected):
    assert digits_only(raw) == expected
