# Voice Control Plane API

Endpoints the `webapp` Control Plane calls to manage voice config, telephony numbers, balance, and call history. All require `Authorization: Bearer <CONTROL_PLANE_SECRET>` (`require_control_plane_token`).

> **Maintenance rule:** an endpoint added/removed/changed here updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Method | Path | File | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/voice/config/tones` | `voice_config.py` | list the 8 preset tones (+ any shop-authored ones) |
| `GET` | `/api/v1/voice/config/{shop_id}` | `voice_config.py` | read Layer 1 config |
| `PATCH` | `/api/v1/voice/config/{shop_id}` | `voice_config.py` | update any subset of `_PATCHABLE_FIELDS` (enabled, display_name, greetings, voice_preset, tone_id, business_hours, answer_mode, overflow_ring_count, services_to_mention, retention_days, manual_fallback_number, auto-topup settings) |
| `GET` | `/api/v1/voice/balance/{shop_id}` | `voice_balance.py` | token balance + warning tier |
| `POST` | `/api/v1/voice/heartbeat/forwarding` | `voice_heartbeat.py` | Path-2 (forward) silent-line heartbeat — meant to be hit by a scheduled job, not the webapp UI directly |
| `GET` | `/api/v1/voice/numbers/search` | `voice_telephony.py` | search available Twilio numbers |
| `POST` | `/api/v1/voice/numbers/provision` | `voice_telephony.py` | purchase + bind a number to a shop |
| `GET` | `/api/v1/voice/numbers/{shop_id}/setup-instructions` | `voice_telephony.py` | forwarding setup copy for the shop's existing carrier |
| `GET` | `/api/v1/voice/numbers/{shop_id}` | `voice_telephony.py` | current telephony config for a shop |
| `GET` | `/api/v1/shops/{shop_id}/voice/calls` | `voice.py` | paginated call list |
| `GET` | `/api/v1/shops/{shop_id}/voice/calls/{call_id}` | `voice.py` | full call detail: summary + transcript + events |
| `PATCH` | `/api/v1/shops/{shop_id}/voice/calls/{call_id}/link-customer` | `voice.py` | manually link an unmatched call to a customer |
| `GET` | `/api/v1/shops/{shop_id}/voice/analytics` | `voice.py` | volume/outcome/demand aggregates |
| `GET` | `/api/v1/voice/memos/{shop_id}` | `voice_memos.py` | list callback memos (from `escalate_to_merchant`) |
| `GET` | `/api/v1/voice/memos/{shop_id}/count` | `voice_memos.py` | unread count, for an Action Center badge |
| `PATCH` | `/api/v1/voice/memos/{memo_id}` | `voice_memos.py` | mark a memo read/actioned |

Customer-match enum (`voice.py`'s call responses): `existing \| created \| unmatched \| ambiguous`.
