"""The agent's right to speak, and what it costs.

Three writers share one thread — the customer, the owner (webapp AND the
WhatsApp Business App on their own phone), and the agent — and only the last is
ours to control. So nearly every test here asserts that the agent said
*nothing*: the failure this mechanism exists to prevent is an agent talking over
the owner, and with coexistence that is a day-one event, not an eventual one.

The economics are the other half. A turn costs real money, so "three messages
produce one reply" and "a conversation has a ceiling" are billing tests as much
as behavioural ones.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from booking_engine.clients import marketing_agent
from booking_engine.services.messaging import wa_agent, wa_inbound

SHOP = uuid4()
CALL = uuid4()
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
PHONE = "+393331112222"
SENDER = {"shop_id": SHOP, "phone_number_id": "PNID", "access_token": "opened-token"}


def row(**kw) -> dict:
    return {"id": uuid4(), "from_phone": PHONE, "body": "vorrei prenotare",
            "message_type": "text", "wa_message_id": "wamid.1",
            "intent": None, "confidence": None, "customer_id": None, **kw}


def hist(rid, minutes_ago: int = 0, intent=None) -> dict:
    return {"id": rid, "received_at": NOW - timedelta(minutes=minutes_ago),
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


def a_turn(text="Certo! Che giorno preferisci?", escalate=False, reason=None):
    return marketing_agent.Turn(text=text, escalate=escalate, reason=reason,
                                tool_calls=0, cost_usd=0.0004)


@pytest.fixture
def wired(monkeypatch):
    """Every collaborator replaced. Any real network or DB call is a bug.

    The debounce is set to zero: the *rule* under test is "am I still the newest
    message", never the wall-clock wait, and two real seconds per test would buy
    nothing but a slow suite.
    """
    monkeypatch.setattr(wa_agent, "DEBOUNCE_SECONDS", 0)

    fakes = SimpleNamespace(
        config=Spy({"whatsapp_agent_enabled": True}),
        history=Spy([]),
        open_session=Spy(CALL),
        state=Spy({"started_at": NOW - timedelta(minutes=5), "escalated": False,
                   "agent_turns": 0, "human_replied_at": None}),
        transcript=Spy([{"role": "user", "content": "vorrei prenotare"}]),
        mark_escalated=Spy(None),
        turn=Spy(a_turn()),
        send_text=Spy("wamid.out"),
        record_reply=Spy({}),
        services=Spy([{"id": uuid4(), "service_name": "Taglio",
                       "duration_minutes": 30, "price_eur": 25}]),
        shop=Spy({"shop_name": "Salone Rosa"}),
        customers=Spy([{"id": uuid4(), "full_name": "Giulia"}]),
        intake=Spy({}),
    )
    monkeypatch.setattr(wa_agent.config_q, "get_config", fakes.config)
    monkeypatch.setattr(wa_agent.tq, "inbound_history", fakes.history)
    monkeypatch.setattr(wa_agent.wsq, "open_session", fakes.open_session)
    monkeypatch.setattr(wa_agent.wsq, "session_state", fakes.state)
    monkeypatch.setattr(wa_agent.wsq, "session_transcript", fakes.transcript)
    monkeypatch.setattr(wa_agent.wsq, "mark_escalated", fakes.mark_escalated)
    monkeypatch.setattr(wa_agent.marketing_agent, "turn", fakes.turn)
    monkeypatch.setattr(wa_agent.meta, "send_text", fakes.send_text)
    monkeypatch.setattr(wa_agent.tq, "record_reply", fakes.record_reply)
    monkeypatch.setattr(wa_agent.queries, "list_services", fakes.services)
    monkeypatch.setattr(wa_agent.queries, "get_shop", fakes.shop)
    monkeypatch.setattr(wa_agent.queries, "find_customers_by_phone", fakes.customers)
    monkeypatch.setattr(wa_agent.intake_q, "for_services", fakes.intake)
    return fakes


async def run(wired, r=None, intent="booking"):
    """Handle one message, with the thread's history containing just it.

    History is always reset to exactly this row: otherwise a second `run` in
    one test would look superseded by the first's message and stand down,
    which reads as a passing assertion about a turn that never happened.
    """
    r = r or row()
    wired.history.result = [hist(r["id"])]
    await wa_agent.handle(SENDER, r, intent=intent)
    return r


# --- may_speak is pure, and silence is the default --------------------------

def test_may_speak_is_pure_and_needs_no_clock_or_database():
    ok, reason = wa_agent.may_speak({
        "agent_enabled": True, "intent": "booking",
        "escalated": False, "human_replied_at": None, "agent_turns": 0,
    })
    assert (ok, reason) == (True, "ok")


def test_an_unknown_thread_state_defaults_to_silence():
    """A brand-new key nobody thought of must not become a licence to speak.

    An empty dict is the strongest form of that: the two positive requirements
    are things a caller has to *establish*, never things it gets by default.
    """
    ok, reason = wa_agent.may_speak({})
    assert ok is False
    assert reason == "not_opted_in"


def test_every_refusal_reason_is_a_distinct_string():
    """Task 24 renders one sentence per reason. Two rules sharing a string
    would render the wrong sentence, and 'the agent is quiet and nobody can say
    why' is the state that makes an owner switch it off."""
    reasons = [
        wa_agent.may_speak({})[1],
        wa_agent.may_speak({"agent_enabled": True, "intent": "complaint"})[1],
        wa_agent.may_speak({"agent_enabled": True, "intent": "booking",
                            "escalated": True})[1],
        wa_agent.may_speak({"agent_enabled": True, "intent": "booking",
                            "human_replied_at": NOW})[1],
    ]
    assert reasons == ["not_opted_in", "intent_not_whitelisted", "escalated",
                       "human_took_over"]
    assert len(set(reasons)) == len(reasons)


