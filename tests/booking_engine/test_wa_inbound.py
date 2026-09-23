"""The inbound worker: everything that happens after the webhook answered 200.

Two properties are load-bearing here, and both are asserted rather than argued:

1. **The task is held.** asyncio keeps only a weak reference to a bare
   `create_task`, so a dropped return value can be collected mid-flight. This
   repo shipped exactly that bug in the call supervisor (CLAUDE.md 2026-07-21)
   and it presented as intermittent silence — the hardest symptom to diagnose.
2. **Nothing escapes `process`.** It is awaited from a fire-and-forget task,
   where an uncaught exception is a customer message silently lost with no
   error anywhere. Every collaborator is forced to raise in turn below.

Underneath both: the classifier costs real money per call, so the tests that
assert it was *not* called (a tap, a routed session, an empty body) are about
the economics as much as the behaviour.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from booking_engine.services.messaging import wa_inbound

SHOP = uuid4()
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
PHONE = "+393331112222"

# `access_token` arrives already opened: `whatsapp_queries._opened` unseals it
# at the one boundary every reader goes through, so the worker never touches
# secret_box itself.
SENDER = {"shop_id": SHOP, "phone_number_id": "PNID", "access_token": "opened-token"}


def text_row(**kw) -> dict:
    return {
        "id": uuid4(), "from_phone": PHONE, "body": "ciao",
        "message_type": "text", "wa_message_id": "wamid.1",
        "intent": None, "confidence": None,
        **kw,
    }


def audio_row(**kw) -> dict:
    return text_row(**{"body": "", "message_type": "audio",
                       "media_id": "MEDIA1", **kw})


def hist(minutes_ago: int, intent=None) -> dict:
    return {"id": uuid4(), "received_at": NOW - timedelta(minutes=minutes_ago),
            "intent": intent, "confidence": None, "message_type": "text"}


class Spy:
    """One awaitable stand-in: records its calls, then returns or raises."""

    def __init__(self, result=None, raises: Exception | None = None):
        self.calls: list[dict] = []
        self.args: list[tuple] = []
        self.result = result
        self.raises = raises

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        self.args.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture(autouse=True)
def _drain_tasks():
    """A leaked task from one test must not be counted by the next."""
    wa_inbound._TASKS.clear()
    yield
    wa_inbound._TASKS.clear()


@pytest.fixture
def wired(monkeypatch):
    """Every collaborator replaced. Any real network or DB call is a bug."""
    fakes = SimpleNamespace(
        history=Spy([]),
        set_transcript=Spy(None),
        set_verdict=Spy(None),
        get_media=Spy(b"OggS-fake-bytes"),
        transcribe=Spy("vorrei prenotare per sabato"),
        classify=Spy({"intent": "booking", "confidence": 0.9, "summary": "s"}),
        send_interactive=Spy("wamid.out"),
        # The booking agent is stubbed here on purpose: this file is about what
        # the worker *dispatches*, and the agent's own rules about whether it
        # may then speak have their own file (test_wa_agent.py). Letting the
        # real one run would also drag a database into every test below.
        agent=Spy(None),
    )
    monkeypatch.setattr(wa_inbound.wa_agent, "handle", fakes.agent)
    monkeypatch.setattr(wa_inbound.tq, "inbound_history", fakes.history)
    monkeypatch.setattr(wa_inbound.tq, "set_transcript", fakes.set_transcript)
    monkeypatch.setattr(wa_inbound.tq, "set_verdict", fakes.set_verdict)
    monkeypatch.setattr(wa_inbound.meta, "get_media", fakes.get_media)
    monkeypatch.setattr(wa_inbound.wa_transcribe, "transcribe", fakes.transcribe)
    monkeypatch.setattr(wa_inbound.triage, "classify", fakes.classify)
    monkeypatch.setattr(wa_inbound.meta, "send_interactive", fakes.send_interactive)
    return fakes


# --- the task set -----------------------------------------------------------

async def test_the_task_is_held_so_it_cannot_be_collected_mid_flight(wired):
    wa_inbound.schedule(SENDER, text_row())
    assert len(wa_inbound._TASKS) == 1

    await asyncio.gather(*wa_inbound._TASKS)
    # The done callback runs through call_soon, not inline with completion.
    await asyncio.sleep(0)
    assert wa_inbound._TASKS == set()


async def test_a_task_that_raises_is_still_discarded_from_the_set(wired):
    wired.history.raises = RuntimeError("db down")
    wa_inbound.schedule(SENDER, text_row())
    await asyncio.gather(*wa_inbound._TASKS)
    await asyncio.sleep(0)
    assert wa_inbound._TASKS == set()


# --- voice notes ------------------------------------------------------------

async def test_an_audio_message_is_transcribed_into_transcript_not_body(wired):
    wired.transcribe.result = "vorrei prenotare per sabato"
    row = audio_row()

    await wa_inbound.process(SENDER, row)

    assert wired.set_transcript.count == 1
    assert wired.set_transcript.args[-1][:2] == (row["id"], "vorrei prenotare per sabato")
    # The raw fact stays true: nothing wrote back to `body`.
    assert row["body"] == ""


async def test_the_transcript_is_what_gets_classified(wired):
    wired.transcribe.result = "volevo disdire"

    await wa_inbound.process(SENDER, audio_row())

    assert wired.classify.last["text"] == "volevo disdire"


async def test_the_media_is_fetched_with_the_salons_own_token(wired):
    await wa_inbound.process(SENDER, audio_row())

    assert wired.get_media.last == {"media_id": "MEDIA1", "token": "opened-token"}


async def test_a_failed_transcription_still_classifies_whatever_body_held(wired):
    wired.transcribe.result = None
    row = audio_row(body="testo di riserva")

    await wa_inbound.process(SENDER, row)

    assert wired.set_transcript.count == 0   # NULL means "we do not know"
    assert wired.classify.last["text"] == "testo di riserva"


async def test_an_empty_transcription_result_writes_no_transcript(wired):
    wired.transcribe.result = ""

    await wa_inbound.process(SENDER, audio_row(body="qualcosa"))

    assert wired.set_transcript.count == 0


# --- the classifier, and when it must not run -------------------------------

async def test_an_empty_basket_leaves_raw_text_and_no_verdict(wired):
    # The engine refused (402) and `classify` returns None rather than a guess.
    wired.classify.result = None
    row = text_row(body="ciao")

    await wa_inbound.process(SENDER, row)

    assert wired.set_verdict.count == 0      # intent stays NULL -> a human
    assert wired.send_interactive.count == 0
    assert row["intent"] is None


async def test_a_routed_session_never_calls_the_classifier_again(wired):
    """The classifier must not run again — but the *handler* must. A routed
    session used to return outright here, which meant the agent only ever saw
    the first message of a conversation and every follow-up met silence."""
    wired.history.result = [hist(5, intent="booking"), hist(1)]

    await wa_inbound.process(SENDER, text_row(body="e per il colore?"))

    assert wired.classify.count == 0
    assert wired.send_interactive.count == 0
    assert wired.agent.count == 1
    assert wired.agent.last["intent"] == "booking"


async def test_a_button_tap_does_not_call_the_classifier(wired):
    # The webhook already stored the verdict: the id is one we defined, so it
    # IS the intent. A model call here would be money spent on a known answer.
    tap = text_row(body="Prenotare", message_type="interactive",
                   intent="booking", confidence=1.0)
    wired.history.result = [hist(0, intent="booking")]

    await wa_inbound.process(SENDER, tap)

    assert wired.classify.count == 0


async def test_an_empty_body_is_never_sent_to_the_classifier(wired):
    # An unsupported type (a sticker, a location) lands with no text at all.
    # There is nothing to classify and the call costs real money.
    await wa_inbound.process(SENDER, text_row(body="   ", message_type="image"))

    assert wired.classify.count == 0
    assert wired.set_verdict.count == 0


# --- acting on the decision -------------------------------------------------

async def test_low_confidence_sends_the_button_menu(wired):
    wired.classify.result = {"intent": "booking", "confidence": 0.3}

    await wa_inbound.process(SENDER, text_row(body="boh"))

    assert wired.send_interactive.count == 1
    assert wired.send_interactive.last["buttons"] == [
        ("booking", "Prenotare"),
        ("reschedule", "Spostare o disdire"),
        ("other", "Altro"),
    ]
    assert wired.send_interactive.last["to"] == PHONE
    assert wired.send_interactive.last["phone_number_id"] == "PNID"
    assert wired.send_interactive.last["token"] == "opened-token"
    assert wired.send_interactive.last["body"] == wa_inbound.MENU_BODY


async def test_the_menu_decision_stores_a_verdict_with_no_intent(wired):
    """A menu leaves the session unrouted on purpose — otherwise the next
    message would skip the classifier and the menu would be answered by
    nobody. `set_verdict` owns that rule; this asserts what it is handed."""
    wired.classify.result = {"intent": "booking", "confidence": 0.3}

    await wa_inbound.process(SENDER, text_row(body="boh"))

    assert wired.set_verdict.count == 1
    assert wired.set_verdict.args[-1][2].action == "menu"


async def test_a_human_decision_sends_nothing_at_all(wired):
    """We do not tell the customer "a human will reply" — the thread simply
    lands in the owner's 'Da gestire' queue."""
    wired.classify.result = {"intent": "complaint", "confidence": 0.95}

    await wa_inbound.process(SENDER, text_row(body="pessimo servizio"))

    assert wired.send_interactive.count == 0
    assert wired.set_verdict.count == 1
    assert wired.set_verdict.args[-1][2].action == "human"


