"""3-layer system-prompt assembler.

Composes the session prompt sent to OpenAI on session.started:
  Layer 3 (safety, immutable) → caller context → Layer 1 (personality)
  → Layer 2 (disclosure) → tool descriptions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.safety_layer import (
    DEFAULT_TOOL_ALLOWLIST,
    SAFETY_PROMPT,
    tool_descriptions,
)


@dataclass
class AssembledPrompt:
    prompt: str
    tools: list[dict[str, Any]]
    voice: str


_TONE_INJECT = {
    "warm": "Tono caloroso, accogliente, mai distante.",
    "professional": "Tono professionale e diretto, senza fronzoli.",
    "casual": "Tono informale e amichevole, come parlare a un amico.",
}


def _caller_context(resolution: ResolutionResult) -> str:
    if resolution.is_anonymous:
        return (
            "Il chiamante ha il numero anonimo. Per prenotare avrai bisogno del "
            "nome e di un numero di telefono pronunciato dal chiamante. "
            "Saluta in modo neutro."
        )
    if resolution.unique_match:
        m = resolution.unique_match
        parts = [f"Il cliente è {m.first_name}" + (f" {m.last_name}" if m.last_name else "") + "."]
        if m.last_visit_at:
            days = (datetime.now(timezone.utc) - m.last_visit_at).days
            parts.append(f"Ultima visita: {days} giorni fa.")
        if m.notes_tags:
            parts.append(f"Note: {', '.join(m.notes_tags)}.")
        parts.append("Saluta per nome e chiedi come puoi aiutarla.")
        return " ".join(parts)
    if len(resolution.matches) > 1:
        names = ", ".join(
            f"{m.first_name} {m.last_name or ''}".strip() for m in resolution.matches
        )
        return (
            f"Il numero è collegato a più clienti: {names}. "
            "Chiedi al chiamante per chi è la prenotazione prima di procedere."
        )
    return (
        "Il chiamante non è ancora un cliente del salone. "
        "Saluta in modo neutro, chiedi il nome e con cosa puoi aiutarlo. "
        "Crea il record cliente solo quando hai un nome confermato."
    )


def assemble_session_prompt(
    *,
    config: dict[str, Any],
    policy: dict[str, Any],
    resolution: ResolutionResult,
    allowlist: list[str] | None = None,
) -> AssembledPrompt:
    allowlist = allowlist or DEFAULT_TOOL_ALLOWLIST
    tone_text = _TONE_INJECT.get(config.get("tone_preset", "warm"), _TONE_INJECT["warm"])

    parts = [
        SAFETY_PROMPT,
        "",
        "CONTESTO CHIAMANTE:",
        _caller_context(resolution),
        "",
        f"SEI L'ASSISTENTE DI: {config.get('display_name', '')}.",
        f"FRASE DI BENVENUTO: \"{config.get('greeting_after_disclosure', '')}\"",
        tone_text,
        "",
        "DISCLOSURE OBBLIGATORIA (dilla all'inizio della conversazione):",
        policy.get("disclosure_text", ""),
    ]

    return AssembledPrompt(
        prompt="\n".join(parts),
        tools=tool_descriptions(allowlist=allowlist),
        voice=config.get("voice_preset", "warm_female"),
    )