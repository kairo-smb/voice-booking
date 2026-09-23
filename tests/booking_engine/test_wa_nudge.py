"""The 20h nudge: one last free message inside the window, never after it.

Meta permits free-form messages only within 24h of the customer's *last*
message, and the window resets every time they write. So the only thing truly
forbidden is us speaking first after 24h of their silence — that needs an
approved template, a paid conversation and a Meta review. A single invitation
to write back, sent while the window is still open, is the cheap mitigation:
their reply is what reopens it.

Nearly every test here asserts silence, for the same reason `test_wa_agent`
does: the expensive mistakes are all "it spoke when it should not have", and
the one past the window is not merely impolite — it is a certain 131047.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from booking_engine.db import connection
from booking_engine.services.messaging import wa_nudge

SHOP = uuid4()
PHONE = "+393331112222"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def thread(*, hours_silent: float = 20.0, anchor: datetime = NOW, **kw) -> dict:
    """The canonical nudgeable thread: the agent answered, and then nothing.

    `last_agent_at` sits one minute after the customer's last message, which is
    what "the agent is waiting" looks like on the rows.

    `anchor` is the instant the silence is measured back from. The rule tests
    pass a fixed one, because `should_nudge` takes `now` as an argument and a
    frozen clock is the point. The sweep tests pass the real one: `sweep` reads
    the wall clock itself, which is exactly the seam a pure rule exists to keep
    out of the policy.
    """
    last_inbound = anchor - timedelta(hours=hours_silent)
    base = {
        "shop_id": SHOP,
        "phone": PHONE,
        "agent_enabled": True,
        "escalated": False,
        "last_inbound": last_inbound,
        "last_agent_at": last_inbound + timedelta(minutes=1),
        "last_nudge_at": None,
        "human_replied_at": None,
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ the rule

def test_a_thread_at_20h_with_the_agent_waiting_is_nudged():
    assert wa_nudge.should_nudge(thread(hours_silent=20), now=NOW) is True


def test_a_thread_at_19h_is_too_early():
    """Exactly 20h nudges; anything short of it is still a live conversation."""
    assert wa_nudge.should_nudge(thread(hours_silent=19.9), now=NOW) is False


def test_a_thread_already_answered_by_the_customer_is_not():
    """They wrote after our last message. The thread is waiting on *us*, and
    "scrivimi pure" to someone who just did is nonsense."""
    last_inbound = NOW - timedelta(hours=20)
    t = thread(last_agent_at=last_inbound - timedelta(hours=1))
    assert t["last_inbound"] > t["last_agent_at"]
    assert wa_nudge.should_nudge(t, now=NOW) is False


def test_a_thread_the_agent_never_spoke_on_is_not_nudged():
    """No agent turn at all — nothing is waiting, so there is nothing to chase."""
    assert wa_nudge.should_nudge(thread(last_agent_at=None), now=NOW) is False


def test_a_thread_is_nudged_at_most_once():
    """The marker is a row, so this survives a restart — see `sweep`."""
    nudged = thread()
    assert wa_nudge.should_nudge(nudged, now=NOW) is True
    already = thread(last_nudge_at=NOW - timedelta(hours=1))
    assert wa_nudge.should_nudge(already, now=NOW) is False


def test_a_nudge_from_before_their_last_message_does_not_block_a_new_one():
    """A nudge is scoped to the silence it answers. A customer who replied and
    then went quiet again has a new silence, and may be invited again."""
    t = thread(last_nudge_at=NOW - timedelta(days=3))
    assert t["last_nudge_at"] < t["last_inbound"]
    assert wa_nudge.should_nudge(t, now=NOW) is True


def test_a_closed_window_is_never_nudged():
    """Past 24h the nudge would have to be a template, which this is not.
    Sending anyway is a certain Meta 131047."""
    assert wa_nudge.should_nudge(thread(hours_silent=24), now=NOW) is False
    assert wa_nudge.should_nudge(thread(hours_silent=30), now=NOW) is False


def test_a_thread_the_owner_replied_to_is_not_nudged():
    """Webapp or phone echo, same rule: they have it, and we are not chasing
    their customer on their behalf."""
    t = thread(human_replied_at=NOW - timedelta(hours=2))
    assert wa_nudge.should_nudge(t, now=NOW) is False


def test_an_owner_reply_from_a_previous_conversation_does_not_block_forever():
    t = thread(human_replied_at=NOW - timedelta(days=40))
    assert wa_nudge.should_nudge(t, now=NOW) is True


def test_an_escalated_thread_is_not_nudged():
    assert wa_nudge.should_nudge(thread(escalated=True), now=NOW) is False


def test_a_shop_that_never_opted_in_is_not_nudged():
    """A nudge is agent behaviour. A manual inbox does not send unbidden."""
    assert wa_nudge.should_nudge(thread(agent_enabled=False), now=NOW) is False


def test_a_thread_that_never_had_an_inbound_message_is_not_nudged():
    assert wa_nudge.should_nudge(thread(last_inbound=None), now=NOW) is False


def test_an_empty_thread_dict_is_silence_not_a_crash():
    assert wa_nudge.should_nudge({}, now=NOW) is False


def test_should_nudge_is_pure(monkeypatch):
    """A dict in, a bool out: no clock and no database.

    `now` is an argument, so the same thread answers differently at different
    instants and identically at the same one. The database is made to explode
    to prove nothing here reaches for it.
    """
    def boom(*a, **kw):  # pragma: no cover - must never be called
        raise AssertionError("should_nudge touched the database")

    monkeypatch.setattr(connection, "execute", boom)
    monkeypatch.setattr(connection, "execute_one", boom)

    t = thread(hours_silent=20)
    assert wa_nudge.should_nudge(t, now=NOW) is True
    assert wa_nudge.should_nudge(t, now=NOW) is True          # no state carried
    assert wa_nudge.should_nudge(t, now=NOW - timedelta(hours=5)) is False
    assert wa_nudge.should_nudge(t, now=NOW + timedelta(hours=5)) is False
    assert t == thread(hours_silent=20)                        # unmutated


# ----------------------------------------------------------------- the sweep

def live() -> datetime:
    """The wall clock `sweep` itself reads. See `thread`'s `anchor`."""
    return datetime.now(timezone.utc)


