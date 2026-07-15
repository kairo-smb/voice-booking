"""Map free-text services from the call brief to the shop's real catalog."""
from __future__ import annotations

from uuid import uuid4

from booking_engine.services.service_catalog_match import match_services_to_catalog

TAGLIO, COLORE, COLPI = uuid4(), uuid4(), uuid4()
CATALOG = [
    {"id": TAGLIO, "name": "Taglio Donna"},
    {"id": COLORE, "name": "Colore"},
    {"id": COLPI, "name": "Colpi di sole"},
]


def _match(servizio, note=""):
    return match_services_to_catalog(
        services_requested=[{"servizio": servizio, "note": note}],
        catalog=CATALOG,
    )[0]


def test_exact_name_match():
    m = _match("colore")
    assert m["matched_service_id"] == COLORE
    assert m["matched_name"] == "Colore"


def test_token_match_ignores_filler_words_and_case():
    m = _match("Vorrei un TAGLIO")
    assert m["matched_service_id"] == TAGLIO


def test_multiword_service_matches():
    m = _match("colpi di sole sulle punte")
    assert m["matched_service_id"] == COLPI


def test_unmatched_service_is_flagged_null():
    m = _match("massaggio")
    assert m["matched_service_id"] is None
    assert m["matched_name"] is None


def test_preserves_servizio_and_note():
    m = _match("colore", note="lo vuole più freddo")
    assert m["servizio"] == "colore"
    assert m["note"] == "lo vuole più freddo"


def test_does_not_cross_match_similar_but_distinct():
    # 'colore' must not match 'Colpi di sole'
    m = _match("colore")
    assert m["matched_service_id"] == COLORE
