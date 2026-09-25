-- Six months of interactions, and the eval set the product is already writing.
-- Idempotent: migrate.sh re-applies every file.
--
-- **No new table.** The material already exists — `inbound_messages` carries
-- the routing verdict (intent, confidence, summary, transcript),
-- `outbound_messages` every reply including the owner's from their own phone,
-- and `voice_agent.calls` the session outcome. A summary table would be a
-- third copy of two truths and the first place they would quietly disagree
-- (the day a backfill is skipped). Two views instead: a view cannot drift from
-- what it reads.
--
-- The retention policy that bounds both of these lives in
-- `services/messaging/wa_retention.py` (183 days). These views therefore see
-- six months, by construction, which is the intended scope for both a
-- post-mortem and an eval set.


-- ---------------------------------------------------------------------------
-- Did the router get it right? The verdict and the outcome live in different
-- tables; the question is a join, not a pipeline.
--
-- One row per inbound message, carrying what the model made of it and how the
-- session it belongs to ended. LEFT JOIN, deliberately: a message that never
-- reached a session — unrouted, or arriving at a shop with no agent — is
-- exactly the kind this is for, and an inner join would hide it.
--
-- **Session membership is not `received_at >= started_at`, and that trap is
-- the reason this comment is long.** The session row is opened by the worker
-- *after* the message that caused it is already committed (webhook records,
-- worker debounces ~2s, `wa_session_queries.open_session` inserts), so the
-- first message of every conversation — the one carrying the routing verdict,
-- the single most interesting row here — precedes its own session by seconds.
-- The span is therefore read the way `wa_routing.session_messages` reads one:
--
--   * the message must not be after the session finished — `ended_at`, or
--     `started_at + 24h` for a session nothing ever closed;
--   * the session must not have started more than 24h after the message,
--     which is precisely the gap that would make it a *different* request.
--
-- 24 hours is `wa_routing.SESSION_GAP`. A view takes no parameters, so it is
-- written here as a literal and must move if that constant does — the one
-- place in this feature where the two are not bound together by a parameter.
--
-- Phones are matched with the leading '+' stripped on both sides, the rule
-- `whatsapp_thread_queries` already settled on: Meta sends `from` bare while
-- `caller_number` and `to_phone` usually carry the plus.
CREATE OR REPLACE VIEW whatsapp.interaction_history AS
SELECT i.shop_id,
       i.id                                      AS message_id,
       i.received_at,
       i.from_phone,
       i.customer_id,
       i.message_type,
       coalesce(nullif(i.transcript, ''), i.body) AS text,
       i.intent,
       i.confidence,
       i.summary,
       c.id                                      AS call_id,
       c.started_at                              AS session_started_at,
       c.ended_at                                AS session_ended_at,
       c.outcome,
       c.outcome_reason,
       c.appointment_id
  FROM whatsapp.inbound_messages i
  LEFT JOIN LATERAL (
       SELECT s.id, s.started_at, s.ended_at, s.outcome, s.outcome_reason,
              s.appointment_id
         FROM voice_agent.calls s
        WHERE s.shop_id = i.shop_id
          AND s.channel = 'whatsapp'
          AND ltrim(s.caller_number, '+') = ltrim(i.from_phone, '+')
          AND i.received_at < coalesce(s.ended_at,
                                       s.started_at + interval '24 hours')
          AND s.started_at <= i.received_at + interval '24 hours'
        ORDER BY s.started_at DESC
        LIMIT 1
  ) c ON true;

COMMENT ON VIEW whatsapp.interaction_history IS
  'One row per inbound WhatsApp message: the routing verdict beside the '
  'outcome of the session it belongs to. A view, not a summary table, so it '
  'cannot drift from the rows it reads. Bounded to six months by '
  'services/messaging/wa_retention.py.';


-- ---------------------------------------------------------------------------
-- The disambiguation menu is a labelling machine, and nobody designed it as one.
--
-- When the model is not confident we send buttons; the customer's tap comes
-- back as an id we defined and is stored as a verdict with confidence = 1.0
-- (`whatsapp_queries.record_inbound`). So every low-confidence message
-- followed by a tap is a case the model got wrong PLUS the correct label,
-- supplied by the person who wrote the message. That is a human-verified eval
-- set for the routing prompt, accumulating for free from the day the menu
-- shipped, and it costs one view to read.
--
-- **The miss is `intent IS NULL AND confidence IS NOT NULL`**, not a
-- comparison against 0.7. `whatsapp_thread_queries.set_verdict` writes an
-- intent for every decision it routed or named — so a NULL intent with a
-- confidence beside it means the classifier ran and did not name it, which is
-- the menu case exactly. Writing `confidence < 0.7` would copy
-- `wa_routing.ROUTING_CONFIDENCE` into SQL, where it would drift silently the
-- day the threshold moves; this predicate is equivalent and has nothing to
-- keep in sync.
--
-- **The tap must be the very next thing that customer said.** The LATERAL
-- takes one row — the immediately following inbound message — and the pair is
-- kept only if that row is a tap. This is what stops a miss being credited
-- with a label from a different conversation: a customer who goes quiet and
-- writes again three days later types first, and a typed message is not a tap.
-- The 24h bound catches the remaining case, a stale menu button tapped in a
-- later session, since 24h of silence is by definition a new request
-- (`wa_routing.SESSION_GAP`). The cost of being this strict is losing a
-- correction where the customer typed something before tapping; a smaller eval
-- set is cheaper than one with mislabelled rows in it.
--
-- `correct_intent` includes 'other', which routes to a human: "no handler
-- covers this" is a correct label too, and one the model most needs.
CREATE OR REPLACE VIEW whatsapp.routing_corrections AS
SELECT miss.shop_id,
       miss.id                                          AS message_id,
       miss.received_at,
       coalesce(nullif(miss.transcript, ''), miss.body) AS text,
       miss.message_type,
       miss.confidence                                  AS model_confidence,
       miss.summary                                     AS model_summary,
       tap.intent                                       AS correct_intent,
       tap.id                                           AS correction_message_id,
       tap.received_at                                  AS corrected_at
  FROM whatsapp.inbound_messages miss
  JOIN LATERAL (
       SELECT n.id, n.intent, n.confidence, n.received_at
         FROM whatsapp.inbound_messages n
        WHERE n.shop_id = miss.shop_id
          AND ltrim(n.from_phone, '+') = ltrim(miss.from_phone, '+')
          AND n.received_at > miss.received_at
        ORDER BY n.received_at ASC
        LIMIT 1
  ) tap ON true
 WHERE miss.intent IS NULL
   AND miss.confidence IS NOT NULL
   AND tap.intent IS NOT NULL
   AND tap.confidence = 1.0
   AND tap.received_at <= miss.received_at + interval '24 hours';

COMMENT ON VIEW whatsapp.routing_corrections IS
  'Human-verified eval set for the routing prompt: a message the classifier '
  'could not name, paired with the intent the customer then tapped off the '
  'disambiguation menu. Collected for free by a menu nobody designed as a '
  'labelling machine. Bounded to six months by '
  'services/messaging/wa_retention.py.';
