from booking_engine.observability import redact_credentials


def test_credentials_are_redacted_by_value_everything_else_kept():
    event = redact_credentials({
        "request": {"data": {"transcript": "Giulia 3331234567"}},
        "vars": {"scope": [["b'authorization'", "b'Bearer abc.def-123'"]], "meta_token": "EAAG" + "x" * 30},
    })

    assert event["request"]["data"]["transcript"] == "Giulia 3331234567"
    assert event["vars"]["scope"][0][1] == "b'[Filtered]'"
    assert event["vars"]["meta_token"] == "[Filtered]"
