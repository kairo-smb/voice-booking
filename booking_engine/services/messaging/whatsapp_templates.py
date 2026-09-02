"""The marketing template catalogue.

**The constraint this file exists to encode:** a business-initiated WhatsApp
marketing message can only be a template Meta approved in advance. There is no
free-form path — the "personalised copy" the SMS flow generates end-to-end
cannot be sent on WhatsApp as-is. So personalisation happens *inside*
variables: a fixed, approved skeleton plus a per-customer line the LLM writes
— an offer or proposal, or for promo_v1 a gentle observation tuned to the
customer's last visit.

That is a real product downgrade from SMS and it is deliberate: the alternative
(a template that is essentially one big `{{1}}`) is the single most common
cause of Meta rejecting a template outright, and a rejected template sends
nothing at all.

Variable values must be single-line: Meta rejects parameters containing
newlines, tabs, or 4+ consecutive spaces. `clean_variable` enforces that
rather than letting the send fail at the provider.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Meta's own limit on a template body is 1024 characters; the offer line is
# the only part we don't control, so it gets a budget well under that.
MAX_VARIABLE_CHARS = 500

_WHITESPACE = re.compile(r"[\r\n\t]+|\s{4,}")

# The locales whose copy actually exists. Italian only today — this is the
# whole reason template names carry the locale (`it_promo_v1`): a second
# language is a second set of bodies approved under its own names on the same
# WABA, not an edit of these. Adding one means writing the copy, adding it
# here, and pushing it; nothing else in the flow has to change.
SUPPORTED_LANGUAGES = ("it",)
DEFAULT_LANGUAGE = "it"


def resolve_language(language: str | None) -> str:
    """The locale we can actually send in, for a shop that asked for `language`.

    A shop set to a locale we have no copy for gets Italian rather than a name
    (`en_promo_v1`) that exists on no WABA — which would surface as every
    template `missing`, for a reason nothing on screen could explain.
    """
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


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
    # Who writes the generated slot. 'llm' for templates whose variable is
    # composed by the model; 'owner' when the shop types it verbatim (the
    # Campagna Promo tile); None when there is no such slot at all, which is
    # what makes a template UTILITY. `generated_slot` says WHICH variable is
    # filled; this says BY WHOM, and the two are no longer the same question.
    filled_by: str | None = None
    max_chars: int = 90
    # Passed to the model as-is. Never sent to Meta, so both can be retuned
    # freely after a template is approved.
    intent: str = ""
    guidance: str = ""


CATALOGUE: dict[str, Template] = {
    # ── MARKETING ────────────────────────────────────────────────────────────
    # Exactly one generated slot each, always the last variable. Everything
    # before it is a fact: name, the stylist of the last visit, the salon, and
    # a lookup from the visit record. Every body closes with the same soft CTA
    # («Se ti va, scrivimi pure.») — a hard-sell imperative reads worse and is
    # harder to get approved; an open invitation is enough, the observation
    # alone invites a reply.
    #
    # The message is signed by the stylist, not the salon: «sono {{2}} di {{3}}»
    # reads like the person who served them writing, not a mailing. {{2}} is the
    # primary staff of the customer's last visit (first if several).
    "promo_v1": Template(
        body=(
            "Ciao {{1}}, sono {{2}} di {{3}}. A proposito della tua ultima visita: "
            "{{4}} Se ti va, scrivimi pure."
        ),
        variables=4,
        sample={
            "1": "Giulia",
            "2": "Chiara",
            "3": "Salone Bellezza",
            "4": "sono passate circa tre settimane dal tuo colore, com'è la ricrescita?",
        },
        generated_slot=4,
        filled_by="llm",
        max_chars=200,
        intent="promo",
        guidance=(
            "Scrivi solo l'osservazione che segue i due punti: una frase di check-in "
            "calda, ancorata a un servizio che il cliente ha fatto davvero nell'ultima "
            "visita e al tempo passato. Mai venditivo: niente prezzi, niente urgenza, "
            "niente «approfitta», nessun invito a prenotare o rispondere (la chiusura "
            "è già nel testo fisso). Al massimo una domanda naturale, la ricrescita "
            "per un colore, la forma per un taglio. Inizia in minuscolo (dopo i due "
            "punti). Se il contesto non mostra un servizio recente, scrivi un "
            "check-in generico: non inventare servizi."
        ),
    ),
    "winback_v1": Template(
        body=(
            "Ciao {{1}}, sono {{2}} di {{3}}. Pensavo a te: non ci vediamo da {{4}}, "
            "quindi volevo proporti {{5}}. Se ti va, scrivimi pure."
        ),
        variables=5,
        sample={
            "1": "Giulia",
            "2": "Chiara",
            "3": "Salone Bellezza",
            "4": "tre mesi",
            "5": "un ritocco colore con piega a 45€",
        },
        generated_slot=5,
        filled_by="llm",
        intent="winback",
        guidance=(
            "Scrivi solo il complemento oggetto di «volevo proporti»: un sintagma "
            "nominale con articolo (servizio ed eventuale prezzo), minuscolo, senza "
            "punto finale. L'assenza è già nel testo fisso: non ripeterla. Nessun "
            "invito a prenotare o rispondere: la chiusura è già nel testo fisso."
        ),
    ),
    "rebook_v1": Template(
        body=(
            "Ciao {{1}}, sono {{2}} di {{3}}. Di solito passi da noi ogni {{4}}, "
            "quindi potrebbe essere il momento giusto per {{5}}. "
            "Se ti va, scrivimi pure."
        ),
        variables=5,
        sample={
            "1": "Giulia",
            "2": "Chiara",
            "3": "Salone Bellezza",
            "4": "sei settimane",
            "5": "un taglio e piega",
        },
        generated_slot=5,
        filled_by="llm",
        intent="rebook",
        guidance=(
            "Il cliente è regolare: tono di continuità, non di recupero. Scrivi il "
            "complemento oggetto di «il momento giusto per»: sintagma nominale con "
            "articolo, SOLO servizi — mai importi, mai prezzi, mai sconti. Minuscolo, "
            "senza punteggiatura finale. Nessun invito a prenotare o rispondere: la "
            "chiusura è già nel testo fisso."
        ),
    ),

    # Owner-written, not model-written: the Campagna Promo tile takes a line
    # the shop types and sends it verbatim. A separate template rather than a
    # mode of promo_v1 because Meta approves bodies, and owner-written copy
    # reads as an announcement where promo_v1 expects a model observation.
    "promo_manual_v1": Template(
        body=(
            "Ciao {{1}}, ti scriviamo da {{2}} con una novità: {{3}}. "
            "Rispondi a questo messaggio o chiamaci per prenotare."
        ),
        variables=3,
        sample={
            "1": "Giulia",
            "2": "Salone Bellezza",
            "3": "da lunedì trovi la nuova linea di trattamenti ristrutturanti",
        },
        generated_slot=3,
        filled_by="owner",
        max_chars=300,
        intent="promo_manual",
        guidance="",
    ),

    # ── UTILITY ──────────────────────────────────────────────────────────────
    # NOTHING GENERATED AND NOTHING PERSUASIVE. That is the whole reason these
    # cost €0.0341 instead of €0.0691, need no marketing consent and are exempt
    # from the recipient cooldown. Adding so much as "e approfitta del 10%"
    # makes Meta recategorise the template as MARKETING — not a rejection, a
    # silent doubling of the economics of the highest-volume messages we send.
    # Enforced by test_utility_templates_stay_utility.
    # The review request: the owner picks where to ask ({{5}}) and may attach
    # a link ({{6}}). Still facts only — a platform name and a URL are facts,
    # so the UTILITY economics hold.
    #
    # `feedback_v1` (same body, no review ask) was dropped from the catalogue
    # on 2026-09-01 rather than edited: Meta locks an approved body, so new copy
    # is always a new key. It was safe to delete outright because no sender had
    # ever received it — nothing to retire downstream, and the catalogue is the
    # push list, not a history of what we once sent.
    # The salon's name and the service list came out on 2026-09-01. Coexistence
    # means this arrives from the salon's own number under the salon's own
    # display name — naming it again in the body is what made a two-line message
    # read like a mailshot. The visit date stays: it is the anchor to the
    # customer's own transaction, and that anchor is what keeps Meta reading
    # these as UTILITY rather than recategorising them.
    "feedback_v2": Template(
        body=(
            "Ciao {{1}}, grazie per la tua visita del {{2}}! Se ti va, "
            "lascia una recensione su {{3}}: per noi conta molto."
        ),
        variables=3,
        sample={
            "1": "Giulia",
            "2": "12 marzo",
            "3": "Google",
        },
        category="UTILITY",
        generated_slot=None,
    ),
    # `_v6` because that is the copy submitted and approved on Kairo's WABA —
    # the key tracks Meta's name, never the other way round. Meta locks an
    # approved body, so the version in this dict has to be the version Meta
    # holds or the send addresses a template that does not exist.
    #
    # The salon name is back as {{3}}: it turns out a reminder arriving from a
    # number the customer may not have saved reads as "who is waiting for me?"
    # without it. It is still a fact from the shop row, so still UTILITY.
    "reminder_v6": Template(
        body=(
            "Ciao {{1}}, ti aspettiamo {{2}} da {{3}}. Se non puoi venire, "
            "rispondi a questo messaggio e lo spostiamo."
        ),
        variables=3,
        sample={
            "1": "Giulia",
            "2": "giovedì 14 alle 10:30",
            "3": "Salone Bellezza",
        },
        category="UTILITY",
        generated_slot=None,
    ),
}


# ── DOCUMENT-header templates ────────────────────────────────────────────────
# Deliberately NOT CATALOGUE entries: the payload is an attachment, not a body
# with variables, so they are created with `create_document_template` and need a
# publicly-hosted sample file for Meta's review (`META_RECEIPT_SAMPLE_URL`) —
# neither of which the catalogue path knows about.
#
# The name is Meta's pre-built preset (`purchase_receipt_1`, Utility › Payments
# › receipt attachment), used **verbatim** rather than language-prefixed like
# the marketing keys — hence `name` on the dataclass instead of composing it
# from the key. They still push from here rather than being hand-made in
# WhatsApp Manager: `kairo_waba.py push-templates` reconciles them alongside the
# catalogue, so the body lives in one place.
@dataclass(frozen=True)
class DocumentTemplate:
    name: str
    body: str
    language: str = "it"
    category: str = "UTILITY"


DOCUMENT_TEMPLATES: dict[str, DocumentTemplate] = {
    "purchase_receipt_1": DocumentTemplate(
        name="purchase_receipt_1",
        body="In allegato trovi la ricevuta del tuo pagamento. Grazie e a presto!",
    ),
}

RECEIPT_TEMPLATE_KEY = "purchase_receipt_1"
_RECEIPT = DOCUMENT_TEMPLATES[RECEIPT_TEMPLATE_KEY]
RECEIPT_TEMPLATE_NAME = _RECEIPT.name
RECEIPT_TEMPLATE_LANGUAGE = _RECEIPT.language
RECEIPT_TEMPLATE_BODY = _RECEIPT.body


def body_hash(body: str) -> str:
    """Fingerprint of one approved body — how copy drift is detected.

    `whatsapp.templates.body_hash` records which version of the copy a salon's
    WABA actually holds. Without it, editing a body in this file changed nothing
    downstream: `ensure_templates` skips any key it already has a row for, so
    every connected salon kept sending the old text forever, with the status
    column cheerfully reading `approved`.
    """
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def catalogue_fingerprints() -> list[str]:
    """`key|hash` per catalogue entry — the worklist key for the hourly sweep.

    One array covers both questions the sweep has to ask ("is a template
    missing?" and "is one stale?") in a single count, and being keyed on the
    catalogue is also what stops non-catalogue rows (the receipt) from padding
    that count until a shop missing a real template looks complete.
    """
    return [f"{key}|{body_hash(tpl.body)}" for key, tpl in CATALOGUE.items()]


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
