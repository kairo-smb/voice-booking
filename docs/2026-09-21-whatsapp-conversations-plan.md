# WhatsApp Conversations + Booking Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Customers talk to the salon on WhatsApp; the salon answers from the Inbox, and an agent names, handles and books the routine requests on its own.

**Architecture:** Phase A builds the substrate — threads, the 24h service window, echoes from the owner's phone, voice-note transcripts, free-form replies, and a session-scoped intent classifier. Phase B adds the agent: a tool loop in marketing-engine driving the booking tools that already exist in voice-booking, suspended the moment a human writes. Nothing about booking, authz or constraints is rebuilt — `voice_agent.calls` is already a session table and `execute_tool` is already transport-agnostic.

**Tech Stack:** Python/FastAPI/asyncpg (voice-booking), TypeScript/Express + Anthropic SDK over OpenRouter (marketing-engine), Next.js (webapp), Postgres/Neon, Meta Cloud API v26.0.

**Spec:** `docs/2026-09-21-whatsapp-conversations-design.md`. **Branch:** `feat/whatsapp-conversations`.

---

## Design decisions closed before this plan

| Question | Decision | Why |
|---|---|---|
| Where does the agent loop run? | **marketing-engine**, following `business-advisor/engine.ts`. Its tools HTTP-call voice-booking's `/voice/tools/{name}`. | `llm()` returns an Anthropic client already pointed at OpenRouter with the provider allowlist, ZDR and metering attached. A second loop in voice-booking would duplicate all four. `src/lib/webapp/client.ts` already establishes outbound tool calls from the engine. |
| Two messages arriving together? | **Debounce ~2s, process the group as one turn.** No per-thread lock. | People send "ciao" / "volevo prenotare" / "per sabato" as three messages. Batching is cheaper *and* reads more naturally than three replies. |
| Which tools does the agent get? | All 12 of `DEFAULT_TOOL_ALLOWLIST`, but **`MIN_CHECK_LATENCY_SECONDS` is skipped**. | That 0.8s floor exists so a voice filler phrase isn't followed by a suspiciously instant answer. WhatsApp has no filler phrase; the floor is pure latency. |
| Agent model | `deepseek/deepseek-v4.1-flash` — already in `PROVIDERS_BY_MODEL`, ZDR-routed. | The agent prompt carries the catalogue, intake questions, history and the customer's name. **The §6.4 ZDR waiver is classifier-only and must not extend here.** |

---

## File structure

**voice-booking**

| Path | Responsibility |
|---|---|
| `booking_engine/db/sql/24_whatsapp_conversations.sql` | migration: inbound columns, `origin`, `service_intake`, `calls.channel` |
| `booking_engine/services/messaging/wa_routing.py` | **pure**: session boundary, routing decision, turn cap |
| `booking_engine/services/messaging/wa_threads.py` | thread list/detail assembly, window computation |
| `booking_engine/services/messaging/wa_inbound.py` | the background task: dedup, transcribe, classify, dispatch |
| `booking_engine/clients/webapp_triage.py` | HTTP client to marketing-engine `/whatsapp/triage` and `/whatsapp/agent` |
| `booking_engine/db/whatsapp_thread_queries.py` | SQL for threads, sessions, intake |
| `booking_engine/api/routes/whatsapp.py` | *modify*: echo branch, interactive branch, thread endpoints |
| `booking_engine/clients/meta_whatsapp.py` | *modify*: `send_text`, `send_interactive`, `get_media` |

**marketing-engine**

| Path | Responsibility |
|---|---|
| `src/routes/whatsapp-triage.ts` | the routing classifier (Jev, `allowNonZdr`) |
| `src/routes/whatsapp-agent.ts` | the booking agent turn |
| `src/lib/whatsapp/agent.ts` | the tool loop |
| `src/lib/whatsapp/tools.ts` | tool defs + proxy to voice-booking `/voice/tools/*` |
| `src/lib/llm/client.ts` | *modify*: `allowNonZdr` opt-out |

**webapp**

| Path | Responsibility |
|---|---|
| `src/app/api/v1/hair-salon/whatsapp/threads/route.ts` | list proxy |
| `src/app/api/v1/hair-salon/whatsapp/threads/[phone]/route.ts` | detail + reply proxy |
| `src/components/inbox/whatsapp/ThreadList.tsx` | two sections, "Da gestire" first |
| `src/components/inbox/whatsapp/ThreadView.tsx` | timeline, window countdown, reply box |
| `src/components/inbox/tabs/ConversationsTab.tsx` | *modify*: WhatsApp becomes the content |
| `src/components/settings/ServiceIntakeField.tsx` | per-service questions |

---

# PHASE A — Conversations

## Task 1: Migration 24

**Files:**
- Create: `booking_engine/db/sql/24_whatsapp_conversations.sql`
- Test: `tests/booking_engine/test_migration_23.py`

- [ ] **Step 1: Write the migration**

```sql
-- WhatsApp becomes two-way. See docs/2026-09-21-whatsapp-conversations-design.md.
-- Idempotent: migrate.sh re-applies every file.

-- Who wrote it, and what we made of it.
ALTER TABLE whatsapp.inbound_messages
  ADD COLUMN IF NOT EXISTS customer_id   uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS wa_message_id text,
  ADD COLUMN IF NOT EXISTS transcript    text,
  ADD COLUMN IF NOT EXISTS intent        text,
  ADD COLUMN IF NOT EXISTS confidence    numeric,
  ADD COLUMN IF NOT EXISTS summary       text,
  ADD COLUMN IF NOT EXISTS read_at       timestamptz;

-- Meta retries webhooks. Without this a retry is a duplicate in the thread.
CREATE UNIQUE INDEX IF NOT EXISTS inbound_messages_wa_id_uniq
  ON whatsapp.inbound_messages (wa_message_id) WHERE wa_message_id IS NOT NULL;

COMMENT ON COLUMN whatsapp.inbound_messages.transcript IS
  'Voice-note transcript. NULL means not transcribed — never overwrites body.';
COMMENT ON COLUMN whatsapp.inbound_messages.intent IS
  'Routing verdict for the session this message belongs to. NULL = not classified.';

-- Every sender is coexistence: the owner also answers from their phone, and
-- Meta reports those as message_echoes. 'phone' rows have no provider status
-- lifecycle and never extend the 24h window.
ALTER TABLE whatsapp.outbound_messages
  ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'kairo';
ALTER TABLE whatsapp.outbound_messages DROP CONSTRAINT IF EXISTS outbound_origin_check;
ALTER TABLE whatsapp.outbound_messages ADD CONSTRAINT outbound_origin_check
  CHECK (origin IN ('kairo','phone'));

-- What the agent must ask before booking a given service. Owner-authored.
-- Not a column on business_app_core.services: that schema belongs to the
-- webapp and this repo does not alter it.
CREATE TABLE IF NOT EXISTS voice_agent.service_intake (
  shop_id    uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  service_id uuid NOT NULL REFERENCES business_app_core.services(id) ON DELETE CASCADE,
  questions  text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, service_id)
);

-- voice_agent.calls was never telephony-only: it is a session row.
ALTER TABLE voice_agent.calls
  ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'voice';
ALTER TABLE voice_agent.calls DROP CONSTRAINT IF EXISTS calls_channel_check;
ALTER TABLE voice_agent.calls ADD CONSTRAINT calls_channel_check
  CHECK (channel IN ('voice','whatsapp'));
COMMENT ON COLUMN voice_agent.calls.duration_seconds IS
  'Voice only. Meaningless for channel = whatsapp.';
```

- [ ] **Step 2: Apply twice against a scratch Postgres**

```bash
createdb wa_scratch
for i in 1 2; do psql wa_scratch -v ON_ERROR_STOP=1 -f booking_engine/db/sql/24_whatsapp_conversations.sql && echo "pass $i ok"; done
psql wa_scratch -c '\d whatsapp.inbound_messages' -c '\d voice_agent.service_intake'
```

Expected: `pass 1 ok`, `pass 2 ok`, both tables showing the new columns. A migration that only works once is a migration that breaks the next deploy.

- [ ] **Step 3: Verify the partial unique index actually dedups**

```bash
psql wa_scratch -c "INSERT INTO whatsapp.inbound_messages (shop_id, from_phone, wa_message_id) VALUES ('<shop>','+39','wamid.X') ON CONFLICT (wa_message_id) DO NOTHING;" # twice
psql wa_scratch -c "SELECT count(*) FROM whatsapp.inbound_messages WHERE wa_message_id='wamid.X';"
```

Expected: `1`. Also insert two rows with `wa_message_id IS NULL` and confirm both survive — the index is partial on purpose, because echo and legacy rows have no id.

- [ ] **Step 4: Commit**

```bash
git add booking_engine/db/sql/24_whatsapp_conversations.sql
git commit -m "feat(whatsapp): schema for two-way conversations"
```

---

## Task 2: Graph client — text, interactive, media

**Files:**
- Modify: `booking_engine/clients/meta_whatsapp.py`
- Test: `tests/booking_engine/test_meta_whatsapp_send.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_send_text_posts_a_text_message(monkeypatch):
    seen = {}
    async def fake_request(method, path, *, token, json=None, **kw):
        seen.update(path=path, body=json)
        return {"messages": [{"id": "wamid.OUT"}]}
    monkeypatch.setattr(meta, "_request", fake_request)

    sid = await meta.send_text(phone_number_id="PNID", to="+393331112223",
                               body="Ciao!", token="T")

    assert sid == "wamid.OUT"
    assert seen["path"] == "/PNID/messages"
    assert seen["body"]["type"] == "text"
    assert seen["body"]["text"]["body"] == "Ciao!"


@pytest.mark.asyncio
async def test_send_interactive_carries_at_most_three_buttons(monkeypatch):
    seen = {}
    async def fake_request(method, path, *, token, json=None, **kw):
        seen.update(body=json)
        return {"messages": [{"id": "wamid.B"}]}
    monkeypatch.setattr(meta, "_request", fake_request)

    await meta.send_interactive(
        phone_number_id="PNID", to="+39", token="T",
        body="Cosa ti serve?",
        buttons=[("book", "Prenotare"), ("move", "Spostare"), ("other", "Altro")],
    )

    btns = seen["body"]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in btns] == ["book", "move", "other"]
    assert all(len(b["reply"]["title"]) <= 20 for b in btns)


@pytest.mark.asyncio
async def test_send_interactive_refuses_more_than_three(monkeypatch):
    with pytest.raises(ValueError):
        await meta.send_interactive(
            phone_number_id="P", to="+39", token="T", body="x",
            buttons=[("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_meta_whatsapp_send.py -v`
