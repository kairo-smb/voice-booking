# Credit pause for the WhatsApp responder + Owner veil — Plan

> For agentic workers: implement task-by-task, TDD, one commit per task. Do not push. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. webapp: stage files by name only (another process commits there). The WhatsApp onboarding/auth path is frozen.

Owner decisions (2026-09-29), final:
- **Single low-credit threshold per shop:** `voice_agent.shop_config.auto_topup_threshold_tokens`, default **10 000** when null. Every surface (banner, email, responder, voice) reads the same value.
- **Low credit disengages the automatic responder.** WABA stays connected. The Conversations tab stays usable, and everything becomes manual.
- **Jev keeps classifying** while balance > 0. **The fixed routing menu still goes out.** A tap labels the conversation, but no agent answers. A conversation with no intent (unconfident and no tap, or no credit at all) shows an **"In valutazione"** badge.
- **Automatic resume after top-up.** Conversations received during low credit **stay manual**.
- **Alert email** to the owner, once per low-credit episode.
- **Owner-only surfaces:**
  - a real employee sees a non-clickable veil and never loads the content; owner-only buttons are grey and cannot be clicked;
  - an owner in employee view (downgraded, `canElevate`) sees a veil that opens the existing PIN dialog on any click; owner-only buttons are clickable, ask for the PIN, then run the action;
  - only the owner ever escalates (existing `/api/v1/auth/elevate`), so no new auth endpoint;
  - single financial numbers inside pages employees can reach stay hidden as today.

## Part 1 — Credit pause (voice-booking + webapp)

### Shared contract
- `low_credit = balance <= threshold`.
- `balance` is effective granted plus purchased, as in voice-booking `db/token_basket_queries.py::get_balance` and webapp `effectiveBalance`.
- `threshold = coalesce(shop_config.auto_topup_threshold_tokens, 10000)`.
- New session reason **`low_credit`**, alongside `human_took_over`: `voice_agent.calls.outcome='escalated'`, `outcome_reason='low_credit'`.

### voice-booking
1. `services/credit_state.py`: `async def credit_state(shop_id) -> {"balance", "threshold", "low"}`, using the default above. Unit-tested.
2. `wa_agent.handle`: after `open_session` (so the stamp lands on a row), if `low` → `_stand_down(call_id, …, reason="low_credit", escalate=True)`. Add `low_credit` to `_ESCALATING_REASONS`. `agent_status` maps an escalated session whose `outcome_reason == "low_credit"` to reason `low_credit` (like TAKEOVER_REASON). Tests: low → no marketing-engine call and the session is stamped; after top-up a *new* session answers; the stamped one stays manual.
3. `wa_nudge`: skip shops where `low`.
4. **Evaluation flag.** The thread row gains `needs_evaluation: bool`: the current session has inbound messages but no routed intent. Derive it in `whatsapp_thread_queries` from the existing `routed` CTE. Test the SQL shape and execute it read-only on QA.
5. **Email tick.** Migration `2x_credit_low_notice.sql` adds `voice_agent.shop_config.credit_low_notified_at timestamptz`.
   - In the hourly sweep, for shops with `whatsapp_agent_enabled`: if `low` and `credit_low_notified_at IS NULL` → POST webapp `/api/v1/hair-salon/whatsapp/credit-low` (engine bearer, same pattern as `webapp_notify` token-expiring), then stamp.
   - If not low and stamped → clear, so the next episode sends again.
   - Apply the migration twice on a scratch Postgres.
6. `GET /whatsapp/status/{shop}` (or the thread list) exposes `credit: {balance, threshold, low}` so the webapp has one source.
7. Docs: `docs/knowledge/api/whatsapp.md`, `database.md`.

### webapp
1. The threshold default (10 000) applies when null: `CreditReminderBanner` and the `VoiceAgentSettingsTab` read the same default, so no shop is left without a banner. Fix the stale "unset = opted out" docstring.
2. `src/lib/whatsapp/agent.ts`: add `low_credit` to `AgentReason` + `REASON_KEYS`. `agentLead` → human.
3. Inbox Conversations tab: a banner when `credit.low`, reading *"Risponditore automatico in pausa: credito sotto soglia. Ricarica per riattivarlo."* with a CTA to top-up. Cockpit `CreditReminderBanner`: add the same responder line when WhatsApp is online.
4. `WhatsAppPanel` agent toggle: disabled/grey with the explanation while low. The stored preference stays untouched.
5. `ThreadList`/`ThreadView`: an **"In valutazione"** chip when `needs_evaluation`.
6. Email:
   - route `POST /api/v1/hair-salon/whatsapp/credit-low` (engine-only, copy `whatsapp/token-expiring/route.ts`);
   - template `creditLowEmail` in `src/lib/email/templates.ts` (it/en/es);
   - owner lookup identical to token-expiring;
   - returns `{sent, reason}`.
7. i18n it/en/es + key tests. `npm run verify`.

## Part 2 — Owner veil + owner actions (webapp, in a separate git worktree/branch)

1. `role-visibility.ts`: split the "role-blocked" state from "disabled". New helpers `isTileVeiled` / `isFeatureVeiled`, exposed in app-context. `isTileEnabled` keeps meaning "feature on".
2. `src/components/auth/OwnerVeil.tsx`: `<OwnerVeil area="…">{children}</OwnerVeil>`.
   - **Never renders children** when veiled; shows a neutral skeleton plus the message.
   - Employee (`!canElevate`): "Sezione riservata al titolare", not clickable.
   - Owner downgraded (`canElevate`): "Inserisci il PIN del titolare per accedere"; the whole surface is clickable and opens `ElevationDialog`. After a successful elevate the content renders; the role change re-renders.
   - Tests for all three roles.
3. Replace hide/redirect with the veil:
   - nav Marketing shown with a lock;
   - `/marketing` page;
   - Settings tabs abbonamento/bundle/inbox/dati;
   - Business views spese/cruscotto/servizi/analisi (also stop the unconditional `/business/dashboard` fetch for members);
   - Inbox configuration/analytics;
   - Invoices register/analytics;
   - `settings/consumi`;
   - action-center owner cockpit/ImpactCard.
4. `useOwnerAction()` / `<OwnerActionButton>`: one mechanism for owner-only actions.
   - Employee → disabled grey.
   - Owner downgraded → enabled; click → `ElevationDialog` → on success run the original handler.
   - Owner elevated → runs directly.
   - Apply to the `memberOnly` / `isOwner` action sites: BusinessOperationalSetup, BusinessProductsView, ProductForm, Packages/Promotions/StaffCost/Costs views, settings, invoices reopen/delete, BusinessSuggestedServices.
   - Inputs that were read-only stay read-only for employees and become editable only after PIN.
5. api-client: a non-demo 403 on a member session with `canElevate` dispatches `owner:required`, which opens `ElevationDialog`.
6. Docs (`docs/knowledge/features.md` roles section). `npm run verify`.

## Order
Part 1 and Part 2 run in parallel (Part 2 in its own worktree). Then a cross-review, a merge of the Part 2 branch into webapp QA, one push of all repos, a QA reseed and a live test.