async def test_tapping_altro_lands_on_a_human(wired):
    """'other' is deliberately NOT in wa_routing.WHITELIST, so the third
    button routes to a person rather than to a handler that cannot help."""
    from booking_engine.services.messaging import wa_routing

    assert "other" not in wa_routing.WHITELIST
    assert wa_routing.decide(history=[], button_id="other") == ("human", None)


async def test_a_confident_route_hands_the_thread_to_the_agent(wired):
    """The verdict is stored, no menu goes out, and the booking agent is asked
    to answer. Whether it then *may* speak is its own decision — opt-in,
    handover and the turn ceiling all live in `wa_agent.may_speak`."""
    wired.classify.result = {"intent": "booking", "confidence": 0.9}

    await wa_inbound.process(SENDER, text_row(body="vorrei un taglio"))

    assert wired.send_interactive.count == 0
    assert wired.set_verdict.args[-1][2] == ("route", "booking")
    assert wired.agent.count == 1
    assert wired.agent.last["intent"] == "booking"


async def test_a_human_decision_never_reaches_the_agent(wired):
    wired.classify.result = {"intent": "complaint", "confidence": 0.95}

    await wa_inbound.process(SENDER, text_row(body="pessimo servizio"))

    assert wired.agent.count == 0


async def test_a_menu_decision_never_reaches_the_agent(wired):
    """The session is still unrouted — there is no named request to answer."""
    wired.classify.result = {"intent": "booking", "confidence": 0.3}

    await wa_inbound.process(SENDER, text_row(body="boh"))

    assert wired.agent.count == 0