def test_the_opt_in_default_is_off():
    """A salon that has not asked for a robot must never get one. The column
    defaults to false in migration 25; this pins the code path that reads it."""
    assert wa_agent.may_speak({"agent_enabled": False, "intent": "booking"}) \
        == (False, "not_opted_in")


# --- opt-in and the allowlist -----------------------------------------------

async def test_a_shop_that_has_not_opted_in_is_never_handled(wired):
    wired.config.result = {"whatsapp_agent_enabled": False}

    await run(wired)

    assert wired.turn.count == 0
    assert wired.send_text.count == 0
    # Not even a session row: a shop that never asked for an agent costs
    # nothing to not-answer.
    assert wired.open_session.count == 0


async def test_a_shop_with_no_config_row_at_all_is_never_handled(wired):
    wired.config.result = None

    await run(wired)

    assert wired.turn.count == 0
    assert wired.send_text.count == 0


async def test_an_intent_outside_the_whitelist_never_runs_a_turn(wired):
    """Opted in is not enough. Routing is an explicit allowlist, and a
    complaint is a person's even on a shop that bought the agent."""
    await run(wired, intent="complaint")

    assert wired.turn.count == 0
    assert wired.send_text.count == 0


# --- the debounce ------------------------------------------------------------

async def test_three_messages_in_a_row_produce_one_reply(wired):
    """"ciao" / "volevo prenotare" / "per sabato" is one thought. Answering
    each is three replies to it, and three billed turns."""
    r1, r2, r3 = row(body="ciao"), row(body="volevo prenotare"), row(body="per sabato")
    # Whoever asks, the database says the same thing: r3 is the newest.
    wired.history.result = [hist(r1["id"], 2), hist(r2["id"], 1), hist(r3["id"], 0)]

    await asyncio.gather(*(wa_agent.handle(SENDER, r, intent="booking")
                           for r in (r1, r2, r3)))

    assert wired.turn.count == 1
    assert wired.send_text.count == 1


