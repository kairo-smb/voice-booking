"""Per-call signed token that threads shop_id/call_id to the MCP server."""
from __future__ import annotations

from uuid import uuid4

from booking_engine.services.call_token import mint_call_token, verify_call_token

SECRET = "test-secret"


def test_mint_then_verify_roundtrip():
    shop, call = uuid4(), uuid4()
    tok = mint_call_token(shop_id=shop, call_id=call, secret=SECRET)
    claims = verify_call_token(token=tok, secret=SECRET)
    assert claims["shop_id"] == str(shop)
    assert claims["call_id"] == str(call)


def test_tampered_token_rejected():
    tok = mint_call_token(shop_id=uuid4(), call_id=uuid4(), secret=SECRET)
    body, _sig = tok.split(".")
    forged = body + ".AAAA"
    assert verify_call_token(token=forged, secret=SECRET) is None


def test_wrong_secret_rejected():
    tok = mint_call_token(shop_id=uuid4(), call_id=uuid4(), secret=SECRET)
    assert verify_call_token(token=tok, secret="other-secret") is None


def test_bearer_prefix_is_tolerated():
    tok = mint_call_token(shop_id=uuid4(), call_id=uuid4(), secret=SECRET)
    assert verify_call_token(token=f"Bearer {tok}", secret=SECRET) is not None


def test_garbage_token_returns_none():
    assert verify_call_token(token="not-a-token", secret=SECRET) is None
