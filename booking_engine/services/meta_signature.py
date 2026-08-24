"""Verify Meta's X-Hub-Signature-256 header on WhatsApp webhooks.

Unlike Twilio — which signs the URL plus sorted form fields, and with the auth
token of whichever account owns the resource — Meta signs the **raw request
body** with one app secret shared by every customer's traffic. That is the
whole reason the per-subaccount token column disappeared in migration 15.

The body must be the bytes as received: re-serialising the parsed JSON changes
whitespace and key order and the digest no longer matches.
"""
from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def meta_signature_valid(body: bytes, header: str | None, app_secret: str) -> bool:
    """True if `header` is a genuine signature of `body`.

    No-op when `META_APP_SECRET` is unset, matching `twilio_signature_valid`'s
    behaviour and the same known gap: an unconfigured environment accepts
    unsigned webhooks.
    """
    if not app_secret:
        return True
    if not header or not header.startswith(_PREFIX):
        return False
    expected = hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header[len(_PREFIX):])