async def test_the_debounce_batches_rather_than_dropping(wired):
    """All three messages reach the agent, not just the last one. Only the two
    earlier *turns* are dropped — never the two earlier messages."""
    r1, r2, r3 = row(body="ciao"), row(body="volevo prenotare"), row(body="per sabato")
    wired.history.result = [hist(r1["id"], 2), hist(r2["id"], 1), hist(r3["id"], 0)]
    wired.transcript.result = [
        {"role": "user", "content": "ciao"},
        {"role": "user", "content": "volevo prenotare"},
        {"role": "user", "content": "per sabato"},
    ]

    await asyncio.gather(*(wa_agent.handle(SENDER, r, intent="booking")
                           for r in (r1, r2, r3)))

    seen = [m["content"] for m in wired.turn.last["messages"]]
    assert seen == ["ciao", "volevo prenotare", "per sabato"]
    # The last message is in what the agent saw, not merely the first.
    assert "per sabato" in seen


async def test_two_messages_cannot_produce_two_turns(wired):
    r1, r2 = row(body="ciao"), row(body="per sabato")
    wired.history.result = [hist(r1["id"], 1), hist(r2["id"], 0)]

    await asyncio.gather(wa_agent.handle(SENDER, r1, intent="booking"),
                         wa_agent.handle(SENDER, r2, intent="booking"))

    assert wired.turn.count == 1


async def test_two_different_threads_do_not_interfere(wired, monkeypatch):
    """The debounce is scoped to one shop and one phone. Two customers writing
    at the same instant must each get an answer."""
    other_phone = "+393339998888"
    mine, theirs = row(), row(from_phone=other_phone)

    async def history(shop_id, phone):
        return [hist(mine["id"])] if phone == PHONE else [hist(theirs["id"])]

    monkeypatch.setattr(wa_agent.tq, "inbound_history", history)

    await asyncio.gather(wa_agent.handle(SENDER, mine, intent="booking"),
                         wa_agent.handle(SENDER, theirs, intent="booking"))

    assert wired.turn.count == 2
    assert {c["to"] for c in wired.send_text.calls} == {PHONE, other_phone}


# --- handover: the owner is speaking ----------------------------------------

async def test_an_echo_from_the_owners_phone_suspends_the_agent(wired):
    """Every salon is coexistence: the number is live on the owner's phone and
    they answer from it out of habit. Meta reports that as origin='phone'."""
    wired.state.result = {**wired.state.result, "human_replied_at": NOW}

    await run(wired)

    assert wired.turn.count == 0
    assert wired.send_text.count == 0


async def test_a_reply_typed_in_the_webapp_suspends_it_too(wired):
    """origin='kairo'. Same rule, same reason — a human took the thread."""
    wired.state.result = {**wired.state.result, "human_replied_at": NOW}

    ok, reason = wa_agent.may_speak({
        "agent_enabled": True, "intent": "booking", "human_replied_at": NOW,
    })

    await run(wired)

    assert (ok, reason) == (False, "human_took_over")
    assert wired.send_text.count == 0


async def test_a_human_taking_over_is_not_marked_escalated(wired):
    """They are already handling it. Marking it escalated would put a thread
    the owner is actively answering back into their own 'to do' queue."""
    wired.state.result = {**wired.state.result, "human_replied_at": NOW}

    await run(wired)

    assert wired.mark_escalated.count == 0


async def test_the_agents_own_reply_does_not_re_trigger_itself(wired):
    """The reply is recorded as origin='agent', NOT 'kairo'. Recorded as
    'kairo' it would read as the owner arriving and the agent would silence
    itself after exactly one turn — a self-inflicted suspension."""
    await run(wired)

    assert wired.record_reply.last["origin"] == "agent"
    # And the handover rule only ever counts the two human origins.
    import inspect
    src = inspect.getsource(wa_agent.wsq)
    assert "o.origin IN ('kairo', 'phone')" in src
    assert "o.origin = 'agent'" in src
    # Second line of defence, verified against a scratch Postgres: if Meta
    # turns out to echo our own Cloud API sends back on `smb_message_echoes`
    # (unverified — no live WABA has ever been called from this repo), the
    # echo lands as origin='phone' and would read as the owner arriving. Both
    # paths carry the wamid in provider_sid, so a self-echo is excluded.
    assert "mine.origin = 'agent'" in src
    assert "mine.provider_sid = o.provider_sid" in src


