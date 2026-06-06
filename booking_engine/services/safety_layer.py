"""Layer 3 — hardcoded safety rules and tool descriptions.

Merchants cannot view or modify these. They are prepended to every session prompt.
The tool descriptions are JSON schemas OpenAI uses to advertise available tools.
"""
from __future__ import annotations

from typing import Any


SAFETY_PROMPT = """\
REGOLE NON NEGOZIABILI (in italiano):
- Non dare mai consigli medici, diagnostici o farmaceutici. Se il chiamante \
chiede, indirizzalo al medico o al farmacista.
- Non trattare prezzi al di fuori di quelli forniti dagli strumenti. Non \
contrattare sconti non già configurati.
- Non promettere risultati estetici specifici ("ti farò sembrare 10 anni più giovane").
- Se il chiamante chiede di parlare con una persona, richiedi al salone di richiamare — usa escalate_to_merchant e termina educatamente la chiamata.
- Se il chiamante è aggressivo, ripetutamente offensivo o usa linguaggio \
inappropriato, termina cordialmente la chiamata.
- Conferma sempre i dettagli di una prenotazione a voce prima di chiamare lo \
strumento create_booking.
- Prima di modificare o cancellare una prenotazione esistente, devi:
  1. Confermare l'identità del chiamante con UNA domanda verifica (es. orario \
     della prenotazione, servizio prenotato, nome completo).
  2. Passare verification_passed=true SOLO se la risposta è corretta.
- Parla sempre in italiano salvo richiesta esplicita del chiamante.
- Mantieni le risposte concise. Una o due frasi per turno.
"""


DEFAULT_TOOL_ALLOWLIST = [
    "lookup_customer",
    "create_customer_from_call",
    "update_customer_from_call",
    "get_services",
    "get_staff_for_service",
    "check_availability",
    "create_booking",
    "get_booking",
    "modify_booking",
    "cancel_booking",
    "mark_outcome",
    "escalate_to_merchant",
]


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "lookup_customer": {
        "name": "lookup_customer",
        "description": "Trova clienti per numero di telefono normalizzato. Restituisce 0-5 risultati.",
        "parameters": {
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"],
        },
    },
    "create_customer_from_call": {
        "name": "create_customer_from_call",
        "description": "Crea un nuovo cliente dopo aver raccolto nome e telefono.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "phone_source": {"type": "string", "enum": ["caller_id", "stated"]},
            },
            "required": ["phone", "first_name", "phone_source"],
        },
    },
    "update_customer_from_call": {
        "name": "update_customer_from_call",
        "description": "Aggiorna un campo del cliente (last_name, email, notes_tags).",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "field": {"type": "string", "enum": ["last_name", "email", "notes_tags"]},
                "value": {"type": "string"},
            },
            "required": ["customer_id", "field", "value"],
        },
    },
    "get_services": {
        "name": "get_services",
        "description": "Lista dei servizi del salone, opzionalmente filtrati per nome.",
        "parameters": {
            "type": "object",
            "properties": {"filter": {"type": "string"}},
        },
    },
    "get_staff_for_service": {
        "name": "get_staff_for_service",
        "description": "Personale qualificato per un servizio.",
        "parameters": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
        },
    },
    "check_availability": {
        "name": "check_availability",
        "description": "Slot disponibili per un servizio. Restituisce fino a 5 opzioni.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "preferred_when": {"type": "string", "description": "ISO 8601"},
                "staff_id": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["service_id"],
        },
    },
    "create_booking": {
        "name": "create_booking",
        "description": "Crea una prenotazione confermata.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "service_id": {"type": "string"},
                "slot_start": {"type": "string"},
                "staff_id": {"type": "string"},
            },
            "required": ["customer_id", "service_id", "slot_start", "staff_id"],
        },
    },
    "get_booking": {
        "name": "get_booking",
        "description": "Recupera la prossima prenotazione di un cliente, opzionalmente filtrando per data approssimativa.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "fuzzy_when": {"type": "string"},
            },
            "required": ["customer_id"],
        },
    },
    "modify_booking": {
        "name": "modify_booking",
        "description": "Modifica una prenotazione. Richiede verification_passed=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "new_slot_start": {"type": "string"},
                "new_service_id": {"type": "string"},
                "verification_passed": {"type": "boolean"},
            },
            "required": ["appointment_id", "verification_passed"],
        },
    },
    "cancel_booking": {
        "name": "cancel_booking",
        "description": "Cancella una prenotazione. Richiede verification_passed=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "verification_passed": {"type": "boolean"},
            },
            "required": ["appointment_id", "verification_passed"],
        },
    },
    "mark_outcome": {
        "name": "mark_outcome",
        "description": "Marca l'esito della chiamata prima di chiudere.",
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": [
                    "booked", "rescheduled", "cancelled", "info",
                    "abandoned", "escalated", "failed",
                ]},
                "summary": {"type": "string"},
                "callback_window": {"type": "string"},
            },
            "required": ["outcome", "summary"],
        },
    },
    "escalate_to_merchant": {
        "name": "escalate_to_merchant",
        "description": "Crea un memo per il salone che richiama il cliente.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "callback_window": {"type": "string"},
                "customer_message": {"type": "string"},
            },
            "required": ["reason", "customer_message"],
        },
    },
}


def tool_descriptions(*, allowlist: list[str]) -> list[dict[str, Any]]:
    return [_TOOL_SCHEMAS[name] for name in allowlist if name in _TOOL_SCHEMAS]