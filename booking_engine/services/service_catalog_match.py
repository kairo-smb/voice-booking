"""Reconcile the call brief's free-text services against the shop's catalog.

The post-call brief records services in the customer's words ("un taglio",
"colpi di sole"). This maps each to a real catalog service_id so the salon sees
canonical names and can spot requests it doesn't offer (matched_service_id=None).

ponytail: token-overlap matcher, good enough for Italian hairdresser terms.
Upgrade path if recall is poor: a per-shop synonym table or an LLM match step.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


def _tokens(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return {t for t in s.split() if len(t) >= 3}  # drop fillers: un, di, il...


def parse_brief(raw: Any) -> dict[str, Any]:
    """jsonb comes back from asyncpg as a str; tolerate str | dict | None."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def enrich_brief(raw: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse a stored brief and annotate its services with catalog matches."""
    brief = parse_brief(raw)
    if brief.get("services_requested"):
        brief["services_requested"] = match_services_to_catalog(
            services_requested=brief["services_requested"], catalog=catalog,
        )
    return brief


def match_services_to_catalog(
    *,
    services_requested: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return each requested service annotated with matched_service_id/name."""
    indexed = [(c, _tokens(c["name"])) for c in catalog]
    out: list[dict[str, Any]] = []
    for req in services_requested:
        req_tokens = _tokens(req.get("servizio", ""))
        best, best_score = None, 0
        for cat, cat_tokens in indexed:
            score = len(req_tokens & cat_tokens)
            if score > best_score:
                best, best_score = cat, score
        out.append({
            "servizio": req.get("servizio", ""),
            "note": req.get("note", ""),
            "matched_service_id": best["id"] if best else None,
            "matched_name": best["name"] if best else None,
        })
    return out