# --- escalation --------------------------------------------------------------

async def test_an_escalation_marks_the_session_and_stops_the_agent(wired):
    wired.turn.result = a_turn(text="", escalate=True, reason="customer_asked")

    await run(wired)

    assert wired.mark_escalated.count == 1
    assert wired.mark_escalated.last["call_id"] == CALL
    assert wired.mark_escalated.last["reason"] == "customer_asked"


async def test_the_next_message_after_an_escalation_does_not_run_a_turn(wired):
    """Not just this message — the *next* one. An escalation that only stopped
    the turn that caused it would hand the thread back one message later."""
    wired.state.result = {**wired.state.result, "escalated": True}

    await run(wired)

    assert wired.turn.count == 0
    assert wired.send_text.count == 0


async def test_a_turn_that_escalates_sends_nothing_to_the_customer(wired):
    """`text` is empty whenever `escalate` is true. An apology that still reads
    like an answer leaves the customer waiting for a reply never coming."""
    wired.turn.result = a_turn(text="", escalate=True, reason="unclear")

    await run(wired)

    assert wired.send_text.count == 0
    assert wired.record_reply.count == 0


async def test_text_riding_along_with_an_escalation_is_still_not_sent(wired):
    """Belt and braces on the engine's contract: if a future change there let
    an apology ride along, the customer must still not be answered twice."""
    wired.turn.result = marketing_agent.Turn(
        text="Mi dispiace, ti faccio richiamare.", escalate=True,
        reason="unclear", tool_calls=0, cost_usd=0.0,
    )

    await run(wired)

    assert wired.send_text.count == 0


async def test_an_empty_text_sends_nothing_even_when_not_escalating(wired):
    """Graph rejects an empty body, and a blank bubble is worse than silence."""
    wired.turn.result = a_turn(text="   ", escalate=False)

    await run(wired)

    assert wired.send_text.count == 0
    assert wired.record_reply.count == 0


# --- the empty basket --------------------------------------------------------

async def test_the_agent_stands_down_silently_on_an_empty_basket(wired):
    """402 from the engine. No message at all, and the thread needs attention."""
    from booking_engine.db import whatsapp_thread_queries as tq

    wired.turn.result = a_turn(text="", escalate=True, reason="no_credit")

    await run(wired)

    assert wired.send_text.count == 0
    assert wired.record_reply.count == 0
    assert wired.mark_escalated.last["reason"] == "no_credit"
    # ...and 'escalated' is exactly what puts it in the owner's 'Da gestire'.
    assert tq.needs_attention({"escalated": True, "intent": "booking"}) is True






# --- the payload the engine is handed ---------------------------------------

async def test_the_first_turn_is_flagged_as_such(wired):
    await run(wired)
    assert wired.turn.last["first_turn"] is True


async def test_a_later_turn_is_not_flagged_as_first(wired):
    wired.state.result = {**wired.state.result, "agent_turns": 3}
    await run(wired)
    assert wired.turn.last["first_turn"] is False


async def test_the_session_id_is_the_authorization_basis_for_the_turn(wired):
    """The engine passes call_id back to this repo's voice tools, which read
    the shop off the session row and never off a header."""
    await run(wired)
    assert wired.turn.last["call_id"] == CALL
    assert wired.open_session.last["shop_id"] == SHOP


async def test_prices_reach_the_agent_as_cents(wired):
    await run(wired)
    assert wired.turn.last["services"][0]["price_cents"] == 2500
    assert wired.turn.last["services"][0]["name"] == "Taglio"


async def test_a_known_customer_is_named_and_an_unknown_one_is_not(wired):
    await run(wired)
    assert wired.turn.last["customer_name"] == "Giulia"

    wired.customers.result = []
    await run(wired, r=row())
    assert wired.turn.last["customer_name"] is None