Expected: FAIL — `AttributeError: module has no attribute 'send_text'`.

- [ ] **Step 3: Implement**

```python
async def send_text(*, phone_number_id: str, to: str, body: str, token: str) -> str:
    """Free-form text. Legal only inside the 24h service window — the caller
    checks that; this function does not, because it is also the agent's send
    path and a second check there would be a second place to get it wrong."""
    data = await _request(
        "POST", f"/{phone_number_id}/messages", token=token,
        json={"messaging_product": "whatsapp", "to": to,
              "type": "text", "text": {"body": body}},
    )
    return (data.get("messages") or [{}])[0].get("id", "")


# Meta's ceiling. A fourth button is silently dropped by Graph, which is worse
# than refusing: the menu would be missing an option nobody notices is missing.
MAX_REPLY_BUTTONS = 3
MAX_BUTTON_TITLE = 20


async def send_interactive(
    *, phone_number_id: str, to: str, body: str,
    buttons: list[tuple[str, str]], token: str,
) -> str:
    """A reply-button menu. `buttons` is [(id, title)] — the id comes back on
    the webhook as interactive.button_reply.id and is the routed intent."""
    if len(buttons) > MAX_REPLY_BUTTONS:
        raise ValueError(f"at most {MAX_REPLY_BUTTONS} reply buttons")
    data = await _request(
        "POST", f"/{phone_number_id}/messages", token=token,
        json={
            "messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [
                    {"type": "reply",
                     "reply": {"id": bid, "title": title[:MAX_BUTTON_TITLE]}}
                    for bid, title in buttons
                ]},
            },
        },
    )
    return (data.get("messages") or [{}])[0].get("id", "")


async def get_media(*, media_id: str, token: str) -> bytes:
    """Two hops: Meta returns a short-lived URL, then the bytes need the same
    bearer. The URL expires in minutes — never store it, always re-fetch."""
    meta_info = await _request("GET", f"/{media_id}", token=token)
    url = meta_info.get("url") or ""
    if not url:
        raise MetaError(None, "media_url_missing")
    async with AsyncClient(timeout=30.0) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.content
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_meta_whatsapp_send.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/clients/meta_whatsapp.py tests/booking_engine/test_meta_whatsapp_send.py
git commit -m "feat(whatsapp): send text, reply buttons, and fetch media"
```

---

## Task 3: Routing — the pure core

This is the heart of Phase A and the only part with real branching. It is a pure
function so it can be tested without a clock, a database or Meta.

**Files:**
- Create: `booking_engine/services/messaging/wa_routing.py`
- Test: `tests/booking_engine/test_wa_routing.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, timedelta, timezone
from booking_engine.services.messaging import wa_routing as r

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

def msg(minutes_ago, intent=None, confidence=None):
    return {"received_at": NOW - timedelta(minutes=minutes_ago),
            "intent": intent, "confidence": confidence}


def test_session_starts_after_a_gap_over_24h():
    history = [msg(60 * 30), msg(60 * 2), msg(5)]   # 30h ago, then 2h, then 5m
    assert r.session_messages(history) == [msg(60 * 2), msg(5)]


def test_a_single_message_is_its_own_session():
    assert r.session_messages([msg(1)]) == [msg(1)]


def test_routed_reads_the_latest_intent_in_the_session():
    history = [msg(60 * 30, "cancel", 0.9), msg(60, "booking", 0.9), msg(5)]
    assert r.routed_intent(history) == "booking"


def test_an_intent_from_a_previous_session_does_not_carry_over():
    history = [msg(60 * 30, "booking", 0.95), msg(5)]
    assert r.routed_intent(history) is None


def test_a_confident_whitelisted_verdict_routes():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": 0.86})
    assert d == r.Decision("route", "booking")


def test_low_confidence_on_the_first_message_sends_the_menu():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": 0.4})
    assert d == r.Decision("menu", None)


def test_low_confidence_again_after_the_menu_goes_to_a_human():
    history = [msg(10), msg(0)]        # two turns spent
    d = r.decide(history=history, verdict={"intent": "booking", "confidence": 0.4})
    assert d == r.Decision("human", None)


def test_an_intent_outside_the_whitelist_goes_straight_to_a_human():
    # Confident, and precisely because it is confident we know it is not ours.
    d = r.decide(history=[msg(0)], verdict={"intent": "complaint", "confidence": 0.99})
    assert d == r.Decision("human", "complaint")


def test_a_button_tap_routes_with_no_verdict_at_all():
    assert r.decide(history=[msg(0)], button_id="booking") == r.Decision("route", "booking")


def test_an_unknown_button_id_is_not_trusted():
    assert r.decide(history=[msg(0)], button_id="../admin") == r.Decision("human", None)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_routing.py -v`
Expected: FAIL — `ModuleNotFoundError: booking_engine.services.messaging.wa_routing`.

- [ ] **Step 3: Implement**

```python
"""Naming a WhatsApp request — a phase of the conversation, not a property of
each message.

Pure by design: no clock, no database, no Meta. The awkward parts here are the
session boundary and the turn cap, and both are exactly the kind of thing that
is impossible to reason about once it is tangled with IO.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NamedTuple

# The set a handler may act on. Everything else — price, complaint, promo_reply,
# opt_out, other — is a human's. Routing is an explicit allowlist, never the
# absence of a red flag.
WHITELIST = ("booking", "reschedule", "cancel", "hours")

ROUTING_CONFIDENCE = 0.7

# One call on the opening message; if that misses, the button menu goes out and
# the model gets one more try. Two rather than three because the menu sits
# between the attempts — a second blind call on a conversation the model already
# failed once is what the menu exists to replace.
MAX_ROUTING_TURNS = 2

# The same boundary as Meta's service window (§2), reused rather than reinvented:
# a customer writing again after a longer silence has a new request.
SESSION_GAP = timedelta(hours=24)


class Decision(NamedTuple):
    action: str            # 'route' | 'menu' | 'human'
    intent: str | None


def session_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tail of `history` since the last gap longer than SESSION_GAP.

    `history` is ascending by received_at — oldest first.
    """
    if not history:
        return []
    start = 0
    for i in range(1, len(history)):
        if history[i]["received_at"] - history[i - 1]["received_at"] > SESSION_GAP:
            start = i
    return history[start:]


def routed_intent(history: list[dict[str, Any]]) -> str | None:
    """The session's verdict, derived rather than stored — the latest non-NULL
    intent inside the current session. A verdict from a previous session is
    deliberately invisible."""
    for m in reversed(session_messages(history)):
        if m.get("intent"):
            return str(m["intent"])
    return None


def decide(
    *,
    history: list[dict[str, Any]],
    verdict: dict[str, Any] | None = None,
    button_id: str | None = None,
) -> Decision:
    """What to do with the message that just arrived.

    `history` includes that message. `verdict` is the classifier's answer, or
    None when it was not consulted (a button tap).
    """
    # A tap is an id we defined, so it needs no model — but it is still input
    # from outside, so it is checked against the whitelist rather than trusted.
    if button_id is not None:
        return Decision("route", button_id) if button_id in WHITELIST \
            else Decision("human", None)

    if verdict is None:
        return Decision("human", None)

    intent = str(verdict.get("intent") or "")
    confidence = float(verdict.get("confidence") or 0.0)

    if intent in WHITELIST and confidence >= ROUTING_CONFIDENCE:
        return Decision("route", intent)

    # Confident about something that is not ours: a human, and we know why.
    if intent and intent not in WHITELIST and confidence >= ROUTING_CONFIDENCE:
        return Decision("human", intent)

    # Not confident. One menu, then a human — never a third guess.
    if len(session_messages(history)) < MAX_ROUTING_TURNS:
        return Decision("menu", None)
    return Decision("human", None)
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_wa_routing.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/wa_routing.py tests/booking_engine/test_wa_routing.py
git commit -m "feat(whatsapp): session-scoped routing, as a pure decision"
```

---

## Task 4: The `allowNonZdr` opt-out (marketing-engine)

**Files:**
- Modify: `src/lib/llm/client.ts`
- Test: `tests/lib/llm-client-zdr.test.ts`

> Tests live in `tests/`, not beside the source. Jest's `testMatch` is
> `**/tests/**/*.test.ts`, so a test under `src/` never runs — and `tsconfig`'s
> `include: ["src"]` would compile it into `dist/`. Applies to every
> marketing-engine task in this plan.
>
> The public entry point is **`llm(): Anthropic`**, a memoised SDK client with
> `provider` injected by a custom transport `fetch`. There is no `askLLM`.

- [ ] **Step 1: Write the failing tests**

```ts
it('sends zdr: true by default', async () => {
  const body = await captureRequestBody(() => askLLM({ model: 'z-ai/glm-5.3-flash', messages: [] }))
  expect(body.provider.zdr).toBe(true)
})

it('omits zdr only when a caller names itself as an exception', async () => {
  const body = await captureRequestBody(() =>
    askLLM({ model: 'typesafe/jev-1.13', messages: [], allowNonZdr: true }))
  expect(body.provider.zdr).toBeUndefined()
  expect(body.provider.data_collection).toBe('deny')   // the other control stays
})
```

- [ ] **Step 2: Run them and watch the second fail**

Run: `npx jest src/lib/llm/client.zdr.test.ts`
Expected: the first passes, the second fails — `zdr` is still `true`.

- [ ] **Step 3: Implement**

```ts
/**
 * ZDR is on for every request (2026-09-16). One caller is exempt: the WhatsApp
 * routing classifier, whose model has no ZDR endpoint and which the owner
 * waived on 2026-09-21 — see the design doc §6.4.
 *
 * Deliberately a named per-call argument and not a config value: the exception
 * is visible at the one call site that takes it, `grep -rn allowNonZdr src`
 * finds every one, and adding a second is an edit somebody reviews rather than
 * a default somebody inherits. `data_collection: 'deny'` is NOT waived — it is
 * a separate control and still refuses any provider that may retain the payload.
 */
function providerConfig(model: unknown, allowNonZdr = false): Record<string, unknown> {
  const only = typeof model === 'string' ? PROVIDERS_BY_MODEL[model] : undefined
  return {
    data_collection: 'deny',
    ...(allowNonZdr ? {} : { zdr: true }),
    ...(only ? { only: [...only] } : {}),
  }
}
```

Thread `allowNonZdr?: boolean` through `askLLM`'s options into that call.

- [ ] **Step 4: Run them and watch them pass**

Run: `npx jest src/lib/llm/client.zdr.test.ts && npx tsc --noEmit`
Expected: 2 passed, tsc exits 0.