class Spy:
    def __init__(self, result=None, raises=None):
        self.calls: list[dict] = []
        self.result = result
        self.raises = raises

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def wired(monkeypatch):
    """Every collaborator replaced. Any real network or DB call is a bug."""
    candidates: list[dict] = []
    sender = {"shop_id": SHOP, "status": "online",
              "phone_number_id": "PNID", "access_token": "opened-token"}

    async def list_candidates(nudge_body):
        # The marker the query matches on is the message itself — passed in,
        # never retyped, so the two cannot drift.
        assert nudge_body == wa_nudge.NUDGE_BODY
        return list(candidates)

    get_sender = Spy(result=sender)
    send_text = Spy(result="wamid.nudge")
    record = Spy(result={"id": uuid4()})

    monkeypatch.setattr(wa_nudge.tq, "list_nudge_candidates", list_candidates)
    monkeypatch.setattr(wa_nudge.wq, "get_sender", get_sender)
    monkeypatch.setattr(wa_nudge.meta, "send_text", send_text)
    monkeypatch.setattr(wa_nudge.tq, "record_reply", record)

    return {"candidates": candidates, "sender": sender,
            "send_text": send_text, "record": record, "get_sender": get_sender}


@pytest.mark.asyncio
async def test_the_sweep_sends_and_records_the_nudge_as_the_agent(wired):
    wired["candidates"].append(thread(anchor=live()))
    counts = await wa_nudge.sweep()

    assert counts["nudged"] == 1
    assert wired["send_text"].count == 1
    assert wired["send_text"].calls[0]["body"] == wa_nudge.NUDGE_BODY
    assert wired["send_text"].calls[0]["to"] == PHONE
    # 'agent', never 'kairo': a nudge recorded as the owner would read, on the
    # next turn, as a human taking the thread over.
    assert wired["record"].calls[0]["origin"] == "agent"
    assert wired["record"].calls[0]["body"] == wa_nudge.NUDGE_BODY


@pytest.mark.asyncio
async def test_the_recorded_row_is_what_blocks_the_second_nudge(wired):
    """Durability, stated as the mechanism rather than asserted by faith.

    `record_reply` writes an origin='agent' row whose preview is NUDGE_BODY;
    `list_nudge_candidates` reads exactly that back as `last_nudge_at`. Nothing
    is held in memory, so a restarted process reaches the same verdict.
    """
    wired["candidates"].append(thread(anchor=live()))
    assert (await wa_nudge.sweep())["nudged"] == 1
    assert wired["record"].calls[0]["origin"] == "agent"
    assert wired["record"].calls[0]["body"] == wa_nudge.NUDGE_BODY

    # The next tick re-reads the thread and now finds that row, as
    # `last_nudge_at`. Nothing in the process remembers anything.
    wired["candidates"][:] = [thread(anchor=live(), last_nudge_at=live())]
    assert (await wa_nudge.sweep())["nudged"] == 0
    assert wired["send_text"].count == 1


@pytest.mark.asyncio
async def test_the_sweep_skips_a_thread_the_rule_refuses(wired):
    wired["candidates"].extend([
        thread(anchor=live(), escalated=True),
        thread(anchor=live(), hours_silent=30),
    ])
    counts = await wa_nudge.sweep()
    assert counts["nudged"] == 0
    assert wired["send_text"].count == 0


@pytest.mark.asyncio
async def test_a_sender_that_is_not_online_is_skipped_not_attempted(wired):
    wired["sender"]["status"] = "offline"
    wired["candidates"].append(thread(anchor=live()))
    counts = await wa_nudge.sweep()
    assert counts["nudged"] == 0
    assert wired["send_text"].count == 0


@pytest.mark.asyncio
async def test_one_failing_thread_does_not_abort_the_sweep(wired, monkeypatch):
    good = thread(anchor=live())
    bad = thread(anchor=live(), phone="+393339998888")

    calls: list[str] = []

    async def send_text(*, phone_number_id, to, body, token):
        calls.append(to)
        if to == bad["phone"]:
            raise RuntimeError("Graph is having a day")
        return "wamid.ok"

    monkeypatch.setattr(wa_nudge.meta, "send_text", send_text)
    wired["candidates"].extend([bad, good])

    counts = await wa_nudge.sweep()
    assert counts == {"nudged": 1, "errors": 1}
    assert calls == [bad["phone"], good["phone"]]