# --- nothing escapes ---------------------------------------------------------

async def test_an_agent_failure_does_not_escape_the_inbound_worker(wired,
                                                                   monkeypatch):
    wired.turn.raises = RuntimeError("engine down")
    r = row()
    wired.history.result = [hist(r["id"])]

    # `process` is the fire-and-forget wrapper; nothing may escape it.
    monkeypatch.setattr(wa_inbound.tq, "inbound_history", wired.history)
    monkeypatch.setattr(wa_inbound.tq, "set_transcript", Spy(None))
    monkeypatch.setattr(wa_inbound.tq, "set_verdict", Spy(None))
    monkeypatch.setattr(wa_inbound.triage, "classify",
                        Spy({"intent": "booking", "confidence": 0.9}))

    await wa_inbound.process(SENDER, r)


async def test_a_send_failure_does_not_lose_the_escalation_path(wired):
    wired.send_text.raises = RuntimeError("meta 500")

    with pytest.raises(RuntimeError):
        await run(wired)   # handle() itself does not swallow; process() does


async def test_a_reply_that_cannot_be_recorded_is_not_re_sent(wired):
    """Meta already delivered it. Re-sending is the worse of the two wrongs."""
    wired.record_reply.raises = RuntimeError("db down")

    await run(wired)

    assert wired.send_text.count == 1


async def test_a_sender_without_credentials_sends_nothing(wired):
    await wa_agent.handle({"shop_id": SHOP}, row(), intent="booking")

    assert wired.send_text.count == 0


async def test_a_row_with_no_phone_is_ignored(wired):
    await wa_agent.handle(SENDER, {"id": uuid4()}, intent="booking")

    assert wired.turn.count == 0


# --- the inbound worker actually reaches the agent --------------------------

async def test_a_routed_follow_up_message_still_reaches_the_agent(monkeypatch):
    """This is the wiring bug that would have made the whole feature inert:
    `_process` used to *return* on an already-routed session, so the agent
    could only ever see the first message and "sabato alle 10" met silence."""
    handled: list = []
    r = row(body="sabato alle 10")

    monkeypatch.setattr(wa_inbound.tq, "inbound_history",
                        Spy([hist(uuid4(), 5, intent="booking"), hist(r["id"])]))
    monkeypatch.setattr(wa_inbound.triage, "classify", Spy(None))

    async def handle(sender, rr, *, intent):
        handled.append(intent)

    monkeypatch.setattr(wa_inbound.wa_agent, "handle", handle)

    await wa_inbound.process(SENDER, r)

    assert handled == ["booking"]


async def test_a_fresh_routed_message_reaches_the_agent(monkeypatch):
    handled: list = []
    r = row()

    monkeypatch.setattr(wa_inbound.tq, "inbound_history", Spy([hist(r["id"])]))
    monkeypatch.setattr(wa_inbound.tq, "set_verdict", Spy(None))
    monkeypatch.setattr(wa_inbound.triage, "classify",
                        Spy({"intent": "booking", "confidence": 0.9}))

    async def handle(sender, rr, *, intent):
        handled.append(intent)

    monkeypatch.setattr(wa_inbound.wa_agent, "handle", handle)

    await wa_inbound.process(SENDER, r)

    assert handled == ["booking"]


async def test_a_human_decision_never_reaches_the_agent(monkeypatch):
    handled: list = []
    r = row(body="pessimo servizio")

    monkeypatch.setattr(wa_inbound.tq, "inbound_history", Spy([hist(r["id"])]))
    monkeypatch.setattr(wa_inbound.tq, "set_verdict", Spy(None))
    monkeypatch.setattr(wa_inbound.triage, "classify",
                        Spy({"intent": "complaint", "confidence": 0.95}))

    async def handle(sender, rr, *, intent):
        handled.append(intent)

    monkeypatch.setattr(wa_inbound.wa_agent, "handle", handle)

    await wa_inbound.process(SENDER, r)

    assert handled == []