- [ ] **Step 5: Pin that nobody else took the exception**

```ts
it('no route other than whatsapp-triage passes allowNonZdr', () => {
  const hits = execSync("grep -rln 'allowNonZdr' src --include='*.ts'")
    .toString().trim().split('\n')
    .filter((f) => !f.includes('llm/client') && !f.includes('.test.'))
  expect(hits).toEqual(['src/routes/whatsapp-triage.ts'])
})
```

This is the test that makes the waiver stay a waiver. Without it, `allowNonZdr` becomes ambient over six months.

- [ ] **Step 6: Commit**

```bash
git add src/lib/llm/client.ts src/lib/llm/client.zdr.test.ts
git commit -m "feat(llm): named per-call ZDR exception, pinned to one caller"
```

---

## Task 5: The triage route (marketing-engine)

**Files:**
- Create: `src/routes/whatsapp-triage.ts`
- Test: `tests/routes/whatsapp-triage.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
it('returns intent, confidence and summary', async () => {
  mockLLM({ intent: 'booking', confidence: 0.86, summary: 'Chiede sabato' })
  const res = await request(app).post('/whatsapp/triage')
    .set('Authorization', `Bearer ${process.env.MARKET_INTEL_SECRET}`)
    .send({ shop_id: SHOP, text: 'ciao volevo prenotare per sabato' })
  expect(res.status).toBe(200)
  expect(res.body.data).toEqual({ intent: 'booking', confidence: 0.86, summary: 'Chiede sabato' })
})

it('sends only the message text to the model', async () => {
  const seen = mockLLM({ intent: 'hours', confidence: 0.9, summary: '' })
  await post({ shop_id: SHOP, text: 'a che ora aprite?', customer_name: 'Maria' })
  const prompt = JSON.stringify(seen.messages)
  expect(prompt).toContain('a che ora aprite?')
  expect(prompt).not.toContain('Maria')      // no customer record reaches it
  expect(prompt).not.toContain(SHOP)
})

it('degrades to other/0 rather than throwing when the model returns junk', async () => {
  mockLLMRaw('not json at all')
  const res = await post({ shop_id: SHOP, text: 'ciao' })
  expect(res.body.data).toEqual({ intent: 'other', confidence: 0, summary: '' })
})

it('rejects an unauthenticated call', async () => {
  const res = await request(app).post('/whatsapp/triage').send({ shop_id: SHOP, text: 'x' })
  expect(res.status).toBe(401)
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `npx jest tests/routes/whatsapp-triage.test.ts`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Implement**

```ts
// Names a WhatsApp request, once, in the opening turns of a session. The
// payload is the customer's message and nothing else — no customer record, no
// phone, no history, no catalogue. That keeps the prompt cheap AND keeps what
// crosses to a non-ZDR provider to the minimum; the two reasons hold each
// other up, so do not add "just one" grounding field here.
const MODEL = 'typesafe/jev-1.13'
const INTENTS = ['booking', 'reschedule', 'cancel', 'hours',
                 'price', 'complaint', 'promo_reply', 'opt_out', 'other'] as const

const SYSTEM = `Sei un classificatore. Leggi il messaggio di un cliente a un parrucchiere e rispondi SOLO con JSON:
{"intent": uno di ${INTENTS.join('|')}, "confidence": 0..1, "summary": massimo 12 parole in italiano}
Non inventare servizi, prezzi o disponibilità. Se il messaggio è ambiguo usa una confidence bassa: è corretto ammettere di non aver capito.`

router.post('/whatsapp/triage', requireSecret, async (req, res) => {
  const text = String(req.body?.text ?? '').slice(0, 2000)
  if (!text) return res.json({ data: { intent: 'other', confidence: 0, summary: '' } })

  // INLINE, never stashed in a module-level const and never exported.
  //
  // llm() returns a MEMOISED Anthropic client and the waiver rides on the
  // client, not on the call — so a stashed `const c = llm({ allowNonZdr: true })`
  // would widen the exemption to every later call through `c` without
  // `allowNonZdr` appearing a second time anywhere, which is precisely what the
  // grep guard cannot see. Calling inline is what keeps the waiver the size it
  // was granted at.
  const out = await llm({ allowNonZdr: true }).messages.create({
    model: MODEL,
    system: SYSTEM,
    messages: [{ role: 'user', content: text }],
    max_tokens: 200,
  })

  // A classifier that throws takes the whole inbound task with it. Junk in
  // means unrouted, which means a human, which is the safe direction.
  let parsed: { intent?: string; confidence?: number; summary?: string } = {}
  try { parsed = JSON.parse(out.text) } catch { /* falls through to other/0 */ }

  const intent = INTENTS.includes(parsed.intent as never) ? parsed.intent! : 'other'
  const confidence = Number.isFinite(parsed.confidence)
    ? Math.max(0, Math.min(1, Number(parsed.confidence))) : 0

  await recordUsage({ shopId: req.body.shop_id, surface: 'whatsapp_triage', usage: out.usage })
  return res.json({ data: { intent, confidence, summary: String(parsed.summary ?? '').slice(0, 200) } })
})
```

- [ ] **Step 4: Run them and watch them pass**

Run: `npx jest tests/routes/whatsapp-triage.test.ts && npx tsc --noEmit`
Expected: 4 passed, tsc exits 0.

- [ ] **Step 5: Commit**

```bash
git add src/routes/whatsapp-triage.ts tests/routes/whatsapp-triage.test.ts
git commit -m "feat(whatsapp): routing classifier, message text and nothing else"
```

---

## Task 6: The triage client (voice-booking)

**Files:**
- Create: `booking_engine/clients/marketing_triage.py`
- Test: `tests/booking_engine/test_marketing_triage.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_posts_the_text_with_the_shared_bearer(monkeypatch, settings):
    seen = {}
    monkeypatch.setattr(triage, "_post", fake_post(seen, {"data": {"intent": "booking", "confidence": 0.9, "summary": "s"}}))
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out == {"intent": "booking", "confidence": 0.9, "summary": "s"}
    assert seen["headers"]["Authorization"] == f"Bearer {settings.market_intel_secret}"
    assert seen["path"].endswith("/whatsapp/triage")


@pytest.mark.asyncio
async def test_unconfigured_returns_none_and_does_not_guess(settings_without_url):
    assert await triage.classify(shop_id=SHOP, text="ciao", settings=settings_without_url) is None


@pytest.mark.asyncio
async def test_a_402_is_a_refusal_not_a_crash(monkeypatch, settings):
    monkeypatch.setattr(triage, "_post", raising_status(402))
    assert await triage.classify(shop_id=SHOP, text="ciao", settings=settings) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_marketing_triage.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
"""Classify an inbound WhatsApp message via the marketing-engine gateway.

Returns None for every failure — unconfigured, empty basket, engine down,
malformed answer. None means "unrouted", which means a human looks at it. There
is no path here that invents a verdict, because a wrong verdict routes a real
customer to the wrong handler and nobody finds out.
"""
TIMEOUT_SECONDS = 15.0


