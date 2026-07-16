"""Compact signed token carrying call context to the MCP server.

OpenAI passes the accept payload's `authorization` value to our MCP server as a
bearer token. We mint one per call so the MCP tools know which shop/call they
run for, without a lookup. HMAC-signed (stdlib), not encrypted — it carries only
ids, no secrets.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from uuid import UUID


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def mint_call_token(*, shop_id: UUID, call_id: UUID, secret: str) -> str:
    body = _b64(json.dumps(
        {"shop_id": str(shop_id), "call_id": str(call_id)}
    ).encode())
    return f"{body}.{_sign(body, secret)}"


def verify_call_token(*, token: str, secret: str) -> dict | None:
    """Return the claims dict if the signature is valid, else None."""
    if not token:
        return None
    token = token.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body, sig = parts
    if not hmac.compare_digest(sig, _sign(body, secret)):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, json.JSONDecodeError):
        return None
