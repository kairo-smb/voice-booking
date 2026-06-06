"""Tests for the Telnyx Numbers API client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from booking_engine.clients.telnyx_numbers import (
    search_available_numbers,
    purchase_number,
    AvailableNumber,
)


@pytest.fixture
def fake_telnyx():
    with patch("booking_engine.clients.telnyx_numbers.Telnyx") as cls:
        client = MagicMock()
        cls.return_value = client
        yield client


def test_search_available_numbers_returns_typed(fake_telnyx):
    mock_num = MagicMock()
    mock_num.phone_number = "+390212345678"
    mock_num.friendly_name = "Milano"
    mock_num.city = "Milano"
    mock_num.region_information = [MagicMock(region_name="Lombardia")]
    fake_telnyx.available_phone_numbers.list.return_value = MagicMock(
        data=[mock_num, MagicMock(
            phone_number="+390212345679",
            friendly_name="Milano2",
            city="Milano",
            region_information=[MagicMock(region_name="Lombardia")],
        )]
    )
    results = search_available_numbers(area_code="02", country="IT", limit=5, api_key="key")
    assert len(results) == 2
    assert results[0].phone_number == "+390212345678"
    assert isinstance(results[0], AvailableNumber)


def test_purchase_number_returns_sid(fake_telnyx):
    mock_order = MagicMock()
    mock_order.id = "ORD1234"
    mock_order.phone_numbers = [MagicMock(phone_number="+390212345678")]
    fake_telnyx.number_orders.create.return_value = MagicMock(data=mock_order)
    result = purchase_number(
        phone_number="+390212345678",
        voice_url="https://api.example.com/voice/texml/incoming",
        api_key="key",
    )
    assert result.sid == "ORD1234"
    assert result.phone_number == "+390212345678"
    fake_telnyx.number_orders.create.assert_called_once_with(
        phone_numbers=[{"phone_number": "+390212345678"}],
    )
