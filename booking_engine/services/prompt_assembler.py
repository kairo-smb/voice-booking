"""3-layer system-prompt assembler.

Composes the session prompt sent to OpenAI on session.started:
  Layer 3 (safety, immutable) → caller context → Layer 1 (personality,
  display, tone) → Layer 2 (disclosure) → tool descriptions.

The tone instruction is fetched from voice_agent.voice_tones via tone_id;
unknown / missing / lookup failures fall back to DEFAULT_TONE_INSTRUCTION.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from booking_engine.db.voice_tone_queries import get_tone_by_id
from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.safety_layer import (
    DEFAULT_TOOL_ALLOWLIST,
    SAFETY_PROMPT,
    tool_descriptions,
)

logger = logging.getLogger(__name__)


@dataclass
class AssembledPrompt:
    prompt: str
    tools: list[dict[str, Any]]
    voice: str


DEFAULT_TONE_INSTRUCTION = (
    "Usa un linguaggio chiaro e professionale. Sii utile e diretto."
)


def _default_overflow_greeting(display_name: str) -> str:
    who = display_name or "il salone"
    return (
        f"Salve, sono l'assistente di {who}. In questo momento non possiamo "
        "rispondere di persona, ma posso aiutarla io con la prenotazione."
    )


def _greeting(config: dict[str, Any]) -> str:
    """Overflow shops greet as a stand-in for busy staff; full shops greet cold.

    Both greetings are shop-authored (webapp); we fall back to a sensible
    default only for overflow when the shop hasn't written one yet.
    """
    if config.get("answer_mode") == "overflow":
        return config.get("greeting_overflow") or _default_overflow_greeting(
            config.get("display_name", "")
        )
    return config.get("greeting_after_disclosure", "")


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


async def _resolve_tone_instruction(tone_id: UUID | None) -> str:
    if tone_id is None:
        return DEFAULT_TONE_INSTRUCTION
    try:
        tone = await get_tone_by_id(tone_id)
    except Exception:
        logger.exception("voice_tones lookup failed for %s; using default", tone_id)
        return DEFAULT_TONE_INSTRUCTION
    if tone is None:
        return DEFAULT_TONE_INSTRUCTION
    return tone["system_prompt_instruction"]


async def assemble_session_prompt(
    *,
    config: dict[str, Any],
    policy: dict[str, Any],
    resolution: ResolutionResult,
    allowlist: list[str] | None = None,
) -> AssembledPrompt:
    allowlist = allowlist or DEFAULT_TOOL_ALLOWLIST
    tone_text = await _resolve_tone_instruction(config.get("tone_id"))

    parts = [
        SAFETY_PROMPT,
        "",
        "CONTESTO CHIAMANTE:",
        _caller_context(resolution),
        "",
        f"SEI L'ASSISTENTE DI: {config.get('display_name', '')}.",
        f"FRASE DI BENVENUTO: \"{_greeting(config)}\"",
        "APERTURA CHIAMATA: al primo turno usa subito la FRASE DI BENVENUTO per "
        "presentarti, poi chiedi come puoi aiutare. Vai dritto al punto: niente "
        "menzioni di registrazioni, trattamento dati o consensi.",
        tone_text,
    ]

    return AssembledPrompt(
        prompt="\n".join(parts),
        tools=tool_descriptions(allowlist=allowlist),
        voice=config.get("voice_preset", "verse"),
    )
