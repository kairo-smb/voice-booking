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
    # Which {{n}} the LLM writes. None means nothing is generated — the mark of
    # a UTILITY template, and the reason it stays UTILITY. Every other variable
    # is a fact the webapp already holds, which is both cheaper and unable to
    # hallucinate a service the customer never had.
    generated_slot: int | None = None
    max_chars: int = 90
    # Passed to the model as-is. Never sent to Meta, so both can be retuned
    # freely after a template is approved.
    intent: str = ""
    guidance: str = ""


CATALOGUE: dict[str, Template] = {
    # ── MARKETING ────────────────────────────────────────────────────────────
    # Exactly one generated slot each, always the last variable. Everything
    # before it is a fact: name, salon, and a lookup from the visit record.
    "promo_v1": Template(
        body=(
            "Ciao {{1}}, ti scriviamo da {{2}} per proporti {{3}}. "
            "Rispondi a questo messaggio o chiamaci per prenotare."
        ),
        variables=3,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "un taglio con piega a 35€ questa settimana",
        },
        generated_slot=3,
        intent="promo",
        guidance=(
            "Una proposta concreta, basata su un servizio che il cliente fa già "
            "o su uno complementare. Nessun saluto e nessun invito a prenotare: "
            "ci sono già nel testo fisso. Frammento che completa «per proporti», "
            "minuscolo, senza punto finale."
        ),
    ),
    "winback_v1": Template(
        body=(
            "Ciao {{1}}, ti scriviamo da {{2}}: non ci vediamo da {{3}} "
            "e per questo ti proponiamo {{4}}. "
            "Rispondi a questo messaggio per prenotare."
        ),
        variables=4,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "tre mesi",
            "4": "un ritocco colore con piega a 45€",
        },
        generated_slot=4,
        intent="winback",
        guidance=(
            "Scrivi solo il complemento oggetto di «ti proponiamo»: un sintagma "
            "nominale con articolo (servizio ed eventuale prezzo), minuscolo, senza "
            "punto finale. L'assenza è già nel testo fisso: non ripeterla. Nessun "
            "invito a prenotare o rispondere: sono già nel testo fisso."
        ),
    ),
    "rebook_v1": Template(
        body=(
            "Ciao {{1}}, ti scriviamo da {{2}}. Di solito passi da noi ogni {{3}}, "
            "quindi potrebbe essere il momento giusto per {{4}}. "
            "Rispondi a questo messaggio per prenotare."
        ),
        variables=4,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "sei settimane",
            "4": "un taglio e piega a 35€",
        },
        generated_slot=4,
        intent="rebook",
        guidance=(
            "Il cliente è regolare: tono di continuità, non di recupero. Frammento "
            "che completa «il momento giusto per», minuscolo, senza punteggiatura "
            "finale. Nessun invito a prenotare o rispondere: sono già nel testo fisso."
        ),
    ),

    # ── UTILITY ──────────────────────────────────────────────────────────────
    # NOTHING GENERATED AND NOTHING PERSUASIVE. That is the whole reason these
    # cost €0.0341 instead of €0.0691, need no marketing consent and are exempt
    # from the recipient cooldown. Adding so much as "e approfitta del 10%"
    # makes Meta recategorise the template as MARKETING — not a rejection, a
    # silent doubling of the economics of the highest-volume messages we send.
    # Enforced by test_utility_templates_stay_utility.
    "feedback_v1": Template(
        body=(
            "Ciao {{1}}, grazie per la tua visita da {{2}} il {{3}} per {{4}}. "
            "Com'è andata? Rispondi a questo messaggio per dirci la tua."
        ),
        variables=4,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "12 marzo",
            "4": "colore e piega",
        },
        category="UTILITY",
        generated_slot=None,
    ),
    "reminder_v1": Template(
        body=(
            "Ciao {{1}}, ti ricordiamo il tuo appuntamento da {{2}} in programma {{3}} "
            "per {{4}}. Se non puoi venire, rispondi a questo messaggio per spostarlo."
        ),
        variables=4,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "giovedì 14 alle 10:30",
            "4": "taglio e piega",
        },
        category="UTILITY",
        generated_slot=None,
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
