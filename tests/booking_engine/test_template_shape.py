"""Meta's structural rules for a template body, as tests.

Violating any of these yields INVALID_FORMAT at submission — and because Meta
blocks reusing a deleted template's name for 30 days, a rejection is expensive
in a way an ordinary bug is not. These run in milliseconds; the alternative is
finding out a day later and losing the name for a month.

Rules verified against Meta's template guidelines, 2026-08-31.
"""
from __future__ import annotations

import re

from booking_engine.services.messaging.whatsapp_templates import CATALOGUE

_VAR = re.compile(r"\{\{(\d+)\}\}")
_LETTER = re.compile(r"[A-Za-zÀ-ÿ]")


def test_no_body_starts_or_ends_with_a_variable():
    """A "dangling parameter" — a variable with no text anchoring it — is
    rejected, because it makes the template a blank container."""
    for key, tpl in CATALOGUE.items():
        body = tpl.body.strip()
        assert not body.startswith("{{"), f"{key} starts with a variable"
        assert not body.endswith("}}"), f"{key} ends with a variable"


def test_variables_are_separated_by_real_words():
    """Back-to-back variables are rejected, and punctuation does not count as
    separation: `{{2}}: {{3}}` reads to Meta as two adjacent parameters."""
    for key, tpl in CATALOGUE.items():
        for gap in re.findall(r"\}\}(.*?)\{\{", tpl.body, re.S):
            assert _LETTER.search(gap), f"{key}: only {gap!r} between two variables"


def test_variables_are_sequential_from_one():
    """Meta requires 1..N with no gaps; deleting a variable means renumbering."""
    for key, tpl in CATALOGUE.items():
        found = sorted({int(n) for n in _VAR.findall(tpl.body)})
        assert found == list(range(1, tpl.variables + 1)), f"{key}: {found}"


def test_enough_fixed_text_around_the_variables():
    """Meta's ratio heuristic: at least 3 words of fixed copy per variable, plus
    one. Below that the template reads as a placeholder container."""
    for key, tpl in CATALOGUE.items():
        fixed = _VAR.sub(" ", tpl.body)
        words = [w for w in fixed.split() if _LETTER.search(w)]
        assert len(words) >= 3 * tpl.variables + 1, f"{key}: {len(words)} words"


def test_every_variable_has_a_sample():
    """Submission fails without an example value for each parameter."""
    for key, tpl in CATALOGUE.items():
        for i in range(1, tpl.variables + 1):
            assert tpl.sample.get(str(i)), f"{key} has no sample for {{{{{i}}}}}"


def test_samples_avoid_characters_meta_rejects_in_parameters():
    """# $ % and runs of whitespace are refused inside a parameter value. The
    samples are the only parameter values we control, so they are the only ones
    that can be checked ahead of time."""
    for key, tpl in CATALOGUE.items():
        for slot, value in tpl.sample.items():
            assert not re.search(r"[#$%\r\n\t]", value), f"{key}.{slot}: {value!r}"
            assert "    " not in value, f"{key}.{slot}: 4+ spaces"


_PROMO_WORDS = re.compile(r"scont|offert|promo|gratis|omagg|saldo|%", re.I)


def test_utility_templates_stay_utility():
    """UTILITY needs BOTH non-promotional intent AND user-specific content.
    Mixed content is MARKETING — and Meta recategorises rather than rejects, so
    the failure is silent: same template, double the price, consent and cooldown
    quietly back in force. This test is the only thing standing between a
    well-meaning copy edit and that outcome."""
    for key, tpl in CATALOGUE.items():
        if tpl.category != "UTILITY":
            continue
        assert tpl.generated_slot is None, f"{key}: UTILITY must not generate"
        assert not _PROMO_WORDS.search(tpl.body), f"{key}: promotional language"
        for slot, value in tpl.sample.items():
            assert not _PROMO_WORDS.search(value), f"{key}.{slot}: promotional sample"


def test_marketing_templates_generate_exactly_one_slot():
    """The invariant the send path, the preview renderer and the prompt all
    rely on: one generated slot, and it is a real variable of that template."""
    for key, tpl in CATALOGUE.items():
        if tpl.category != "MARKETING":
            continue
        assert tpl.generated_slot is not None, f"{key}: MARKETING must generate"
        assert 1 <= tpl.generated_slot <= tpl.variables, f"{key}: slot out of range"
        assert tpl.guidance, f"{key}: no guidance for the model"
