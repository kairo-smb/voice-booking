"""What the owner wants asked before a service is booked.

Some services cannot be booked on the name alone. *Colore* is the standing
example: "chiedi se è ritocco radici o colore completo, e se ha già fatto una
decolorazione negli ultimi 2 mesi." That is domain knowledge only the salon
has, so it is data the owner writes, not copy in a prompt file.

`voice_agent.service_intake` (migration 24) holds it, one row per
(shop, service). It is in `voice_agent` and not `whatsapp` because the phone
agent needs exactly the same thing; it is a table of its own and not a column
on `business_app_core.services` because that schema belongs to the webapp and
this repo does not alter it.

**The cap is the point of this module, not a detail.** The text is re-read
into the agent's prompt on *every turn of every conversation that touches the
service*. An owner who pastes an essay pays for it on each one, and the
agent's own instructions drown in it. So 2000 characters, enforced here — the
character counter in the webapp shows the owner the same limit, but a cap that
only exists in a textarea is not a cap.
"""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute, execute_one

# Per service. Chosen against the prompt, not against the column: `questions`
# is plain `text` and the database would take a novel. Raised from 500 to 2000
# for the webapp's builder, which composes several questions per service and
# had outgrown the older, single-prompt budget.
MAX_QUESTIONS_CHARS = 2000


def normalise(questions: str | None) -> str:
    """Trim, then cap. Empty is a legal, meaningful value.

    Whitespace-only becomes `''` rather than `'   '`: the latter puts a blank
    line in the prompt and leaves the owner believing they configured
    something. Trimming happens **before** the cut so leading spaces cannot eat
    into the 2000 characters the owner is counting down in the UI.
    """
    return (questions or "").strip()[:MAX_QUESTIONS_CHARS].strip()


async def for_services(shop_id: UUID, service_ids: list[UUID]) -> dict[str, str]:
    """What to ask about each of these services, keyed by service id as text.

    Returns **only services that have something to ask**: a row set to empty
    means "nothing extra", and carrying it as `""` into the caller would add a
    blank section to the prompt for no reason. A service with no row at all is
    absent for the same reason — absence and emptiness mean the same thing
    here, which is why the two are deliberately not distinguished.

    No ids, no query. The agent calls this on every turn, and a statement that
    can only answer `{}` is a round trip bought for nothing.

    Scoped by `shop_id` on its own, never by the caller having passed the right
    ids: a prompt is exactly where another salon's configuration would leak
    without anyone seeing it.
    """
    if not service_ids:
        return {}
    rows = await execute(
        """
        SELECT service_id, questions
          FROM voice_agent.service_intake
         WHERE shop_id = $1
           AND service_id = ANY($2::uuid[])
           AND questions <> ''
        """,
        shop_id, list(service_ids),
    )
    return {str(r["service_id"]): r["questions"] for r in rows}


async def for_shop(shop_id: UUID) -> list[dict]:
    """Every row the shop has, for the config screen.

    Unlike `for_services`, empty rows are kept: the owner who deliberately
    cleared a field should see it cleared, not see it vanish. The screen joins
    these to the service list it already loaded — service *names* live in
    `business_app_core`, which this repo reads but does not own, and the webapp
    has them in hand already.
    """
    return await execute(
        """
        SELECT service_id, questions, updated_at
          FROM voice_agent.service_intake
         WHERE shop_id = $1
         ORDER BY service_id
        """,
        shop_id,
    )


async def set_questions(
    *, shop_id: UUID, service_id: UUID, questions: str | None
) -> dict | None:
    """Write (or clear) one service's intake. None = that shop has no such service.

    The `EXISTS` is the tenancy check, and it is in the statement rather than a
    separate read for a reason: a check-then-write leaves a window, and the
    only thing this table is ever used for is text that goes into a prompt.
    A service the shop does not own inserts nothing and returns nothing, which
    the route turns into a 404 — the same answer as an id that does not exist
    at all, which is all a caller for another shop is entitled to learn.

    Upsert, because `(shop_id, service_id)` is the primary key and the second
    edit of a field is the ordinary case, not the error case.
    """
    return await execute_one(
        """
        INSERT INTO voice_agent.service_intake
          (shop_id, service_id, questions, updated_at)
        SELECT $1, $2, $3, now()
         WHERE EXISTS (SELECT 1 FROM business_app_core.services
                        WHERE id = $2 AND shop_id = $1)
        ON CONFLICT (shop_id, service_id) DO UPDATE
           SET questions = EXCLUDED.questions, updated_at = now()
        RETURNING *
        """,
        shop_id, service_id, normalise(questions),
    )
