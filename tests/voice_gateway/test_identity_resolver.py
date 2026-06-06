"""Tests for the identity resolver — handles all caller-vs-customer edge cases."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.identity_resolver import (
    resolve_caller,
    ResolutionResult,
)


@pytest.mark.asyncio
async def test_resolve_no_match_returns_empty():
    shop_id = uuid4()
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=[])):
        result = await resolve_caller(shop_id=shop_id, caller_phone="+393201234567")
    assert isinstance(result, ResolutionResult)
    assert result.matches == []
    assert result.unique_match is None


@pytest.mark.asyncio
async def test_resolve_single_match_returns_unique():
    shop_id = uuid4()
    cid = uuid4()
    fake = [{
        "id": cid, "first_name": "Maria", "last_name": "Rossi",
        "last_visit_at": None, "preferred_staff_id": None,
        "notes_tags": [], "verified": True,
    }]
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=fake)):
        result = await resolve_caller(shop_id=shop_id, caller_phone="+393201234567")
    assert len(result.matches) == 1
    assert result.unique_match is not None
    assert result.unique_match.customer_id == cid


@pytest.mark.asyncio
async def test_resolve_multiple_matches_marks_ambiguous():
    shop_id = uuid4()
    fake = [
        {"id": uuid4(), "first_name": "Maria", "last_name": "Rossi",
         "last_visit_at": None, "preferred_staff_id": None,
         "notes_tags": [], "verified": True},
        {"id": uuid4(), "first_name": "Giulia", "last_name": "Rossi",
         "last_visit_at": None, "preferred_staff_id": None,
         "notes_tags": [], "verified": True},
    ]
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=fake)):
        result = await resolve_caller(shop_id=shop_id, caller_phone="+393201234567")
    assert len(result.matches) == 2
    assert result.unique_match is None  # ambiguous


@pytest.mark.asyncio
async def test_resolve_anonymous_caller_returns_anonymous():
    shop_id = uuid4()
    result = await resolve_caller(shop_id=shop_id, caller_phone=None)
    assert result.is_anonymous is True
    assert result.matches == []


@pytest.mark.asyncio
async def test_resolve_normalizes_input_phone():
    shop_id = uuid4()
    with patch("booking_engine.services.identity_resolver.find_customers_by_phone",
               new=AsyncMock(return_value=[])) as q:
        await resolve_caller(shop_id=shop_id, caller_phone="320-1234567")
    q.assert_awaited_once()
    # Second positional/kwarg should be normalized digits
    args, kwargs = q.await_args
    assert "3201234567" in str(args) or "3201234567" in str(kwargs)