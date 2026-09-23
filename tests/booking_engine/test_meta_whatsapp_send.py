"""Graph client: free-form text, reply-button menus, and media download.

These three calls are the ones two-way conversation needs on top of the
existing template-only client — see AGENTS.md's WhatsApp entries for why
`send_template`/`send_document_template` were the only send paths until now.
"""
import pytest

from booking_engine.clients import meta_whatsapp as meta


@pytest.mark.asyncio
async def test_send_text_posts_a_text_message(monkeypatch):
    seen = {}

    async def fake_request(method, path, *, token, json_body=None, params=None):
        seen.update(method=method, path=path, body=json_body, token=token)
        return {"messages": [{"id": "wamid.OUT"}]}

    monkeypatch.setattr(meta, "_request", fake_request)

    sid = await meta.send_text(
        phone_number_id="PNID", to="+393331112223", body="Ciao!", token="T",
    )

    assert sid == "wamid.OUT"
    assert seen["method"] == "POST"
    assert seen["path"] == "PNID/messages"
    assert seen["token"] == "T"
    assert seen["body"]["messaging_product"] == "whatsapp"
    assert seen["body"]["to"] == "+393331112223"
    assert seen["body"]["type"] == "text"
    assert seen["body"]["text"]["body"] == "Ciao!"


@pytest.mark.asyncio
async def test_send_interactive_carries_at_most_three_buttons(monkeypatch):
    seen = {}

    async def fake_request(method, path, *, token, json_body=None, params=None):
        seen.update(body=json_body)
        return {"messages": [{"id": "wamid.B"}]}

    monkeypatch.setattr(meta, "_request", fake_request)

    sid = await meta.send_interactive(
        phone_number_id="PNID", to="+39", token="T",
        body="Cosa ti serve?",
        buttons=[("book", "Prenotare"), ("move", "Spostare"), ("other", "Altro")],
    )

    assert sid == "wamid.B"
    assert seen["body"]["type"] == "interactive"
    assert seen["body"]["interactive"]["type"] == "button"
    assert seen["body"]["interactive"]["body"]["text"] == "Cosa ti serve?"
    btns = seen["body"]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in btns] == ["book", "move", "other"]
    assert [b["reply"]["title"] for b in btns] == ["Prenotare", "Spostare", "Altro"]
    assert all(len(b["reply"]["title"]) <= 20 for b in btns)
    assert all(b["type"] == "reply" for b in btns)


@pytest.mark.asyncio
async def test_send_interactive_refuses_more_than_three(monkeypatch):
    async def fake_request(*a, **kw):
        raise AssertionError("must not call Graph when refusing locally")

    monkeypatch.setattr(meta, "_request", fake_request)

    with pytest.raises(ValueError):
        await meta.send_interactive(
            phone_number_id="P", to="+39", token="T", body="x",
            buttons=[("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")],
        )


@pytest.mark.asyncio
async def test_send_interactive_truncates_a_title_over_twenty_chars(monkeypatch):
    seen = {}

    async def fake_request(method, path, *, token, json_body=None, params=None):
        seen.update(body=json_body)
        return {"messages": [{"id": "wamid.C"}]}

    monkeypatch.setattr(meta, "_request", fake_request)

    await meta.send_interactive(
        phone_number_id="P", to="+39", token="T", body="x",
        buttons=[("id1", "A title that is definitely far too long")],
    )

    title = seen["body"]["interactive"]["action"]["buttons"][0]["reply"]["title"]
    assert len(title) <= 20


@pytest.mark.asyncio
async def test_get_media_fetches_the_url_then_downloads_it_with_the_same_bearer(
    monkeypatch,
):
    seen = {}

    async def fake_request(method, path, *, token, json_body=None, params=None):
        seen.update(meta_method=method, meta_path=path, meta_token=token)
        return {"url": "https://lookaside.example/media/xyz", "mime_type": "application/pdf"}

    class FakeResponse:
        content = b"%PDF-fake-bytes"

        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *, headers=None):
            seen.update(dl_url=url, dl_headers=headers)
            return FakeResponse()

    monkeypatch.setattr(meta, "_request", fake_request)
    monkeypatch.setattr(meta, "AsyncClient", FakeAsyncClient)

    content = await meta.get_media(media_id="MID", token="T")

    assert content == b"%PDF-fake-bytes"
    # first hop: the metadata call that resolves the short-lived URL
    assert seen["meta_method"] == "GET"
    assert seen["meta_path"] == "MID"
    assert seen["meta_token"] == "T"
    # second hop: the download itself, carrying the same bearer
    assert seen["dl_url"] == "https://lookaside.example/media/xyz"
    assert seen["dl_headers"]["Authorization"] == "Bearer T"
