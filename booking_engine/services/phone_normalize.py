"""Phone number normalization utilities.

Wraps libphonenumber to convert salon-entered phone strings (varying formats)
and Twilio-provided caller IDs into a canonical comparable form.
"""
from __future__ import annotations

import phonenumbers


def normalize_e164(raw: str | None, *, default_region: str = "IT") -> str | None:
    """Return E.164 form (+39...) or None if input is not a valid phone number."""
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def digits_only(raw: str | None) -> str:
    """Return digits-only form for index-based comparison against generated columns."""
    if not raw:
        return ""
    return "".join(c for c in raw if c.isdigit())
