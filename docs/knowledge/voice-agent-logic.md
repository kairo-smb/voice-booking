# Voice Agent Logic

> **Maintenance rule:** a change to `prompt_assembler.py`, `realtime_session.py`, `call_supervisor.py` or the tone system updates this file in the same change. See [README](README.md#maintenance-rule).

What the voice agent is told and who enforces what. Source here: `booking_engine/services/{prompt_assembler,realtime_session,identity_resolver,call_supervisor}.py`.

---

## Where the rules live now

Since 2026-09-28 the agent's **tools, their authorization and the booking rules are not in this repo.** They are marketing-engine's customer agents (see [API → Voice Tools](api/voice-tools.md) for the name map), shared with WhatsApp:

- **Identity is the session's caller number, never a model argument.** marketing-engine reads it from our `voice_agent.calls.caller_number` row (the per-call token names the row); no tool takes a phone. `customers_identify` and `appointments_upcoming` are scoped by it, and the webapp's `/agent/*` write routes check that the appointment/customer belongs to that caller (`sameCaller`) and to the shop. This replaces `booking_authz.py::authorize_booking_change`, deleted with the tool layer.
- **Slots come from one engine.** `availability_search` wraps the webapp `/availability` route; `create_appointment`/`reschedule_appointment` take a `proposal_id` from that search, never a free-form time, and the webapp write re-checks overlap, absences (`time_off`), shop hours and shift. The old voice path checked none of the last three, and reschedule checked nothing.
- **Past slots are refused by the webapp** (`slot_in_past`, `appointment_in_past`). The old 2-hour self-service lead time (`VOICE_CANCELLATION_LEAD_TIME_HOURS`, `booking_constraints.within_lead_time`) is **not** carried over: the setting was removed with the code that read it. Re-adding one is a webapp `/agent/*` rule now.
- **The rules text** (catalogue only from `services_catalog`, prices only when asked, summarise and wait for a yes before booking, read the `confirmation` field aloud after a write, escalate when a person is asked for, no medical advice) is `customerRules('voice')` in marketing-engine, fetched at accept time. Role-lock / privacy / scope prose is deliberately absent: the allow-list is the perimeter (owner decision, 2026-09-28).
- **Waiting phrase:** the voice rules tell the model to say a short phrase before searching. The old server-side 0.8s minimum latency on read tools (`mcp_tools.MIN_CHECK_LATENCY_SECONDS`) went with the in-process MCP server.

## Prompt assembly

Source: `prompt_assembler.py` (the persona) + marketing-engine (the rules). Since 2026-09-28 this repo owns only the salon's persona; the agent rules are fetched at accept time from marketing-engine (`GET /customer-agents/voice/instructions`, `clients/customer_agents_voice.py`) and appended after it (`realtime_session.py::build_accept_payload`). The persona, in order:
1. **Caller context** — built from `identity_resolver.py`'s `ResolutionResult`: hidden caller ID → greet neutrally; since no tool takes a phone number and every write is authorized on the session's caller number, a booking/change request is collected and handed over with `escalate_to_owner`; unique phone match → greet by name, mention last visit / notes; multiple customers share this number → ask who the booking is for before proceeding; no match → treat as a new caller, only create a customer record once a name is confirmed.
2. **Shop identity** — `display_name`, and a greeting: `answer_mode == "overflow"` shops use `greeting_overflow` (falling back to a generated default `"Salve, sono l'assistente di {name}. Come posso aiutarla?"` if the shop hasn't written one) since they're standing in for busy staff; other shops use `greeting_after_disclosure` with no code fallback (shop-authored, via the webapp). The first turn is that greeting, said as written.
3. **Tone instruction** — resolved from `shop_config.tone_id` against `voice_agent.voice_tones`; any lookup failure, missing id, or unknown tone falls back to a hardcoded default Italian instruction ("clear and professional"), never a hard error.
4. **Agent rules** (marketing-engine's `customerRules('voice')`) — appended last. If the fetch fails the call is still accepted with the persona alone and a `logger.error`.

## Tone system

8 seeded presets in `voice_tones` (see [Database](database.md)) — each is a `(name, description, system_prompt_instruction)` triple. Shops can eventually author custom tones (`created_by_shop_id` column exists) — not yet exposed in the webapp UI as of this writing.

## Call supervisor behavior

See [Architecture](architecture.md#call-flow) for the mechanism; the *behavioral* rule it exists to enforce is "always speak after a tool result" (hosted MCP does not auto-continue, see [Providers](providers.md#openai-realtime)) — `services/call_supervisor.py`'s `decide()` triggers exactly one `response.create` per tool result (via `response.output_item.done` on an `mcp_call`, guarded by `nudge_pending` to prevent double-nudging on parallel tool calls) and one on connect (the opening greeting, since the SIP accept path itself never triggers one).