async def test_the_menu_is_not_sent_when_the_sender_has_no_credentials(wired):
    wired.classify.result = {"intent": "booking", "confidence": 0.3}

    await wa_inbound.process({"shop_id": SHOP}, text_row(body="boh"))

    assert wired.send_interactive.count == 0


# --- nothing escapes --------------------------------------------------------

async def test_a_media_download_failure_does_not_escape(wired):
    wired.get_media.raises = RuntimeError("media gone")

    await wa_inbound.process(SENDER, audio_row(body="testo di riserva"))

    # Degraded to raw text, and the rest of the pipeline ran anyway.
    assert wired.set_transcript.count == 0
    assert wired.classify.last["text"] == "testo di riserva"


async def test_a_transcription_failure_does_not_escape(wired):
    wired.transcribe.raises = RuntimeError("stt exploded")

    await wa_inbound.process(SENDER, audio_row(body="testo di riserva"))

    assert wired.classify.last["text"] == "testo di riserva"


async def test_a_classifier_failure_does_not_escape(wired):
    wired.classify.raises = RuntimeError("engine down")

    await wa_inbound.process(SENDER, text_row(body="ciao"))

    assert wired.set_verdict.count == 0


async def test_a_verdict_write_failure_does_not_escape(wired):
    wired.set_verdict.raises = RuntimeError("db down")

    await wa_inbound.process(SENDER, text_row(body="ciao"))


