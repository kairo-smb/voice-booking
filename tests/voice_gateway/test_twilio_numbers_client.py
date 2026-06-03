"""Tests for the Twilio Numbers API client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from booking_engine.clients.twilio_numbers import (
    search_available_numbers,
    purchase_number,
    AvailableNumber,
)


@pytest.fixture
def fake_twilio():
    with patch("booking_engine.clients.twilio_numbers.Client") as cls:
        client = MagicMock()
        cls.return_value = client
        yield client


def test_search_available_numbers_returns_typed(fake_twilio):
    fake_twilio.available_phone_numbers.return_value.local.list.return_value = [
        MagicMock(phone_number="+390212345678",
                  friendly_name="Milano",
                  locality="Milano",
                  region="Lombardia"),
        MagicMock(phone_number="+390212345679",
                  friendly_name="Milano",
                  locality="Milano",
                  region="Lombardia"),
    ]
    results = search_available_numbers(area_code="02", country="IT", limit=5,
                                       account_sid="AC", auth_token="tok")
    assert len(results) == 2
    assert results[0].phone_number == "+390212345678"
    assert isinstance(results[0], AvailableNumber)


def test_purchase_number_returns_sid(fake_twilio):
    fake_twilio.incoming_phone_numbers.create.return_value = MagicMock(
        sid="PN1234", phone_number="+390212345678"
    )
    result = purchase_number(
        phone_number="+390212345678",
        voice_url="https://api.example.com/voice/twiml/incoming",
        account_sid="AC", auth_token="tok",
    )
    assert result.sid == "PN1234"
    assert result.phone_number == "+390212345678"
    fake_twilio.incoming_phone_numbers.create.assert_called_once()
    kwargs = fake_twilio.incoming_phone_numbers.create.call_args.kwargs
    assert kwargs["phone_number"] == "+390212345678"
    assert kwargs["voice_url"] == "https://api.example.com/voice/twiml/incoming"
    assert kwargs["voice_method"] == "POST"
