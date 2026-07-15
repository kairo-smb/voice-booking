"""Single-shot OpenAI call: classify a finished call AND extract a service brief.

One LLM pass (token-conscious) returns both the call outcome and a structured
'service_brief' written from a hairstylist's standpoint — what the customer
wants, hair history, constraints — so the salon can prepare for the appointment.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import httpx

_VALID_OUTCOMES = {
    "booked", "rescheduled", "cancelled", "info",
    "abandoned", "escalated", "failed",
}

_EMPTY_BRIEF: dict[str, Any] = {
    "services_requested": [],   # [{servizio, note}]
    "desired_result": "",
    "hair_details": {},         # {lunghezza, tipo, colore_attuale, storia_chimica, condizione}
    "constraints": {},          # {allergie_sensibilita, tempo_disponibile, budget, staff_preferito}
    "appointment": {},          # {finestra_richiesta, flessibilita}
    "customer_type": "sconosciuto",  # nuovo | abituale | sconosciuto
    "products_brands_mentioned": [],
    "open_questions": [],
}

_PROMPT = (
    "Analizza la trascrizione di una telefonata gia terminata fra l'assistente "
    "vocale di un PARRUCCHIERE e un cliente. Restituisci SOLO JSON con: "
    "outcome (uno fra booked, rescheduled, cancelled, info, abandoned, "
    "escalated, failed); outcome_reason (frase breve in italiano); summary "
    "(1-2 frasi in italiano); service_brief (oggetto con le informazioni utili "
    "al parrucchiere per prepararsi all'appuntamento). service_brief contiene: "
    "services_requested (lista di {servizio, note}: i servizi richiesti con le "
    "parole del cliente, es. taglio, colore, colpi di sole/balayage, meches, "
    "piega, trattamento, permanente); desired_result (es. 'biondo piu freddo', "
    "'coprire i capelli bianchi', 'spuntare 2 cm'); hair_details {lunghezza, "
    "tipo, colore_attuale, storia_chimica, condizione} dove storia_chimica sono "
    "tinte/decolorazioni/permanenti precedenti; constraints {allergie_"
    "sensibilita, tempo_disponibile, budget, staff_preferito}; appointment "
    "{finestra_richiesta, flessibilita}; customer_type (nuovo|abituale|"
    "sconosciuto); products_brands_mentioned (lista); open_questions (lista di "
    "cose da chiarire in poltrona). Se un'informazione non e presente usa null "
    "o lista vuota, NON inventare. La storia chimica e le allergie/sensibilita "
    "sono critiche perche determinano la fattibilita del colore. Se l'ID di un "
    "appuntamento e fornito, considera l'esito 'booked' salvo smentita."
)


def _normalize_brief(raw: Any) -> dict[str, Any]:
    """Coerce the LLM's service_brief into the expected shape (trust boundary)."""
    brief = copy.deepcopy(_EMPTY_BRIEF)
    if not isinstance(raw, dict):
        return brief
    sr = raw.get("services_requested")
    if isinstance(sr, list):
        brief["services_requested"] = [
            {"servizio": str(s.get("servizio", ""))[:120],
             "note": str(s.get("note", ""))[:300]}
            for s in sr if isinstance(s, dict)
        ]
    for k in ("desired_result", "customer_type"):
        if isinstance(raw.get(k), str):
            brief[k] = raw[k][:300]
    for k in ("hair_details", "constraints", "appointment"):
        if isinstance(raw.get(k), dict):
            brief[k] = raw[k]
    for k in ("products_brands_mentioned", "open_questions"):
        if isinstance(raw.get(k), list):
            brief[k] = [str(x)[:200] for x in raw[k]]
    return brief


def _failed(reason: str) -> dict[str, Any]:
    return {"outcome": "failed", "outcome_reason": reason, "summary": "",
            "service_brief": copy.deepcopy(_EMPTY_BRIEF)}


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    outcome = raw.get("outcome", "failed")
    brief = _normalize_brief(raw.get("service_brief"))
    if outcome not in _VALID_OUTCOMES:
        return {"outcome": "failed", "outcome_reason": "classification_invalid",
                "summary": str(raw)[:500], "service_brief": brief}
    return {
        "outcome": outcome,
        "outcome_reason": str(raw.get("outcome_reason", ""))[:500],
        "summary": str(raw.get("summary", ""))[:500],
        "service_brief": brief,
    }


async def classify_call(
    *,
    api_key: str,
    model: str,
    transcript: list[dict[str, str]],
    booked_appointment_id: str | None,
) -> dict[str, str]:
    """Returns dict with keys outcome, outcome_reason, summary."""
    transcript_text = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    if booked_appointment_id:
        transcript_text += f"\n\n(Appuntamento creato: {booked_appointment_id})"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": transcript_text or "(nessuna trascrizione disponibile)"},
        ],
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code != 200:
            return _failed(f"classifier_http_{resp.status_code}")
        data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
        parsed = json.loads(text)
    except Exception:
        return _failed("classification_invalid")
    return _normalize(parsed)