async def classify(*, shop_id: UUID, text: str, settings) -> dict | None:
    base = (settings.market_intel_api_url or "").rstrip("/")
    secret = settings.market_intel_secret or ""
    if not base or not secret:
        logger.warning("whatsapp.triage_unconfigured shop=%s", shop_id)
        return None
    try:
        data = await _post(
            f"{base}/whatsapp/triage",
            headers={"Authorization": f"Bearer {secret}"},
            json={"shop_id": str(shop_id), "text": text},
            timeout=TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — every failure is the same refusal
        logger.exception("whatsapp.triage_failed shop=%s", shop_id)
        return None
    verdict = (data or {}).get("data")
    return verdict if isinstance(verdict, dict) else None
```

Add `market_intel_api_url` to `booking_engine/config.py` beside the existing `market_intel_secret`.

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_marketing_triage.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/clients/marketing_triage.py booking_engine/config.py tests/booking_engine/test_marketing_triage.py
git commit -m "feat(whatsapp): triage client, failing closed to 'a human looks at it'"
```

---

## Task 7: Webhook — dedup, echoes, interactive

**Files:**
- Modify: `booking_engine/api/routes/whatsapp.py:580-640` (`_handle_change`)
- Modify: `booking_engine/db/whatsapp_queries.py` (`record_inbound`, new `record_echo`)
- Test: `tests/booking_engine/test_whatsapp_webhook_inbound.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_a_replayed_webhook_does_not_duplicate_the_message(db):
    payload = inbound_payload(wa_id="wamid.A", body="ciao")
    await post_webhook(payload); await post_webhook(payload)
    assert await count_inbound(wa_id="wamid.A") == 1


@pytest.mark.asyncio
async def test_an_echo_is_recorded_as_outbound_from_the_phone(db):
    await post_webhook(echo_payload(to="+393331112223", body="certo, alle 15"))
    row = await latest_outbound()
    assert row["origin"] == "phone"
    assert row["preview"] == "certo, alle 15"
    assert row["template_name"] is None


@pytest.mark.asyncio
async def test_an_echo_does_not_extend_the_service_window(db):
    # The window is driven by customer inbound alone. An echo that extended it
    # would let us answer a conversation Meta considers closed.
    await insert_inbound(received_at=hours_ago(23))
    await post_webhook(echo_payload())
    assert await window_expires_in(SHOP, PHONE) == pytest.approx(1.0, abs=0.1)


@pytest.mark.asyncio
async def test_an_echo_clears_the_unread_state(db):
    await insert_inbound(read_at=None)
    await post_webhook(echo_payload())
    assert await unread_count(SHOP, PHONE) == 0


@pytest.mark.asyncio
async def test_a_button_tap_is_stored_with_its_id_as_the_intent(db):
    await post_webhook(interactive_payload(button_id="booking"))
    row = await latest_inbound()
    assert row["intent"] == "booking"
    assert row["body"] == "Prenotare"     # the title, for the thread view
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_whatsapp_webhook_inbound.py -v`
Expected: 5 FAIL.

- [ ] **Step 3: Implement**

In `_handle_change`, before the existing `if field != "messages"` guard:

```python
    if field == "message_echoes":
        # Coexistence: the owner answers from the WhatsApp Business App and Meta
        # reports it here. Recorded so the thread is not a half-conversation and
        # the owner is not asked to answer something they already answered.
        #
        # Deliberately does NOT touch the 24h window: that is driven by customer
        # inbound alone, and an echo that extended it would let us send into a
        # conversation Meta considers closed.
        for echo in value.get("message_echoes") or []:
            await wq.record_echo(
                shop_id=sender["shop_id"],
                to_phone=str(echo.get("to") or ""),
                body=_message_text(echo),
                wa_message_id=str(echo.get("id") or ""),
            )
        return
```

And in the `messages` loop, replacing the current `record_inbound` call:

```python
    for message in value.get("messages") or []:
        button_id, text = _interactive_or_text(message)
        row = await wq.record_inbound(
            shop_id=sender["shop_id"],
            from_phone=str(message.get("from") or ""),
            body=text,
            message_type=str(message.get("type") or "text"),
            wa_message_id=str(message.get("id") or ""),
            # A tap is already named. Storing it here means the background task
            # sees a routed session and never calls the classifier at all.
            intent=button_id,
            confidence=1.0 if button_id else None,
        )
        if row is None:          # replayed webhook, already have it
            continue
        schedule_inbound_task(sender, row)
```

```python
def _interactive_or_text(message: dict) -> tuple[str | None, str]:
    """(button_id, display_text). button_id is None for anything typed."""
    if message.get("type") == "interactive":
        inter = message.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return (str(reply.get("id") or "") or None, str(reply.get("title") or ""))
    text = message.get("text")
    return None, str(text.get("body") if isinstance(text, dict) else (text or ""))
```

`record_inbound` gains an `ON CONFLICT … DO NOTHING RETURNING *`, so a replay
returns no row and the caller skips the work — the dedup and the "have we
already processed this" question are the same question, answered once.

**The predicate is not optional.** `inbound_messages_wa_id_uniq` is a *partial*
index, so the `ON CONFLICT` clause must repeat its `WHERE` or Postgres cannot
infer it and the statement fails outright with *"no unique or exclusion
constraint matching the ON CONFLICT specification"*:

```sql
INSERT INTO whatsapp.inbound_messages (...) VALUES (...)
ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL DO NOTHING
RETURNING *
```

Verified against a real Postgres while applying migration 24 (Task 1), not
assumed. This repo has been bitten by exactly this before — see the CLAUDE.md
entries for 2026-07-18 and 2026-07-21, both of which were the same inference
failure against an index whose shape did not match the clause.

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_whatsapp_webhook_inbound.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/whatsapp.py booking_engine/db/whatsapp_queries.py tests/booking_engine/test_whatsapp_webhook_inbound.py
git commit -m "feat(whatsapp): dedup inbound, record phone echoes, read button taps"
```

---

## Task 8: The inbound background task

**Files:**
- Create: `booking_engine/services/messaging/wa_inbound.py`
- Test: `tests/booking_engine/test_wa_inbound.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_the_task_is_held_so_it_cannot_be_collected_mid_flight():
    # asyncio keeps only a weak reference to a bare create_task. The call
    # supervisor shipped exactly this bug on 2026-07-21 and it came back as
    # intermittent silence, which is the hardest kind to see.
    wa_inbound.schedule(sender, row)
    assert len(wa_inbound._TASKS) == 1
    await asyncio.sleep(0)
    await asyncio.gather(*wa_inbound._TASKS)
    assert len(wa_inbound._TASKS) == 0


@pytest.mark.asyncio
async def test_an_audio_message_is_transcribed_into_transcript_not_body(db, fake_meta, fake_stt):
    fake_stt.returns("vorrei prenotare per sabato")
    await wa_inbound.process(sender, audio_row(body=""))
    row = await get_inbound(audio_row_id)
    assert row["transcript"] == "vorrei prenotare per sabato"
    assert row["body"] == ""            # the raw fact stays true


@pytest.mark.asyncio
async def test_the_transcript_is_what_gets_classified(db, fake_meta, fake_stt, fake_triage):
    fake_stt.returns("volevo disdire")
    await wa_inbound.process(sender, audio_row(body=""))
    assert fake_triage.last_text == "volevo disdire"


@pytest.mark.asyncio
async def test_an_empty_basket_leaves_raw_text_and_no_verdict(db, fake_triage_402):
    await wa_inbound.process(sender, text_row("ciao"))
    row = await get_inbound(text_row_id)
    assert row["intent"] is None        # unrouted -> a human sees it


@pytest.mark.asyncio
async def test_a_routed_session_never_calls_the_classifier_again(db, fake_triage):
    await insert_inbound(intent="booking", confidence=0.9, received_at=minutes_ago(5))
    await wa_inbound.process(sender, text_row("e per il colore?"))
    assert fake_triage.calls == 0


@pytest.mark.asyncio
async def test_low_confidence_sends_the_button_menu(db, fake_triage_low, fake_meta):
    await wa_inbound.process(sender, text_row("boh"))
    assert fake_meta.last_interactive["buttons"] == [
        ("booking", "Prenotare"), ("reschedule", "Spostare o disdire"), ("other", "Altro")]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_inbound.py -v`
Expected: 6 FAIL.

- [ ] **Step 3: Implement**

```python
"""What happens to an inbound WhatsApp message after the webhook has answered
200 and gone.

The webhook must answer fast and must never fail, so everything slow —
downloading media, transcription, the classifier, the agent — lives here,
behind a fire-and-forget task.
"""
# asyncio holds only a WEAK reference to a task, so a bare create_task can be
# collected mid-flight. That bug shipped once (call supervisor, 2026-07-21) and
# presented as intermittent silence. The set is what stops it recurring.
_TASKS: set[asyncio.Task] = set()

MENU_BODY = "Non ho capito bene, cosa ti serve?"
MENU_BUTTONS = [
    ("booking", "Prenotare"),
    ("reschedule", "Spostare o disdire"),
    ("other", "Altro"),
]


def schedule(sender: dict, row: dict) -> None:
    task = asyncio.create_task(process(sender, row))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def process(sender: dict, row: dict) -> None:
    try:
        text = row.get("body") or ""
        if row.get("message_type") == "audio":
            text = await _transcribe(sender, row) or text

        history = await tq.inbound_history(sender["shop_id"], row["from_phone"])
        if wa_routing.routed_intent(history):
            return                      # named already; the handler owns it

        # A tap carries its own verdict, stored by the webhook — no model call.
        button_id = row["intent"] if row.get("confidence") == 1.0 else None
        verdict = None if button_id else await triage.classify(
            shop_id=sender["shop_id"], text=text, settings=get_settings())

        decision = wa_routing.decide(history=history, verdict=verdict, button_id=button_id)

        if verdict:
            await tq.set_verdict(row["id"], verdict, decision)

        if decision.action == "menu":
            await meta.send_interactive(
                phone_number_id=sender["phone_number_id"], to=row["from_phone"],
                body=MENU_BODY, buttons=MENU_BUTTONS,
                token=decrypt(sender["access_token"]))
        elif decision.action == "route":
            # Phase A: nothing downstream yet, the thread simply stops needing
            # attention. Phase B (Task 21) makes this the agent's entry point.
            await wa_agent.on_inbound(sender, row, intent=decision.intent)
    except Exception:  # noqa: BLE001 — one bad message must not kill the worker
        logger.exception("whatsapp.inbound_task_failed shop=%s id=%s",
                         sender["shop_id"], row.get("id"))
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_wa_inbound.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/wa_inbound.py tests/booking_engine/test_wa_inbound.py
git commit -m "feat(whatsapp): the inbound worker - transcribe, name, route"
```

---

## Task 9: Transcription

**Files:**
- Create: `booking_engine/services/messaging/wa_transcribe.py`
- Test: `tests/booking_engine/test_wa_transcribe.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_transcribe.py -v`
Expected: 3 FAIL.

- [ ] **Step 3: Implement**

```python
"""Voice notes, which on this vertical are not an edge case: customers send
"vorrei fare il colore come l'altra volta" as audio far more often than typed.

Transcription lives here and not in marketing-engine because the audio bytes
need the salon's business token, which this repo holds; shipping the bytes
across to be transcribed elsewhere would move a secret to move a payload.
"""
# Charged before the work, refused on 402: an empty basket means the raw
# message stays in the owner's queue rather than a transcript nobody paid for.
#
# `charge` is a new generic helper on the EXISTING booking_engine/clients/
# webapp_credits.py, beside record_voice_debit and try_debit_for_message. It
# POSTs the same charge-actual contract those two already use — run_type,
# run_ref, credits — and returns False on 402. Do not add a second HTTP client.
async def transcribe(*, shop_id: UUID, audio: bytes, run_ref: str) -> str | None:
    if not await webapp_credits.charge(shop_id=shop_id, run_type="whatsapp_transcribe",
                                       run_ref=run_ref, credits=TRANSCRIBE_CREDITS):
        logger.info("whatsapp.transcribe_refused shop=%s", shop_id)
        return None
    try:
        return (await _openai_transcribe(audio)).strip() or None
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp.transcribe_failed shop=%s", shop_id)
        return None
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_wa_transcribe.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/wa_transcribe.py tests/booking_engine/test_wa_transcribe.py
git commit -m "feat(whatsapp): transcribe voice notes, refused on an empty basket"
```

---

## Task 10: Threads — queries and the window

**Files:**
- Create: `booking_engine/db/whatsapp_thread_queries.py`
- Test: `tests/live_db/test_wa_threads.py` + `tests/booking_engine/test_wa_threads_unit.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_window_is_measured_from_the_last_customer_message():
    assert th.window_expires_at(last_inbound=T(12, 0)) == T(12, 0) + timedelta(hours=24)


def test_a_thread_with_no_inbound_has_no_window():
    assert th.window_expires_at(last_inbound=None) is None


def test_the_window_is_open_strictly_inside_24h():
    assert th.window_open(last_inbound=NOW - timedelta(hours=23, minutes=59), now=NOW)
    assert not th.window_open(last_inbound=NOW - timedelta(hours=24, seconds=1), now=NOW)


def test_needs_attention_when_the_session_is_unrouted():
    assert th.needs_attention({"intent": None, "escalated": False})


def test_needs_attention_when_the_intent_is_outside_the_whitelist():
    assert th.needs_attention({"intent": "complaint", "escalated": False})


def test_a_routed_booking_does_not_need_attention():
    assert not th.needs_attention({"intent": "booking", "escalated": False})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_threads_unit.py -v`
Expected: 6 FAIL.

- [ ] **Step 3: Implement the pure helpers, then the SQL**

```python
SERVICE_WINDOW = timedelta(hours=24)


def window_expires_at(*, last_inbound: datetime | None) -> datetime | None:
    """Meta permits free-form messages for 24h after the customer's LAST
    message. Our own sends do not extend it — only theirs."""
    return None if last_inbound is None else last_inbound + SERVICE_WINDOW


def window_open(*, last_inbound: datetime | None, now: datetime) -> bool:
    expires = window_expires_at(last_inbound=last_inbound)
    return expires is not None and now < expires


def needs_attention(thread: dict) -> bool:
    """What puts a thread in 'Da gestire'. The owner should open the Inbox and
    see only this."""
    return (thread.get("escalated") is True
            or thread.get("intent") not in wa_routing.WHITELIST)
```

Thread list SQL — one query, both directions, one pass:

```sql
WITH last_in AS (
  SELECT from_phone AS phone, max(received_at) AS last_inbound,
         count(*) FILTER (WHERE read_at IS NULL) AS unread
    FROM whatsapp.inbound_messages WHERE shop_id = $1 GROUP BY from_phone
), last_out AS (
  SELECT to_phone AS phone, max(sent_at) AS last_outbound
    FROM whatsapp.outbound_messages WHERE shop_id = $1 GROUP BY to_phone
)
SELECT li.phone, li.last_inbound, li.unread, lo.last_outbound,
       li.last_inbound + interval '24 hours' AS window_expires_at
  FROM last_in li LEFT JOIN last_out lo USING (phone)
 ORDER BY li.last_inbound DESC;
```

- [ ] **Step 4: Run the unit tests, then the query against a scratch DB with real rows**

```bash
python -m pytest tests/booking_engine/test_wa_threads_unit.py -v
psql wa_scratch -f /tmp/thread_list_check.sql
```

Expected: 6 passed. The SQL check must show: a phone with unread inbound appears with the right count; a phone with only outbound does *not* appear (no window, nothing to answer); an echo does not move `window_expires_at`. Run it, do not assume it — every non-trivial statement in this repo is executed against real rows before it ships.

- [ ] **Step 5: Add the two functions Task 8 calls**

`wa_inbound.process` needs both of these and neither exists yet — define them
here, in the same module, rather than discovering the gap mid-task:

```python
async def inbound_history(shop_id: UUID, phone: str) -> list[dict]:
    """Ascending by received_at — oldest first, which is what
    wa_routing.session_messages expects. Bounded: a session cannot span more
    than 24h of silence, so a month of history is never relevant."""
    return await fetch("""
        SELECT id, received_at, intent, confidence
          FROM whatsapp.inbound_messages
         WHERE shop_id = $1 AND from_phone = $2
           AND received_at > now() - interval '7 days'
         ORDER BY received_at ASC
    """, shop_id, phone)


async def set_verdict(message_id: UUID, verdict: dict, decision) -> None:
    """The classifier's answer, stored on the message that triggered it.

    `intent` is written only when the decision actually routed or named a
    human's reason — a 'menu' decision leaves it NULL, because the session is
    still unrouted and routed_intent() must keep saying so.
    """
    await execute("""
        UPDATE whatsapp.inbound_messages
           SET intent = $2, confidence = $3, summary = $4
         WHERE id = $1
    """, message_id,
        decision.intent if decision.action in ("route", "human") else None,
        verdict.get("confidence"), verdict.get("summary"))
```

- [ ] **Step 6: Commit**

```bash
git add booking_engine/db/whatsapp_thread_queries.py tests/booking_engine/test_wa_threads_unit.py
git commit -m "feat(whatsapp): thread list, and the window as a pure function"
```

---

## Task 11: Thread endpoints

**Files:**
- Modify: `booking_engine/api/routes/whatsapp.py`
- Test: `tests/booking_engine/test_wa_thread_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_reading_a_thread_marks_it_read(client, db):
    await client.get(f"/api/v1/whatsapp/threads/{SHOP}/{PHONE}", headers=AUTH)
    assert await unread_count(SHOP, PHONE) == 0


@pytest.mark.asyncio
async def test_the_timeline_merges_both_directions_in_time_order(client, db):
    body = (await client.get(f"/api/v1/whatsapp/threads/{SHOP}/{PHONE}", headers=AUTH)).json()
    assert [m["direction"] for m in body["data"]["messages"]] == ["in", "out", "in"]


@pytest.mark.asyncio
async def test_a_reply_outside_the_window_is_refused_before_any_graph_call(client, db, fake_meta):
    await insert_inbound(received_at=hours_ago(25))
    res = await client.post("/api/v1/whatsapp/reply", headers=AUTH,
                            json={"shop_id": SHOP, "phone": PHONE, "body": "ciao"})
    assert res.json() == {"ok": False, "error": "session_window_closed"}
    assert fake_meta.sends == []        # never handed to Meta to be rejected


@pytest.mark.asyncio
async def test_a_reply_inside_the_window_is_sent_and_recorded(client, db, fake_meta):
    await insert_inbound(received_at=hours_ago(1))
    res = await client.post("/api/v1/whatsapp/reply", headers=AUTH,
                            json={"shop_id": SHOP, "phone": PHONE, "body": "certo!"})
    assert res.json()["data"]["sent"] is True
    row = await latest_outbound()
    assert row["origin"] == "kairo" and row["template_name"] is None and row["campaign_key"] is None


@pytest.mark.asyncio
async def test_a_reply_never_debits_the_basket(client, db, fake_meta, fake_credits):
    # Service conversations are free (Meta, 2024-11-01) and the Tech Provider
    # model has no credit line to share. A debit here would bill the salon for
    # something nobody is charging us for.
    await insert_inbound(received_at=hours_ago(1))
    await client.post("/api/v1/whatsapp/reply", headers=AUTH, json=REPLY)
    assert fake_credits.charges == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_thread_routes.py -v`
Expected: 5 FAIL — 404.

- [ ] **Step 3: Implement the three routes**

```python
@router.get("/threads/{shop_id}")
async def threads(shop_id: UUID, _auth=Depends(require_control_plane_token)) -> dict:
    rows = await tq.thread_list(shop_id)
    now = datetime.now(timezone.utc)
    return {"data": [
        {**r,
         "window_open": th.window_open(last_inbound=r["last_inbound"], now=now),
         "needs_attention": th.needs_attention(r)}
        for r in rows
    ]}


@router.get("/threads/{shop_id}/{phone}")
async def thread(shop_id: UUID, phone: str, _auth=Depends(require_control_plane_token)) -> dict:
    """Reading the thread is what marks it read — the two are the same act, and
    a separate endpoint would be one more thing the webapp can forget to call."""
    messages = await tq.thread_timeline(shop_id, phone)
    await tq.mark_read(shop_id, phone)
    return {"data": {"messages": messages}}


@router.post("/reply")
async def reply(body: ReplyRequest, _auth=Depends(require_control_plane_token)) -> dict:
    sender = await wq.get_sender(body.shop_id)
    if not sender or sender["status"] != "online":
        return {"ok": False, "error": "sender_offline"}

    # Before Graph, not after. Meta answers 131047 for a closed window, which
    # arrives as an opaque provider error the owner cannot act on.
    last_inbound = await tq.last_inbound_at(body.shop_id, body.phone)
    if not th.window_open(last_inbound=last_inbound, now=datetime.now(timezone.utc)):
        return {"ok": False, "error": "session_window_closed"}

    sid = await meta.send_text(
        phone_number_id=sender["phone_number_id"], to=body.phone,
        body=body.body, token=decrypt(sender["access_token"]))
    await tq.record_reply(shop_id=body.shop_id, to_phone=body.phone,
                          body=body.body, provider_sid=sid)
    return {"data": {"sent": True, "provider_sid": sid}}
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_wa_thread_routes.py -v`
Expected: 5 passed.

- [ ] **Step 5: Update the API docs**

`docs/knowledge/api/whatsapp.md` gains the three routes. This repo's rule: a change that adds an endpoint updates the matching `docs/knowledge/*.md` **in the same change**, not as a follow-up.

- [ ] **Step 6: Commit**

```bash
git add booking_engine/api/routes/whatsapp.py docs/knowledge/api/whatsapp.md tests/booking_engine/test_wa_thread_routes.py
git commit -m "feat(whatsapp): thread endpoints, window checked before Graph"
```

---

## Task 12: Subject-access includes inbound

**Files:**
- Modify: `booking_engine/db/whatsapp_queries.py::customer_campaign_messages`
- Test: `tests/booking_engine/test_whatsapp_subject_access.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_the_gdpr_artifact_includes_what_the_customer_wrote(db):
    await insert_inbound(customer_id=CUSTOMER, body="vorrei disdire")
    rows = await wq.customer_campaign_messages(shop_id=SHOP, customer_id=CUSTOMER)
    assert any(r["direction"] == "in" and r["body"] == "vorrei disdire" for r in rows)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/booking_engine/test_whatsapp_subject_access.py -v`
Expected: FAIL — only outbound rows come back.

- [ ] **Step 3: Implement**

Inbound bodies are personal data the customer wrote; a subject-access artifact
that omits half the conversation is not one. UNION them into the existing query:

```sql
SELECT 'out' AS direction, o.sent_at AS at, o.preview AS body,
       o.campaign_key, o.template_name, o.status
  FROM whatsapp.outbound_messages o
 WHERE o.shop_id = $1 AND o.customer_id = $2
UNION ALL
SELECT 'in' AS direction, i.received_at AS at,
       coalesce(i.transcript, i.body) AS body,
       NULL, NULL, NULL
  FROM whatsapp.inbound_messages i
 WHERE i.shop_id = $1 AND i.customer_id = $2
 ORDER BY at ASC
```

`coalesce(transcript, body)` so a voice note appears as its words rather than as
an empty row — the transcript is what we actually hold about that person.

- [ ] **Step 4: Run it and watch it pass**

Run: `python -m pytest tests/booking_engine/test_whatsapp_subject_access.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/whatsapp_queries.py tests/booking_engine/test_whatsapp_subject_access.py docs/knowledge/database.md
git commit -m "fix(whatsapp): subject-access covers inbound, not just what we sent"
```

---

## Task 13: Webapp — proxies

**Files:**
- Create: `src/app/api/v1/hair-salon/whatsapp/threads/route.ts`, `.../threads/[phone]/route.ts`, `.../reply/route.ts`
- Test: `src/app/api/v1/hair-salon/whatsapp/threads/route.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
it('scopes the request to the session shop, never the body', async () => {
  const res = await POST(req({ shop_id: SOMEONE_ELSE, phone: '+39', body: 'x' }))
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(`/whatsapp/reply`),
    expect.objectContaining({ body: expect.stringContaining(SESSION_SHOP) }))
})
```

- [ ] **Step 2: Run it and watch it fail**

Run: `npx jest src/app/api/v1/hair-salon/whatsapp/threads`
Expected: FAIL — route missing.

- [ ] **Step 3: Implement**

Follow `src/app/api/v1/hair-salon/whatsapp/status/route.ts` exactly: `getShopId(req)` for the shop, `VOICE_AGENT_SECRET` bearer upstream. A caller-supplied `shop_id` is never trusted — it is the cross-tenant hole this pattern exists to close.

- [ ] **Step 4: Run it and watch it pass**

Run: `npx jest src/app/api/v1/hair-salon/whatsapp/threads && npx tsc --noEmit`
Expected: passed, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add src/app/api/v1/hair-salon/whatsapp/threads src/app/api/v1/hair-salon/whatsapp/reply
git commit -m "feat(whatsapp): thread proxies, shop from the session"
```

---

## Task 14: Webapp — the Inbox becomes WhatsApp

**Files:**
- Create: `src/components/inbox/whatsapp/ThreadList.tsx`, `ThreadView.tsx`
- Modify: `src/components/inbox/tabs/ConversationsTab.tsx`, `src/components/inbox/InboxTabBar.tsx` caller
- Test: `src/components/inbox/whatsapp/ThreadList.test.tsx`

- [ ] **Step 1: Write the failing tests**

```tsx
it('puts threads needing a human above everything else', () => {
  render(<ThreadList threads={[routedBooking, unrouted]} />)
  const sections = screen.getAllByRole('heading')
  expect(sections[0]).toHaveTextContent('Da gestire')
  expect(within(sections[0].parentElement!).getByText('+39 333 111')).toBeVisible()
})

it('disables the reply box outside the window and says why', () => {
  render(<ThreadView thread={{ ...thread, window_open: false }} />)
  expect(screen.getByRole('textbox')).toBeDisabled()
  expect(screen.getByText(/finestra.*chiusa|scritto.*24 ore/i)).toBeVisible()
})

it('marks a reply the owner sent from their phone', () => {
  render(<ThreadView thread={threadWithEcho} />)
  expect(screen.getByLabelText(/dal telefono/i)).toBeVisible()
})

it('shows the transcript for a voice note', () => {
  render(<ThreadView thread={threadWithAudio} />)
  expect(screen.getByText('vorrei prenotare per sabato')).toBeVisible()
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `npx jest src/components/inbox/whatsapp`
Expected: 4 FAIL.

- [ ] **Step 3: Implement**

Two sections, "Da gestire" first. Window countdown chip (green / amber under 4h / grey closed). `📱` badge on `origin === 'phone'`. Poll every 15s with `setInterval` in a `useEffect` — no websocket, there is no realtime infrastructure in this app and one thread list does not justify introducing it.

Then hide the telephony tabs by narrowing the `visible` array `ConversationsTab`'s parent passes to `InboxTabBar`. **Filter, do not delete** — the voice components come back in the next iteration and a deleted file is a rewrite.

- [ ] **Step 4: Run them and watch them pass**

Run: `npx jest src/components/inbox/whatsapp && npx tsc --noEmit`
Expected: 4 passed, tsc 0.

- [ ] **Step 5: The two thread actions (spec §8)**

```tsx
it('offers a service chip matched from what the customer wrote', () => {
  // The match comes from service_catalog_match.py — token overlap, no LLM, no
  // cost. LLM service matching is Phase B, where the catalogue is in the
  // prompt anyway.
  render(<ThreadView thread={{ ...t, matched_services: [{ id: SRV, name: 'Colore' }] }} />)
  expect(screen.getByRole('button', { name: 'Colore' })).toBeVisible()
})

it('offers to create a customer when the number matches nobody', () => {
  render(<ThreadView thread={{ ...t, customer_id: null }} />)
  expect(screen.getByRole('button', { name: /crea cliente/i })).toBeVisible()
})
```

A chip opens the existing booking flow prefilled with customer + service; it
does not book anything itself. "Crea cliente" opens the existing customer
form with the phone filled in. Both reuse what the webapp already has —
an unknown number with no action is a dead-end thread, which is the only
reason these exist in A at all.

- [ ] **Step 6: Add the three locales**

`src/i18n/{it,en,es}.ts`. Copy avoids jargon: *"il cliente non scrive da più di 24 ore, puoi rispondere solo con un messaggio predefinito"*, never "service window".

- [ ] **Step 7: Commit**

```bash
git add src/components/inbox src/i18n
git commit -m "feat(inbox): WhatsApp threads, with what needs a human on top"
```

---

## Task 15: Phase A verification gate

- [ ] **Step 1: The baseline, measured**

**554 passed, 24 skipped** — measured on this branch on 2026-09-21 by running
the suite with the new test files excluded.

Note it is *not* the 535 in CLAUDE.md's 2026-09-04 entry: nineteen tests landed
in the five WhatsApp commits between that entry and this branch point. Quoting
a baseline from a previous entry is how a regression gets absorbed into an
arithmetic that "looks about right".

- [ ] **Step 2: Run the suite**

```bash
python -m pytest tests/ --ignore=tests/live_db --ignore=tests/live_twilio -q
```

Expected: baseline + the new tests, **0 failed**.

- [ ] **Step 3: Both other repos**

```bash
cd ../marketing-engine/kairo-market-intel && npx jest && npx tsc --noEmit
cd ../../webapp && npx tsc --noEmit
```

Expected: all green, tsc exits 0.

- [ ] **Step 4: Commit the docs**

```bash
git add docs/knowledge/
git commit -m "docs(whatsapp): conversations in architecture, database, providers"
```

---

# PHASE B — The booking agent

## Task 16: Close the authz hole first

`update_customer_from_call` has no shop-ownership check — flagged 2026-07-17,
never fixed. It is reachable only with a valid minted token today, so it is not
exposed. Phase B mints those tokens on a second channel. Closing it first is the
cheap order.

**Files:**
- Modify: `booking_engine/api/routes/voice_tools_identity.py`
- Test: `tests/live_db/test_tool_dispatch_security.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_a_token_for_one_shop_cannot_edit_another_shops_customer(db):
    token = mint_call_token(shop_id=SHOP_A, call_id=CALL_A, secret=SECRET)
    out = await execute_tool("update_customer_from_call",
                             {"customer_id": str(CUSTOMER_OF_SHOP_B), "email": "x@y.z"},
                             token=token, secret=SECRET, app=app)
    assert out == {"ok": False, "error": "wrong_shop"}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/live_db/test_tool_dispatch_security.py -k wrong_shop -v`
Expected: FAIL — the update succeeds.

- [ ] **Step 3: Implement**

Read the customer's `shop_id` and compare against the call row's, the same shape `authorize_booking_change` already uses. Return `wrong_shop`, do not raise.

- [ ] **Step 4: Run it and watch it pass**

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/api/routes/voice_tools_identity.py tests/live_db/test_tool_dispatch_security.py
git commit -m "fix(voice): update_customer_from_call checks the shop"
```

---

## Task 17: WhatsApp sessions on `voice_agent.calls`

**Files:**
- Create: `booking_engine/db/wa_session_queries.py`
- Test: `tests/booking_engine/test_wa_session.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_opening_a_session_writes_a_whatsapp_call_row(db):
    call_id = await s.open_session(shop_id=SHOP, phone=PHONE, customer_id=CUSTOMER)
    row = await get_call(call_id)
    assert row["channel"] == "whatsapp"
    assert row["caller_number"] == PHONE
    assert row["duration_seconds"] is None


@pytest.mark.asyncio
async def test_a_second_message_reuses_the_open_session(db):
    a = await s.open_session(shop_id=SHOP, phone=PHONE, customer_id=CUSTOMER)
    b = await s.open_session(shop_id=SHOP, phone=PHONE, customer_id=CUSTOMER)
    assert a == b


@pytest.mark.asyncio
async def test_a_session_past_the_24h_gap_is_a_new_one(db):
    a = await s.open_session(shop_id=SHOP, phone=PHONE, customer_id=CUSTOMER)
    await age_call(a, hours=25)
    assert await s.open_session(shop_id=SHOP, phone=PHONE, customer_id=CUSTOMER) != a


@pytest.mark.asyncio
async def test_the_minted_token_authorises_the_booking_tools(db):
    call_id = await s.open_session(shop_id=SHOP, phone=PHONE, customer_id=CUSTOMER)
    token = mint_call_token(shop_id=SHOP, call_id=call_id, secret=SECRET)
    out = await execute_tool("get_services", {}, token=token, secret=SECRET, app=app)
    assert out["ok"] is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_session.py -v`
Expected: 4 FAIL.

- [ ] **Step 3: Implement**

```python
"""A WhatsApp conversation is a session row, and voice_agent.calls already is
one: shop, caller_number, customer, outcome, summary, appointment.

Nothing about booking, authz or constraints is rebuilt. caller_number here is
the customer's WhatsApp number, which Meta has verified — strictly stronger
evidence than a voice call's caller ID, so authorize_booking_change works
unchanged.
"""
async def open_session(*, shop_id, phone, customer_id) -> UUID:
    existing = await _open_whatsapp_call(shop_id, phone, within=SESSION_GAP)
    if existing:
        return existing["id"]
    return await _insert_call(
        shop_id=shop_id, caller_number=phone, customer_id=customer_id,
        customer_match="existing" if customer_id else "unmatched",
        channel="whatsapp", started_at=now(),
    )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_wa_session.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/wa_session_queries.py tests/booking_engine/test_wa_session.py
git commit -m "feat(whatsapp): a conversation is a session row on voice_agent.calls"
```

---

## Task 18: Per-service intake questions

**Files:**
- Create: `booking_engine/db/service_intake_queries.py`, `src/components/settings/ServiceIntakeField.tsx`
- Test: `tests/booking_engine/test_service_intake.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_questions_are_capped_at_500_characters(db):
    await si.set_questions(shop_id=SHOP, service_id=SRV, questions="x" * 900)
    assert len(await si.get_questions(SHOP, SRV)) == 500


@pytest.mark.asyncio
async def test_only_the_services_in_play_are_returned(db):
    await si.set_questions(shop_id=SHOP, service_id=COLORE, questions="ritocco o completo?")
    await si.set_questions(shop_id=SHOP, service_id=PIEGA, questions="mai")
    assert await si.for_services(SHOP, [COLORE]) == {str(COLORE): "ritocco o completo?"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_service_intake.py -v`
Expected: 2 FAIL.

- [ ] **Step 3: Implement**

```python
# The cap is not arbitrary: this text enters the prompt on every turn of every
# conversation touching that service. An owner who pastes an essay pays for it
# on each one, and the agent's instructions drown in it.
MAX_QUESTIONS_CHARS = 500
```

Webapp: a textarea per service beside the existing tone and greeting settings, with the character counter visible — a limit the owner cannot see is a limit they hit by surprise.

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_service_intake.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/db/service_intake_queries.py src/components/settings/ServiceIntakeField.tsx tests/booking_engine/test_service_intake.py
git commit -m "feat(agent): per-service intake questions the owner writes"
```

---

## Task 19: Catalogue grounding — the rejection layer

The rule is "the agent may only name a service that exists in this shop's
catalogue". Layers 1 and 3 already exist (injection; `create_appointment_chain`
raising `invalid_service`). This is layer 2, the one that makes it checkable.

**Files:**
- Create: `src/lib/whatsapp/grounding.ts`
- Test: `src/lib/whatsapp/grounding.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
it('drops a service_id the catalogue does not contain', () => {
  expect(keepKnownServices([{ service_id: 'ghost' }, { service_id: SRV_A }],
                           [{ id: SRV_A, name: 'Colore' }]))
    .toEqual([{ service_id: SRV_A }])
})

it('refuses the tool call outright when nothing survives', () => {
  expect(() => assertGrounded([{ service_id: 'ghost' }], [{ id: SRV_A, name: 'Colore' }]))
    .toThrow('invalid_service')
})

it('is not fooled by a name that matches when the id does not', () => {
  expect(keepKnownServices([{ service_id: 'ghost', name: 'Colore' }],
                           [{ id: SRV_A, name: 'Colore' }])).toEqual([])
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `npx jest src/lib/whatsapp/grounding.test.ts`
Expected: 3 FAIL.

- [ ] **Step 3: Implement**

```ts
/** Ids only. The third test is the point: a model inventing a plausible NAME
 *  is the common failure, and a name comparison waves it straight through. */
export function keepKnownServices<T extends { service_id: string }>(
  proposed: T[], catalogue: { id: string }[],
): T[] {
  const known = new Set(catalogue.map((s) => s.id))
  return proposed.filter((p) => known.has(p.service_id))
}

export function assertGrounded<T extends { service_id: string }>(
  proposed: T[], catalogue: { id: string }[],
): T[] {
  const kept = keepKnownServices(proposed, catalogue)
  if (proposed.length > 0 && kept.length === 0) throw new Error('invalid_service')
  return kept
}
```

- [ ] **Step 4: Run them and watch them pass**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/lib/whatsapp/grounding.ts src/lib/whatsapp/grounding.test.ts
git commit -m "feat(agent): a service the shop does not sell never reaches a tool"
```

---

## Task 20: The agent tool loop (marketing-engine)

**Files:**
- Create: `src/lib/whatsapp/tools.ts`, `src/lib/whatsapp/agent.ts`, `src/routes/whatsapp-agent.ts`
- Test: `tests/whatsapp/agent.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
it('proxies a tool call to voice-booking with the minted call token', async () => {
  mockLLMToolUse('check_availability', { services: [{ service_id: SRV }] })
  await runTurn({ callToken: TOKEN, messages: [user('sabato?')] })
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/voice/tools/check_availability'),
    expect.objectContaining({ headers: expect.objectContaining({ 'X-Call-Token': TOKEN }) }))
})

it('stops after MAX_AGENT_TOOL_CALLS rather than looping forever', async () => {
  mockLLMAlwaysToolUse('get_services')
  const out = await runTurn({ callToken: TOKEN, messages: [user('ciao')] })
  expect(toolCalls()).toHaveLength(MAX_AGENT_TOOL_CALLS)
  expect(out.escalate).toBe(true)
})

it('escalates instead of answering when a tool refuses twice', async () => {
  mockToolResult({ ok: false, error: 'invalid_service' })
  const out = await runTurn({ callToken: TOKEN, messages: [user('il trattamento X')] })
  expect(out.escalate).toBe(true)
  expect(out.text).toBe('')      // never a guess dressed as an answer
})

it('never sends allowNonZdr — the agent prompt carries customer data', async () => {
  await runTurn({ callToken: TOKEN, messages: [user('ciao')] })
  expect(lastLLMCall().allowNonZdr).toBeUndefined()
})

it('opens with the AI disclosure on the first turn only', async () => {
  const first = await runTurn({ callToken: TOKEN, messages: [user('ciao')], firstTurn: true })
  expect(first.text).toMatch(/assistente/i)
  const second = await runTurn({ callToken: TOKEN, messages: [user('sabato')], firstTurn: false })
  expect(second.text).not.toMatch(/sono l'assistente/i)
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `npx jest tests/whatsapp/agent.test.ts`
Expected: 5 FAIL.

- [ ] **Step 3: Implement**

Follow `src/lib/business-advisor/engine.ts`: loop while `stop_reason === 'tool_use'`, build `tool_result` blocks, stop at the ceiling.

```ts
// The 12 tools of DEFAULT_TOOL_ALLOWLIST, proxied to voice-booking. The voice
// path's MIN_CHECK_LATENCY_SECONDS is NOT applied: that 0.8s floor exists so a
// spoken "un attimo che controllo…" is not followed by a suspiciously instant
// answer. WhatsApp has no filler phrase, so the floor would be latency for
// nothing.
const MAX_AGENT_TOOL_CALLS = 8

// Deliberately a flash model, NOT the routing classifier's. This prompt carries
// the catalogue, the intake questions, the thread and the customer's name —
// §6.4's ZDR waiver is classifier-only and must never reach here.
const AGENT_MODEL = 'deepseek/deepseek-v4.1-flash'

const DISCLOSURE = "Sono l'assistente digitale di {shop}."
```

Escalation returns `{ escalate: true, text: '' }` — an empty text, because the
alternative is the agent apologising in a way that still sounds like an answer.

- [ ] **Step 4: Run them and watch them pass**

Run: `npx jest tests/whatsapp/agent.test.ts && npx tsc --noEmit`
Expected: 5 passed, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add src/lib/whatsapp src/routes/whatsapp-agent.ts tests/whatsapp
git commit -m "feat(agent): the booking turn loop, over the tools that exist"
```

---

## Task 21: Dispatch, debounce and handover

The safety core of Phase B. Every rule here is about who is allowed to speak.

**Files:**
- Create: `booking_engine/services/messaging/wa_agent.py`
- Test: `tests/booking_engine/test_wa_agent.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_three_messages_in_a_row_produce_one_reply(db, fake_agent):
    for text in ("ciao", "volevo prenotare", "per sabato"):
        await agent.on_inbound(sender, row(text))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    assert fake_agent.turns == 1
    assert "per sabato" in fake_agent.last_messages[-1]["content"]


@pytest.mark.asyncio
async def test_an_echo_from_the_owners_phone_suspends_the_agent(db, fake_agent):
    # Coexistence: the owner answered from their phone. The agent talking over
    # them is the failure this whole mechanism exists to prevent.
    await agent.on_echo(sender, echo_row())
    await agent.on_inbound(sender, row("e per il colore?"))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    assert fake_agent.turns == 0


@pytest.mark.asyncio
async def test_a_reply_typed_in_the_webapp_suspends_it_too(db, fake_agent):
    await agent.on_owner_reply(sender, PHONE)
    await agent.on_inbound(sender, row("ok grazie"))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    assert fake_agent.turns == 0


@pytest.mark.asyncio
async def test_the_agent_stands_down_silently_on_an_empty_basket(db, fake_credits_402, fake_meta):
    await agent.on_inbound(sender, row("vorrei prenotare"))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    assert fake_meta.sends == []                 # never half an answer
    assert await needs_attention(SHOP, PHONE)    # the owner gets it instead


@pytest.mark.asyncio
async def test_a_shop_that_has_not_opted_in_is_never_handled(db, fake_agent):
    await set_agent_enabled(SHOP, False)
    await agent.on_inbound(sender, row("vorrei prenotare"))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    assert fake_agent.turns == 0


@pytest.mark.asyncio
async def test_an_escalation_marks_the_session_and_stops_the_agent(db, fake_agent_escalating):
    await agent.on_inbound(sender, row("voglio parlare con qualcuno"))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    call = await get_call_for_thread(SHOP, PHONE)
    assert call["outcome"] == "escalated"
    await agent.on_inbound(sender, row("ci sei?"))
    await asyncio.sleep(DEBOUNCE_SECONDS + 0.1)
    assert fake_agent.turns == 1     # the first one only
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_agent.py -v`
Expected: 6 FAIL.

- [ ] **Step 3: Implement**

```python
"""Who is allowed to speak on a thread.

Three writers exist at once — the customer, the owner (webapp AND the WhatsApp
Business App on their phone), and the agent. Only one of them is ours to
control, so every rule here is about the agent standing down.
"""
# People send "ciao" / "volevo prenotare" / "per sabato" as three messages.
# Answering each is three replies to one thought, and three billed turns.
DEBOUNCE_SECONDS = 2.0


def may_speak(thread: dict) -> tuple[bool, str]:
    """Every reason the agent stays quiet. An allowlist of conditions, so a new
    thread state defaults to silence rather than to speech."""
    if not thread["agent_enabled"]:
        return False, "not_opted_in"
    if thread["intent"] not in wa_routing.WHITELIST:
        return False, "intent_not_whitelisted"
    if thread["escalated"]:
        return False, "escalated"
    if thread["human_replied_at"] is not None:
        return False, "human_took_over"     # echo or webapp, same rule
    return True, "ok"
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/booking_engine/test_wa_agent.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/wa_agent.py tests/booking_engine/test_wa_agent.py
git commit -m "feat(agent): debounce the turn, and every reason to stay quiet"
```

---

## Task 22: Cost ceiling per conversation

**Files:**
- Modify: `booking_engine/services/messaging/wa_agent.py`
- Test: `tests/booking_engine/test_wa_agent_cost.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_each_turn_is_charged_against_the_basket(fake_credits):
    await agent.run_turn(sender, thread)
    assert fake_credits.charged["run_type"] == "whatsapp_turn"


@pytest.mark.asyncio
async def test_the_agent_hands_over_after_MAX_TURNS_rather_than_chatting_forever(db, fake_agent):
    for _ in range(agent.MAX_SESSION_TURNS + 2):
        await agent.run_turn(sender, thread)
    assert fake_agent.turns == agent.MAX_SESSION_TURNS
    assert await needs_attention(SHOP, PHONE)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_agent_cost.py -v`
Expected: 2 FAIL.

- [ ] **Step 3: Implement**

```python
# A booking is four or five exchanges. Twelve means the conversation is not
# going where the agent thinks it is, and the honest move is a human — not
# another turn on the salon's basket.
MAX_SESSION_TURNS = 12
```

- [ ] **Step 4: Run them and watch them pass**

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/wa_agent.py tests/booking_engine/test_wa_agent_cost.py
git commit -m "feat(agent): a conversation has a ceiling, then a person"
```

---

## Task 23: The 20h nudge

**Files:**
- Modify: `booking_engine/api/routes/messaging_tick.py`
- Create: `booking_engine/services/messaging/wa_nudge.py`
- Test: `tests/booking_engine/test_wa_nudge.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_thread_at_20h_with_the_agent_waiting_is_nudged():
    assert nudge.should_nudge(thread(last_inbound_hours_ago=20, agent_waiting=True))


def test_a_thread_already_answered_by_the_customer_is_not():
    assert not nudge.should_nudge(thread(last_inbound_hours_ago=2, agent_waiting=True))


def test_a_thread_is_nudged_at_most_once():
    assert not nudge.should_nudge(thread(last_inbound_hours_ago=21, nudged=True))


def test_a_closed_window_is_never_nudged():
    # Past 24h the nudge is a template, which this is not. Sending anyway is a
    # certain 131047.
    assert not nudge.should_nudge(thread(last_inbound_hours_ago=25, agent_waiting=True))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/booking_engine/test_wa_nudge.py -v`
Expected: 4 FAIL.

- [ ] **Step 3: Implement**

```python
# We cannot speak first after 24h of customer silence — that needs an approved
# template. A last free message inside the window invites the customer to write
# back, and their reply is what reopens it. The cheapest mitigation there is.
NUDGE_AFTER_HOURS = 20
```

Add as a stage on the hourly tick, wrapped in its own try/except so a nudge
failure is counted under `errors` rather than 500-ing the tick — the same shape
the release sweep already uses.

- [ ] **Step 4: Run them and watch them pass**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/services/messaging/wa_nudge.py booking_engine/api/routes/messaging_tick.py tests/booking_engine/test_wa_nudge.py
git commit -m "feat(agent): one nudge inside the window, never after it"
```

---

## Task 24: Opt-in and the handover controls

**Files:**
- Modify: `src/components/inbox/whatsapp/ThreadView.tsx`, `src/components/inbox/tabs/ConfigurationTab.tsx`
- Test: `src/components/inbox/whatsapp/ThreadView.agent.test.tsx`

- [ ] **Step 1: Write the failing tests**

```tsx
it('shows who is handling the thread', () => {
  render(<ThreadView thread={{ ...t, agent_active: true }} />)
  expect(screen.getByText(/assistente attivo/i)).toBeVisible()
})

it('lets the owner take over, and says the agent has stopped', async () => {
  render(<ThreadView thread={{ ...t, agent_active: true }} />)
  await userEvent.click(screen.getByRole('button', { name: /rispondo io/i }))
  expect(screen.getByText(/assistente in pausa/i)).toBeVisible()
})

it('explains why the agent is not handling a thread', () => {
  render(<ThreadView thread={{ ...t, agent_active: false, agent_reason: 'intent_not_whitelisted' }} />)
  expect(screen.getByText(/questa richiesta la gestisci tu/i)).toBeVisible()
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `npx jest src/components/inbox/whatsapp/ThreadView.agent.test.tsx`
Expected: 3 FAIL.

- [ ] **Step 3: Implement**

A single on/off in Configuration, plus a per-thread "Rispondo io" that sets
`human_replied_at`. Every `may_speak` reason gets human copy — an owner who
cannot tell why the agent is quiet will assume it is broken and turn it off.

- [ ] **Step 4: Run them and watch them pass**

Expected: 3 passed, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add src/components/inbox src/i18n
git commit -m "feat(agent): opt-in, takeover, and a reason for every silence"
```

---

## Task 24b: Six months of interactions, and the free eval set

Owner request, 2026-09-21: keep the last six months of interactions so we can do
post-mortems and improve the routing engine from real experience.

**No new table.** The material is already written: `inbound_messages` carries the
verdict (`intent`, `confidence`, `summary`, `transcript`), `outbound_messages`
carries every reply including the owner's from their phone, and `calls` carries
the session outcome. A summary table would be a third copy of two truths and the
first place they would silently disagree.

Three pieces, and only the first writes anything.

**Files:**
- Create: `booking_engine/db/sql/25_whatsapp_retention.sql`
- Create: `booking_engine/services/messaging/wa_retention.py`
- Modify: `booking_engine/api/routes/messaging_tick.py`
- Test: `tests/booking_engine/test_wa_retention.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_message_older_than_six_months_is_due_for_deletion():
    assert ret.is_expired(received_at=NOW - timedelta(days=200), now=NOW)


def test_a_message_inside_the_window_is_kept():
    assert not ret.is_expired(received_at=NOW - timedelta(days=100), now=NOW)


def test_the_boundary_is_inclusive_of_the_last_day():
    assert not ret.is_expired(received_at=NOW - RETENTION, now=NOW)


@pytest.mark.asyncio
async def test_the_sweep_deletes_both_directions(db):
    # A conversation is not half deleted. Keeping our side of a thread whose
    # customer side has expired is the worst of both: still personal data,
    # no longer readable as a conversation.
    await insert_inbound(received_at=days_ago(200))
    await insert_outbound(sent_at=days_ago(200))
    await ret.sweep()
    assert await count_inbound() == 0 and await count_outbound() == 0


@pytest.mark.asyncio
async def test_the_sweep_leaves_the_calls_row_alone(db):
    # voice_agent.calls is the business record of an appointment being made.
    # It outlives the chat that produced it and is not ours to expire here.
    await insert_call(channel="whatsapp", started_at=days_ago(200))
    await ret.sweep()
    assert await count_calls() == 1
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `python -m pytest tests/booking_engine/test_wa_retention.py -v`

- [ ] **Step 3: Implement**

```python
# Owner's number, 2026-09-21: six months of interactions, kept for post-mortems
# and for improving the routing prompt. It is also the first retention policy
# this feature has had — before it, message bodies accumulated forever, which
# the design doc flagged as an open GDPR gap and this closes.
RETENTION = timedelta(days=183)


def is_expired(*, received_at: datetime, now: datetime) -> bool:
    return now - received_at > RETENTION
```

The sweep is a third stage on the hourly tick, in its own try/except so a
failure is counted under `errors` rather than 500-ing the tick — the same shape
`number_release`'s sweep already uses.

- [ ] **Step 4: The view — analysis, not an ETL**

```sql
-- Did the router get it right? The verdict and the outcome live in different
-- tables; the question is a join, not a pipeline. A view cannot drift from the
-- truth, which a summary table would do the first time a backfill was skipped.
CREATE OR REPLACE VIEW whatsapp.interaction_history AS
SELECT i.shop_id, i.from_phone, i.received_at,
       coalesce(i.transcript, i.body) AS text,
       i.intent AS routed_intent, i.confidence, i.summary,
       c.outcome AS session_outcome, c.appointment_id
  FROM whatsapp.inbound_messages i
  LEFT JOIN voice_agent.calls c
    ON c.shop_id = i.shop_id
   AND c.channel = 'whatsapp'
   AND c.caller_number = i.from_phone
   AND i.received_at BETWEEN c.started_at AND coalesce(c.ended_at, now());
```

- [ ] **Step 5: The eval set, which we are already writing**

```sql
-- The disambiguation menu is a labelling machine, and nobody designed it as one.
--
-- When the model is not confident we send buttons; the customer's tap is stored
-- as a verdict with confidence = 1.0. So every low-confidence message followed
-- by a tap is a case the model got wrong PLUS the correct label, supplied by the
-- person who wrote the message. That is a human-verified eval set for the
-- routing prompt, accumulating for free from the day the menu shipped.
CREATE OR REPLACE VIEW whatsapp.routing_corrections AS
SELECT miss.shop_id,
       coalesce(miss.transcript, miss.body) AS message,
       miss.intent      AS model_guessed,
       miss.confidence  AS model_confidence,
       tap.intent       AS customer_meant,
       miss.received_at
  FROM whatsapp.inbound_messages miss
  JOIN LATERAL (
    SELECT t.intent, t.received_at FROM whatsapp.inbound_messages t
     WHERE t.shop_id = miss.shop_id AND t.from_phone = miss.from_phone
       AND t.received_at > miss.received_at
       AND t.confidence = 1.0            -- a tap, not a model verdict
     ORDER BY t.received_at ASC LIMIT 1
  ) tap ON true
 WHERE miss.confidence IS NOT NULL AND miss.confidence < 1.0;
```

Test it against a scratch Postgres with real rows: a low-confidence message
followed by a tap appears; a confident message followed by a tap does not; a
low-confidence message with no tap after it does not.

- [ ] **Step 6: Commit**

```bash
git add booking_engine/db/sql/25_whatsapp_retention.sql \
        booking_engine/services/messaging/wa_retention.py \
        booking_engine/api/routes/messaging_tick.py \
        tests/booking_engine/test_wa_retention.py docs/knowledge/database.md
git commit -m "feat(whatsapp): six-month retention, and the corrections the menu already collects"
```

---

## Task 25: Final gate and the durable record

- [ ] **Step 1: Full suite, all three repos**

```bash
python -m pytest tests/ --ignore=tests/live_db --ignore=tests/live_twilio -q
cd ../marketing-engine/kairo-market-intel && npx jest && npx tsc --noEmit
cd ../../webapp && npx tsc --noEmit
```

Expected: 0 failed everywhere, both `tsc` exit 0.

- [ ] **Step 2: Migration 24 applied twice against a scratch Postgres**

Confirm every column and both `COMMENT`s with `\d`. Exit 0 both passes.

- [ ] **Step 3: Live-ish check of the one thing tests cannot prove**

The `message_echoes` payload shape is the largest unverified assumption in this
plan. On the first real onboarding: send a message from the WhatsApp Business
App on the connected number and confirm an `outbound_messages` row appears with
`origin = 'phone'`. If Meta's shape differs, `_handle_change`'s echo branch is
the only place to change.

- [ ] **Step 4: Write the CLAUDE.md entry**

Newest on top. It must record, at minimum: the A/B split and why B needed A;
that `voice_agent.calls` was already a session table and nothing about booking
was rebuilt; the 24h window resetting on every customer message and what that
does and does not forbid; the echo rule and why it is load-bearing under
coexistence; **the ZDR waiver, the model it applies to, and exactly what
crosses to a non-ZDR provider**; buttons as the disambiguation step rather than
the opening move; the measured test baseline; and that no live Meta call was
made.

- [ ] **Step 5: Delete the working documents**

```bash
git rm docs/2026-09-21-whatsapp-conversations-design.md docs/2026-09-21-whatsapp-conversations-plan.md
git commit -m "docs: fold the WhatsApp conversations design into the history"
```

The CLAUDE.md entry is the durable record; these two files were the scaffolding,
and the repo's convention since 2026-07-24 is that scaffolding does not survive.
