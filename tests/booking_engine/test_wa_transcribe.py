"""Voice-note transcription: charged before the work, refused on an empty basket.

Two properties are load-bearing here and both are asserted rather than inferred:

1. A refused charge (402) means the provider is **never called**. Paying nothing
   is the whole point of the gate; calling OpenAI anyway and then not charging
   is the bug it exists to prevent.
2. Nothing escapes `transcribe`. It runs in a fire-and-forget background task,
   where an exception is a silently lost customer message.

Every failure — no credit, no key, provider error, empty result — is `None`,
never `""`. A later task stores the return value in a `transcript` column that
is NULL by default: NULL means "we do not know", `""` would mean "we
transcribed it and it said nothing", which is a different and wrong claim.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from booking_engine.clients import webapp_credits
from booking_engine.services.messaging import wa_transcribe as stt

SHOP = uuid4()
URL = "https://api.openai.com/v1/audio/transcriptions"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """A key in the environment, and any unmocked request is an error.

    Without the respx context an implementation that wrongly calls the provider
    on a refused charge would reach the real api.openai.com instead of failing
    the test.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with respx.mock:
        yield


@pytest.fixture
def fake_openai(_no_real_network):
    return respx.post(URL).mock(
        # Deliberately padded: the transcript must come back stripped.
        return_value=httpx.Response(200, text="  vorrei prenotare \n"),
    )


@pytest.fixture
def fake_openai_error(_no_real_network, fake_credits):
    return respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))


class _Credits:
    def __init__(self, ok: bool):
        self.ok = ok
        self.calls = 0
        self.charged: dict | None = None

    async def __call__(self, *, shop_id, run_type, run_ref, credits, settings):
        self.calls += 1
        self.charged = {
            "shop_id": shop_id, "run_type": run_type,
            "run_ref": run_ref, "credits": credits,
        }
        return self.ok


@pytest.fixture
def fake_credits(monkeypatch):
    spy = _Credits(ok=True)
    monkeypatch.setattr(stt.webapp_credits, "charge_actual", spy)
    return spy


@pytest.fixture
def fake_credits_402(monkeypatch):
    spy = _Credits(ok=False)
    monkeypatch.setattr(stt.webapp_credits, "charge_actual", spy)
    return spy


@pytest.mark.asyncio
async def test_transcribes_and_charges_the_basket(fake_openai, fake_credits):
    out = await stt.transcribe(shop_id=SHOP, audio=b"oggvorbis", run_ref="wamid.A")

    assert out == "vorrei prenotare"
    assert fake_credits.charged["run_type"] == "whatsapp_transcribe"
    assert fake_credits.charged["run_ref"] == "wamid.A"


@pytest.mark.asyncio
async def test_an_empty_basket_means_no_transcription_and_no_charge(fake_credits_402):
    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None


@pytest.mark.asyncio
async def test_a_provider_failure_returns_none_rather_than_raising(fake_openai_error):
    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None


@pytest.mark.asyncio
async def test_a_refused_charge_never_calls_the_provider(fake_openai, fake_credits_402):
    # Asserted, not inferred: the gate is there so we pay the provider nothing
    # for a shop that can pay us nothing.
    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None
    assert len(fake_openai.calls) == 0


@pytest.mark.asyncio
async def test_the_run_ref_is_the_meta_message_id(fake_openai, fake_credits):
    await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="wamid.HBgNMzkz")

    assert fake_credits.charged["run_ref"] == "wamid.HBgNMzkz"
    assert fake_credits.charged["shop_id"] == SHOP
    assert fake_credits.charged["credits"] == stt.TRANSCRIBE_CREDITS


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
async def test_an_empty_transcript_is_none_not_an_empty_string(
    _no_real_network, fake_credits, body,
):
    respx.post(URL).mock(return_value=httpx.Response(200, text=body))

    # "" would be stored as "we transcribed it and it said nothing" — a
    # different and wrong claim from NULL's "we do not know".
    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None


@pytest.mark.asyncio
async def test_a_transcript_is_stripped(_no_real_network, fake_credits):
    respx.post(URL).mock(
        return_value=httpx.Response(200, text="\n  il colore come l'altra volta  \n"),
    )

    out = await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w")
    assert out == "il colore come l'altra volta"


@pytest.mark.asyncio
async def test_no_exception_escapes_when_the_charge_client_raises(monkeypatch):
    async def _boom(**_kwargs):
        raise RuntimeError("basket exploded")

    monkeypatch.setattr(stt.webapp_credits, "charge_actual", _boom)

    # Fire-and-forget caller: an exception here is a silently lost message.
    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None


@pytest.mark.asyncio
async def test_no_exception_escapes_when_the_provider_is_unreachable(
    _no_real_network, fake_credits,
):
    respx.post(URL).mock(side_effect=httpx.ConnectError("connection refused"))

    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None


@pytest.mark.asyncio
async def test_no_key_means_no_charge_and_no_call(monkeypatch, fake_openai, fake_credits):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert await stt.transcribe(shop_id=SHOP, audio=b"x", run_ref="w") is None
    # Fails closed *before* spending the salon's credit on work we cannot do.
    assert fake_credits.calls == 0
    assert len(fake_openai.calls) == 0


@pytest.mark.asyncio
async def test_the_wire_contract_sent_to_openai(fake_openai, fake_credits):
    await stt.transcribe(shop_id=SHOP, audio=b"oggvorbis", run_ref="w")

    request = fake_openai.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = request.content
    assert b'name="model"' in body
    assert stt.MODEL.encode() in body
    # WhatsApp voice notes are audio/ogg (opus); the extension is how OpenAI
    # decides how to decode the upload.
    assert b'filename="voice.ogg"' in body
    assert b"oggvorbis" in body


@pytest.mark.asyncio
async def test_nothing_logs_the_audio_bytes(fake_openai, fake_credits, caplog):
    caplog.set_level("DEBUG")
    await stt.transcribe(shop_id=SHOP, audio=b"SECRETAUDIOPAYLOAD", run_ref="w")

    assert "SECRETAUDIOPAYLOAD" not in caplog.text
    assert "sk-test" not in caplog.text


def test_whatsapp_transcribe_is_on_the_shared_charge_client():
    # One charge client, one run-type vocabulary — not a second copy here.
    assert webapp_credits.WHATSAPP_TRANSCRIBE == "whatsapp_transcribe"
