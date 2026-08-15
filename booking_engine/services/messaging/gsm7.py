"""GSM 03.38 encoding detection and segment counting.

Segment count is the billing unit: an SMS is 160 chars in GSM-7 but only 70 in
UCS-2, so one stray curly quote from the LLM can triple the price of a campaign.
`sanitize` removes typographic noise losslessly; anything left that GSM-7 can't
represent (emoji) is kept and the caller is told the real cost instead.
"""
from __future__ import annotations

from dataclasses import dataclass

# GSM 03.38 default alphabet. Note it already contains the Italian lowercase
# accented vowels (à è é ì ò ù) — those are free, contrary to folklore.
_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Extension table: each costs two septets (ESC + char).
_EXTENDED = "^{}\\[~]|€"

# Lossless replacements for characters an LLM emits that GSM-7 lacks.
# Uppercase accented vowels other than É have no GSM-7 form; Italian typewriter
# convention writes them as letter + apostrophe.
_TRANSLITERATE = {
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ",
    "È": "E'", "À": "A'", "Ì": "I'", "Ò": "O'", "Ù": "U'",
}

GSM7_SINGLE, GSM7_MULTI = 160, 153
UCS2_SINGLE, UCS2_MULTI = 70, 67


@dataclass(frozen=True)
class EncodeInfo:
    text: str
    encoding: str   # 'gsm7' | 'ucs2'
    units: int      # septets (gsm7) or UTF-16 code units (ucs2)
    segments: int


def sanitize(text: str) -> str:
    """Replace non-GSM-7 typography with equivalent GSM-7 characters.

    Lossless by construction: only characters with an accepted plain-text
    equivalent are in the table. Emoji and other true non-GSM content are left
    alone — silently deleting words from a message addressed to a named
    customer is worse than charging for a second segment.
    """
    return "".join(_TRANSLITERATE.get(ch, ch) for ch in text)


def _septets(text: str) -> int | None:
    """Septet count, or None if the text can't be represented in GSM-7."""
    total = 0
    for ch in text:
        if ch in _BASIC:
            total += 1
        elif ch in _EXTENDED:
            total += 2
        else:
            return None
    return total


def encode_info(text: str) -> EncodeInfo:
    """Report the encoding, unit count and billable segment count for `text`."""
    septets = _septets(text)
    if septets is not None:
        single, multi, units, encoding = GSM7_SINGLE, GSM7_MULTI, septets, "gsm7"
    else:
        # UCS-2 bills per UTF-16 code unit, so astral chars (most emoji) cost 2.
        units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
        single, multi, encoding = UCS2_SINGLE, UCS2_MULTI, "ucs2"

    if units <= single:
        segments = 1
    else:
        segments = -(-units // multi)   # ceil
    return EncodeInfo(text=text, encoding=encoding, units=units, segments=segments)
