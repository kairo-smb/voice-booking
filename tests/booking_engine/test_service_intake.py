"""What the owner wants asked before a service is booked.

The table is `voice_agent.service_intake` (migration 24). These tests are about
three things, and two of them are about the text never being trusted as given:

- the 500-character cap is the *server's*, not the textarea's — this text is
  re-read into the agent's prompt on every turn of every conversation that
  touches the service, so an owner who pastes an essay pays for it each time;
- a shop can only write intake for a service it owns, because the one place a
  cross-tenant leak would be invisible is inside a prompt;
- `for_services([])` does not go to the database — the agent calls it on every
  turn, and a query that can only return `{}` is a round trip for nothing.

The fake below applies each statement's own predicate rather than matching SQL
text, so the tests are about behaviour. The SQL itself is checked against a
real Postgres separately (the scratch-DB run in the task report) — a fake
cannot tell us whether `= ANY($2::uuid[])` binds, or whether an
`INSERT … SELECT … WHERE EXISTS … ON CONFLICT` is even legal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from booking_engine.api.routes import voice_config
from booking_engine.config import Settings
from booking_engine.db import service_intake_queries as si

SHOP = uuid4()
OTHER_SHOP = uuid4()
COLORE = uuid4()
PIEGA = uuid4()
SOMEONE_ELSES = uuid4()

# service_id -> owning shop_id. The fake's stand-in for business_app_core.services.
SERVICES = {COLORE: SHOP, PIEGA: SHOP, SOMEONE_ELSES: OTHER_SHOP}


class FakeIntake:
    """voice_agent.service_intake as a dict, running the module's own rules.

    `owned` is the EXISTS sub-select in `set_questions`: a service the shop
    does not own produces no row, exactly as the real statement produces no
    RETURNING row.
    """

    def __init__(self, services: dict[UUID, UUID] | None = None):
        self.rows: dict[tuple[UUID, UUID], dict] = {}
        self.services = services if services is not None else dict(SERVICES)
        self.statements: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.statements.append((sql, args))
        if "= ANY(" in sql:
            shop_id, service_ids = args
            return [
                dict(r) for (s, sv), r in self.rows.items()
                if s == shop_id and sv in service_ids and r["questions"] != ""
            ]
        shop_id = args[0]
        return [dict(r) for (s, _), r in self.rows.items() if s == shop_id]

    async def execute_one(self, sql: str, *args):
        self.statements.append((sql, args))
        shop_id, service_id, questions = args
        if self.services.get(service_id) != shop_id:
            return None          # the EXISTS found nothing: no row inserted
        row = {
            "shop_id": shop_id, "service_id": service_id,
            "questions": questions,
            "updated_at": datetime.now(timezone.utc),
        }
        self.rows[(shop_id, service_id)] = row
        return dict(row)


@pytest.fixture
def db():
    fake = FakeIntake()
    with patch.object(si, "execute", fake.execute), \
         patch.object(si, "execute_one", fake.execute_one):
        yield fake


# --- the cap ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_questions_are_capped_at_500_characters(db):
    # The cap is the server's. A UI counter that the server does not back is
    # not a cap — this text enters the prompt on every single turn.
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="x" * 900)
    got = await si.for_services(SHOP, [COLORE])
    assert len(got[str(COLORE)]) == si.MAX_QUESTIONS_CHARS == 500


@pytest.mark.asyncio
async def test_text_at_the_cap_is_stored_whole(db):
    exact = "y" * 500
    row = await si.set_questions(shop_id=SHOP, service_id=COLORE, questions=exact)
    assert row["questions"] == exact


# --- what the agent reads ---------------------------------------------------

@pytest.mark.asyncio
async def test_only_the_services_in_play_are_returned(db):
    await si.set_questions(shop_id=SHOP, service_id=COLORE,
                           questions="ritocco o completo?")
    await si.set_questions(shop_id=SHOP, service_id=PIEGA,
                           questions="liscia o mossa?")

    got = await si.for_services(SHOP, [COLORE])

    assert got == {str(COLORE): "ritocco o completo?"}
    assert str(PIEGA) not in got


@pytest.mark.asyncio
async def test_for_services_with_no_services_does_not_touch_the_database(db):
    # Called on every turn of every conversation. A query that can only answer
    # {} is a round trip bought for nothing.
    assert await si.for_services(SHOP, []) == {}
    assert db.statements == []


@pytest.mark.asyncio
async def test_a_service_with_no_intake_is_simply_absent(db):
    assert await si.for_services(SHOP, [COLORE, PIEGA]) == {}


# --- writing ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_setting_questions_twice_updates_rather_than_erroring(db):
    # (shop_id, service_id) is the primary key, so this is an upsert or it is
    # an error the owner sees the second time they edit the field.
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="prima")
    row = await si.set_questions(shop_id=SHOP, service_id=COLORE,
                                 questions="seconda")

    assert row["questions"] == "seconda"
    assert await si.for_services(SHOP, [COLORE]) == {str(COLORE): "seconda"}


@pytest.mark.asyncio
async def test_empty_questions_are_allowed_and_mean_nothing_extra_to_ask(db):
    # Today's behaviour for every service, and it stays the default: an empty
    # field is a configured shop, not an unfinished one.
    row = await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="")
    assert row["questions"] == ""
    assert await si.for_services(SHOP, [COLORE]) == {}


@pytest.mark.asyncio
async def test_clearing_questions_removes_them_from_the_agents_view(db):
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="qualcosa")
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="")
    assert await si.for_services(SHOP, [COLORE]) == {}


@pytest.mark.asyncio
async def test_whitespace_only_questions_are_stored_as_empty(db):
    # "   " would put a blank line in the prompt and leave the owner believing
    # they configured something. It is nothing, so it is stored as nothing.
    row = await si.set_questions(shop_id=SHOP, service_id=COLORE,
                                 questions="   \n\t  ")
    assert row["questions"] == ""


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_trimmed_before_the_cap_applies(db):
    row = await si.set_questions(shop_id=SHOP, service_id=COLORE,
                                 questions="  ritocco o completo?  ")
    assert row["questions"] == "ritocco o completo?"


# --- tenancy ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_shop_cannot_write_intake_for_a_service_it_does_not_own(db):
    row = await si.set_questions(shop_id=SHOP, service_id=SOMEONE_ELSES,
                                 questions="chiedi il codice fiscale")
    assert row is None
    assert db.rows == {}


@pytest.mark.asyncio
async def test_another_shops_service_is_never_returned_even_when_asked_for(db):
    # The id is passed in deliberately: the read must scope by shop on its own,
    # not rely on the caller having asked for the right ids. A prompt is
    # exactly where a leak of someone else's configuration would be invisible.
    await si.set_questions(shop_id=OTHER_SHOP, service_id=SOMEONE_ELSES,
                           questions="segreto dell'altro salone")

    got = await si.for_services(SHOP, [COLORE, SOMEONE_ELSES])

    assert got == {}


@pytest.mark.asyncio
async def test_for_shop_lists_only_that_shops_rows(db):
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="a")
    await si.set_questions(shop_id=OTHER_SHOP, service_id=SOMEONE_ELSES,
                           questions="b")

    rows = await si.for_shop(SHOP)

    assert [r["service_id"] for r in rows] == [COLORE]


@pytest.mark.asyncio
async def test_for_shop_keeps_empty_rows_so_the_owner_sees_their_own_field(db):
    # The opposite rule from for_services: the config screen must show the
    # field the owner deliberately cleared, the prompt must not.
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="")
    assert [r["questions"] for r in await si.for_shop(SHOP)] == [""]


# --- the routes -------------------------------------------------------------

HEADERS = {"Authorization": "Bearer test-secret"}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(voice_config.router, prefix="/api/v1")
    from booking_engine.api import deps
    app.dependency_overrides[deps._get_settings] = lambda: Settings(
        database_url="", control_plane_secret="test-secret",
    )
    return app


def test_get_intake_returns_the_shops_rows():
    rows = [{"service_id": COLORE, "questions": "ritocco o completo?",
             "updated_at": datetime.now(timezone.utc)}]
    with patch.object(voice_config.intake, "for_shop", AsyncMock(return_value=rows)):
        r = TestClient(_app()).get(f"/api/v1/voice/config/{SHOP}/intake",
                                   headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["data"][0]["questions"] == "ritocco o completo?"


def test_put_intake_writes_and_returns_the_row():
    row = {"shop_id": SHOP, "service_id": COLORE, "questions": "ok",
           "updated_at": datetime.now(timezone.utc)}
    with patch.object(voice_config.intake, "set_questions",
                      AsyncMock(return_value=row)) as sq:
        r = TestClient(_app()).put(
            f"/api/v1/voice/config/{SHOP}/intake/{COLORE}",
            json={"questions": "ok"}, headers=HEADERS,
        )
    assert r.status_code == 200
    assert sq.await_args.kwargs["questions"] == "ok"
    assert r.json()["data"]["questions"] == "ok"


def test_put_intake_for_a_service_the_shop_does_not_own_is_a_404():
    with patch.object(voice_config.intake, "set_questions",
                      AsyncMock(return_value=None)):
        r = TestClient(_app()).put(
            f"/api/v1/voice/config/{SHOP}/intake/{SOMEONE_ELSES}",
            json={"questions": "x"}, headers=HEADERS,
        )
    assert r.status_code == 404


def test_the_intake_routes_need_the_control_plane_token():
    r = TestClient(_app()).get(f"/api/v1/voice/config/{SHOP}/intake")
    assert r.status_code in (401, 403)


def test_the_intake_route_does_not_shadow_the_config_route():
    # `/voice/config/{shop_id}` already exists and `{shop_id}` is greedy in
    # neither direction — pinned because adding a sibling path under a dynamic
    # segment is exactly where a route table silently reorders.
    with patch.object(voice_config, "get_config", AsyncMock(return_value=None)):
        r = TestClient(_app()).get(f"/api/v1/voice/config/{SHOP}", headers=HEADERS)
    assert r.status_code == 200
