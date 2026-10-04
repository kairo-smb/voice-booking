"""The salon's persona for a voice call.

Caller context → who the agent speaks for, the greeting, the opening → tone.
This is shop configuration (voice preset, tone, greeting, overflow text), and
it is all this repo still puts into a call's instructions. The agent rules and
the tools come from marketing-engine's customer agents and are appended after
this at accept time (`services/realtime_session.py`, AGENTS.md 2026-09-28).

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

logger = logging.getLogger(__name__)


@dataclass
class AssembledPrompt:
    prompt: str
    voice: str


DEFAULT_TONE_INSTRUCTION = (
    "Usa un linguaggio chiaro e professionale. Sii utile e diretto."
)


def _default_overflow_greeting(display_name: str) -> str:
    who = display_name or "il salone"
    return f"Salve, sono l'assistente di {who}. Come posso aiutarla?"


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
        # No tool takes a phone number: the session's caller number is the
        # only identity writes are authorized on, and a hidden number has none.
        return (
            "Il chiamante ha il numero nascosto: non puoi ritrovare i suoi "
            "appuntamenti né prenotarne di nuovi. Saluta in modo neutro; se "
            "chiede di prenotare, spostare o annullare, raccogli nome e "
            "richiesta e usa escalate_to_owner."
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
) -> AssembledPrompt:
    tone_text = await _resolve_tone_instruction(config.get("tone_id"))

    parts = [
        "CONTESTO CHIAMANTE:",
        _caller_context(resolution),
        "",
        f"SEI L'ASSISTENTE DI: {config.get('display_name', '')}.",
        f"FRASE DI BENVENUTO: \"{_greeting(config)}\"",
        "APERTURA CHIAMATA: il tuo primo turno è la FRASE DI BENVENUTO, detta "
        "così com'è, poi chiedi come puoi aiutare. Vai dritto al punto: niente "
        "menzioni di registrazioni, trattamento dati o consensi.",
        tone_text,
    ]

    return AssembledPrompt(
        prompt="\n".join(parts),
        voice=config.get("voice_preset", "verse"),
    )
