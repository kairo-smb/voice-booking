import hashlib
import hmac

from booking_engine.services.meta_signature import meta_signature_valid

SECRET = "app-secret"
BODY = b'{"entry":[{"id":"WABA1"}]}'


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_genuine_signature_passes():
    assert meta_signature_valid(BODY, _sign(BODY), SECRET)


def test_a_tampered_body_fails():
    """Meta signs the raw bytes, so one changed character must not verify."""
    assert not meta_signature_valid(b'{"entry":[{"id":"WABA2"}]}', _sign(BODY), SECRET)


def test_a_signature_from_another_secret_fails():
    assert not meta_signature_valid(BODY, _sign(BODY, "someone-else"), SECRET)


def test_a_missing_or_malformed_header_fails():
    assert not meta_signature_valid(BODY, None, SECRET)
    assert not meta_signature_valid(BODY, "", SECRET)
    assert not meta_signature_valid(BODY, "sha1=deadbeef", SECRET)


def test_an_unset_app_secret_is_a_no_op():
    """Same known gap as twilio_signature_valid: unconfigured means permissive."""
    assert meta_signature_valid(BODY, None, "")
