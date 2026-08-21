"""The marketing template catalogue.

**The constraint this file exists to encode:** a business-initiated WhatsApp
marketing message can only be a template Meta approved in advance. There is no
free-form path — the "personalised copy" the SMS flow generates end-to-end
cannot be sent on WhatsApp as-is. So personalisation happens *inside*
variables: a fixed, approved skeleton plus a per-customer offer line the LLM
writes.

That is a real product downgrade from SMS and it is deliberate: the alternative
(a template that is essentially one big `{{1}}`) is the single most common
cause of Meta rejecting a template outright, and a rejected template sends
nothing at all.

Variable values must be single-line: Meta rejects parameters containing
newlines, tabs, or 4+ consecutive spaces. `clean_variable` enforces that
rather than letting the send fail at the provider.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Meta's own limit on a template body is 1024 characters; the offer line is
# the only part we don't control, so it gets a budget well under that.
MAX_VARIABLE_CHARS = 500

_WHITESPACE = re.compile(r"[\r\n\t]+|\s{4,}")


@dataclass(frozen=True)
class Template:
    body: str
    variables: int
    sample: dict[str, str]
    category: str = "MARKETING"
    language: str = "it"


CATALOGUE: dict[str, Template] = {
    # {{1}} customer first name, {{2}} salon name, {{3}} the generated offer.
    # The fixed scaffolding around {{3}} is what makes this approvable: Meta
    # can see what the message is for without seeing the variable's value.
    "promo_v1": Template(
        body=(
            "Ciao {{1}}! Un messaggio da {{2}}: {{3}} "
            "Rispondi a questo messaggio o chiamaci per prenotare."
        ),
        variables=3,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "questa settimana taglio e piega a 35€, valido fino a domenica.",
        },
    ),
}


def clean_variable(value: str) -> str:
    """Make one variable value safe to send: single line, bounded length."""
    return _WHITESPACE.sub(" ", value).strip()[:MAX_VARIABLE_CHARS]


def render(template_key: str, variables: dict[str, str]) -> str:
    """Substitute variables into the body — for the stored preview only.

    Twilio does the real substitution from ContentVariables. This exists so a
    row in whatsapp.outbound_messages says what the customer actually read,
    rather than an opaque HX SID.
    """
    body = CATALOGUE[template_key].body
    for key, value in variables.items():
        body = body.replace("{{" + key + "}}", value)
    return body
