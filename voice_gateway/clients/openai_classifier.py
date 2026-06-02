"""Single-shot OpenAI Responses API call to classify a finished phone call."""
from __future__ import annotations

import json
from typing import Any

import httpx

_VALID_OUTCOMES = {
    "booked", "rescheduled", "cancelled", "info",
    "abandoned", "escalated", "failed",
}

_PROMPT = (
    "Sei un classificatore. Ricevi la trascrizione di una telefonata gia "
    "terminata fra un assistente vocale di un salone/ristorante/officina e "
    "un cliente. Restituisci SOLO JSON con tre campi: "
    "outcome (uno fra booked, rescheduled, cancelled, info, abandoned, "
    "escalated, failed), outcome_reason (frase breve in italiano), summary "
    "(1-2 frasi in italiano). Se l ID di un appuntamento e fornito, "
    "considera l esito 'booked' a meno che la trascrizione lo contraddica."
)


def _normalize(raw: dict[str, Any]) -> dict[str, str]:
    outcome = raw.get("outcome", "failed")
    if outcome not in _VALID_OUTCOMES:
        return {
            "outcome": "failed",
            "outcome_reason": "classification_invalid",
            "summary": str(raw)[:500],
        }
    return {
        "outcome": outcome,
        "outcome_reason": str(raw.get("outcome_reason", ""))[:500],
        "summary": str(raw.get("summary", ""))[:500],
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
            return {"outcome": "failed",
                    "outcome_reason": f"classifier_http_{resp.status_code}",
                    "summary": ""}
        data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
        parsed = json.loads(text)
    except Exception:
        return {"outcome": "failed", "outcome_reason": "classification_invalid", "summary": ""}
    return _normalize(parsed)
