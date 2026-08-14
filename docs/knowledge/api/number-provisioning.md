# Self-Service Number Provisioning API

Endpoints backing the webapp's "Richiedi numero" panel (Inbox → Configurazione → Canali) and the hourly cron that drives it forward. All require `Authorization: Bearer <CONTROL_PLANE_SECRET>` (`require_control_plane_token`) — same scheme as [Voice Control Plane](voice-control-plane.md), just split into its own page because the flow (Twilio Regulatory Compliance, not a plain DB read/write) is substantial enough to document on its own. See [Architecture](../architecture.md#self-service-number-provisioning-path-2-onboarding) for the end-to-end flow and `CLAUDE.md` §2026-08-14 for why this exists alongside the older shared-bundle `/voice/numbers/provision` route.

> **Maintenance rule:** an endpoint added/removed/changed updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Method | Path | File | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/voice/numbers/request` | `voice_telephony.py` | Multipart: `shop_id`, `business_name`, `contact_email`, `document` (the *visura camerale*). Builds that salon's own Twilio regulatory bundle and submits it for review if compliant. Idempotent — a shop already `provisioned` short-circuits with no Twilio call. |
| `GET` | `/api/v1/voice/numbers/request/{shop_id}` | `voice_telephony.py` | The webapp's poll target. Returns `{"request": ..., "telephony": ...}` — both may be `null` (no request yet). The webapp's `viewFor()` state machine picks a UI state from this pair. |
| `POST` | `/api/v1/messaging/tick` | `messaging_tick.py` | The single hourly scheduled entry point (`.github/workflows/messaging-cron.yml`, `0 * * * *`). Polls every `pending_review` request's Twilio bundle status, purchases the number for any newly-approved bundle, marks any newly-rejected one, then refreshes the health semaphore for every already-provisioned number. Returns counts: `{"reviewed", "provisioned", "rejected", "errors", "health"}`. One shop's failure doesn't abort the sweep. |

## `POST /voice/numbers/request` response shapes

| `status` | Meaning |
|---|---|
| `"provisioned"` | Already has a number — no-op, nothing sent to Twilio. |
| `"draft"` + `evaluation_errors` | Twilio's `Evaluations` call found violations; `evaluation_errors` is `[{"friendly_name", "description"}, ...]`, Twilio's own wording verbatim (the `description` field is populated here in our response — sourced from Twilio's `failure_reason`, since Twilio's own evaluation objects have no `description` key; see [Providers](../providers.md#twilio)). |
| `"pending_review"` | Compliant; bundle submitted, now waiting on Twilio's multi-day human review. |
| `{"ok": false, "error": "no_regulation"}` | No regulation exists for `IsoCountry=EE&NumberType=mobile` — stops before creating any Twilio object, since nothing would have a valid regulation to attach to. |

## `POST /messaging/tick` internals worth knowing

- **Purchase happens here, not on request** — the salon never waits on a synchronous Twilio number purchase. `services/number_provisioning.py::provision_approved` is idempotent: checks `shop_telephony` first (handles a double-tick or retried cron run with zero Twilio calls), inserts with `ON CONFLICT DO NOTHING`, and releases the number back to Twilio if the insert loses a race — otherwise a lost race leaks a number billed ~$3/mo forever.
- **Health check** (`services/number_health.py::check_all`) runs every tick for every provisioned number: one `fetch_number` per shop. A confirmed 404 → red (`number_missing`); webhooks not pointed at our own base URL → red (`webhook_drift`); Twilio unreachable → **status left unchanged**, only `health_checked_at` is stamped — a Twilio outage must not repaint every salon's number red.
