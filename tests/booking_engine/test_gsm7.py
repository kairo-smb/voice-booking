from booking_engine.services.messaging.gsm7 import sanitize, encode_info


def test_italian_accents_are_gsm7_and_free():
    # à è é ì ò ù are in the GSM 03.38 alphabet — no UCS-2 penalty.
    info = encode_info("Ciao Giulia, è passato un po'. Ti va un caffè però?")
    assert info.encoding == "gsm7"
    assert info.segments == 1


def test_curly_quote_is_transliterated_not_upgraded():
    # The LLM writes ’ ; left alone it would force UCS-2 and halve the segment.
    raw = "Ciao Giulia, com’è andata?"
    assert sanitize(raw) == "Ciao Giulia, com'è andata?"
    assert encode_info(sanitize(raw)).encoding == "gsm7"


def test_uppercase_e_grave_becomes_apostrophe_form():
    # È is NOT in GSM-7 (only É is). E' is the standard Italian typewriter form.
    assert sanitize("È ora!") == "E' ora!"


def test_emoji_forces_ucs2_and_is_not_stripped():
    # Content is never silently removed — the caller sees the real cost instead.
    info = encode_info(sanitize("Ciao Giulia 💇"))
    assert info.encoding == "ucs2"
    assert "💇" in info.text


def test_gsm7_segment_boundaries():
    assert encode_info("a" * 160).segments == 1
    # Over 160, concatenation headers cut each segment to 153.
    assert encode_info("a" * 161).segments == 2
    assert encode_info("a" * 306).segments == 2
    assert encode_info("a" * 307).segments == 3


def test_ucs2_segment_boundaries():
    assert encode_info("💇" * 35).segments == 1   # 70 UTF-16 units
    assert encode_info("💇" * 36).segments == 2   # 72 > 70


def test_extended_chars_count_double():
    # € { } [ ] ~ ^ | live in the GSM extension table: 2 septets each.
    info = encode_info("€" * 80)
    assert info.encoding == "gsm7"
    assert info.segments == 1     # 80 × 2 = 160 septets = exactly one segment
    assert encode_info("€" * 81).segments == 2