async def test_a_menu_send_failure_does_not_escape_and_the_verdict_survives(wired):
    wired.classify.result = {"intent": "booking", "confidence": 0.3}
    wired.send_interactive.raises = RuntimeError("meta 500")

    await wa_inbound.process(SENDER, text_row(body="boh"))

    # Stored before the send, so the failure costs the menu and nothing else.
    assert wired.set_verdict.count == 1


async def test_a_history_read_failure_does_not_escape(wired):
    wired.history.raises = RuntimeError("db down")

    await wa_inbound.process(SENDER, text_row())

    assert wired.classify.count == 0


async def test_a_malformed_row_does_not_escape(wired):
    await wa_inbound.process(SENDER, {})


# --- the webhook wiring -----------------------------------------------------

async def test_the_webhook_schedules_the_worker_for_a_fresh_message(monkeypatch):
    from booking_engine.api.routes import whatsapp as wa_routes

    stored = {"id": uuid4(), "from_phone": PHONE, "body": "ciao",
              "message_type": "audio", "wa_message_id": "wamid.9"}
    scheduled: list[tuple] = []

    async def _fake_record_inbound(**kw):
        return stored

    monkeypatch.setattr(wa_routes.wq, "record_inbound", _fake_record_inbound)
    monkeypatch.setattr(wa_routes.wa_inbound, "schedule",
                        lambda sender, row: scheduled.append((sender, row)))

    await wa_routes._handle_change(
        sender=SENDER,
        change={"field": "messages", "value": {"messages": [{
            "from": PHONE, "type": "audio", "id": "wamid.9",
            "audio": {"id": "MEDIA1", "mime_type": "audio/ogg"},
        }]}},
    )

    assert len(scheduled) == 1
    sender, row = scheduled[0]
    assert sender["shop_id"] == SHOP
    # The media id lives only on the payload — it is not a column, and the
    # worker cannot download the bytes without it.
    assert row["media_id"] == "MEDIA1"
    assert row["id"] == stored["id"]


async def test_a_replayed_webhook_schedules_nothing(monkeypatch):
    """The dedup exists to stop a Meta retry costing a second classification.
    Scheduling on a replay would hand that cost straight back."""
    from booking_engine.api.routes import whatsapp as wa_routes

    scheduled: list[tuple] = []

    async def _replay(**kw):
        return None

    monkeypatch.setattr(wa_routes.wq, "record_inbound", _replay)
    monkeypatch.setattr(wa_routes.wa_inbound, "schedule",
                        lambda sender, row: scheduled.append((sender, row)))

    await wa_routes._handle_change(
        sender=SENDER,
        change={"field": "messages", "value": {"messages": [
            {"from": PHONE, "type": "text", "text": {"body": "ciao"}},
        ]}},
    )

    assert scheduled == []


async def test_a_typed_message_carries_no_media_id(monkeypatch):
    from booking_engine.api.routes import whatsapp as wa_routes

    stored = {"id": uuid4(), "from_phone": PHONE, "body": "ciao",
              "message_type": "text", "wa_message_id": "wamid.10"}
    scheduled: list[tuple] = []

    async def _fake_record_inbound(**kw):
        return stored

    monkeypatch.setattr(wa_routes.wq, "record_inbound", _fake_record_inbound)
    monkeypatch.setattr(wa_routes.wa_inbound, "schedule",
                        lambda sender, row: scheduled.append((sender, row)))

    await wa_routes._handle_change(
        sender=SENDER,
        change={"field": "messages", "value": {"messages": [
            {"from": PHONE, "type": "text", "text": {"body": "ciao"}},
        ]}},
    )

    assert scheduled[0][1]["media_id"] is None
