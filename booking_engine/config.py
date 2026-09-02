"""Booking Engine configuration from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""
    pool_min_size: int = 2
    pool_max_size: int = 10
    control_plane_secret: str = ""
    # Public base URL used for constructing Twilio webhook URLs
    public_base_url: str = ""
    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_default_country: str = "EE"
    # One-time regulatory Bundle (KYC) for the shared Kairo entity, reused
    # across every provisioned DID — see CLAUDE.md, "Telephony provider:
    # Telnyx -> Twilio"
    twilio_bundle_sid: str = ""
    twilio_address_sid: str = ""
    # WhatsApp — Meta Cloud API direct, as a Tech Provider (no BSP).
    # app_id / config_id / solution_id are served to the webapp so Embedded
    # Signup has one source of truth; they are public values, not secrets.
    meta_app_id: str = ""
    meta_config_id: str = ""
    meta_solution_id: str = ""
    # Secrets. `app_secret` verifies X-Hub-Signature-256 on every inbound
    # webhook *and* signs the Embedded Signup code exchange; `verify_token` is
    # the shared string Meta echoes back when it first registers the webhook.
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    # Kairo's own WABA (scripts/kairo_waba.py sets it up) — templates are
    # created here by hand and reviewed by Meta before ensure_templates will
    # push them into any customer's WABA. Empty means "not configured yet",
    # which fails closed: nothing propagates until these are set.
    meta_kairo_waba_id: str = ""
    meta_kairo_token: str = ""
    # Publicly-hosted sample receipt PDF Meta reviews when we create the
    # DOCUMENT-header `purchase_receipt_1` template on a customer's WABA. Empty
    # means "not configured": ensure_receipt_template fails closed rather than
    # submitting a header-less document template Meta would reject.
    meta_receipt_sample_url: str = ""
    # Marketing sends are dripped across this local window (Europe/Rome) so a
    # salon's promotions arrive during opening hours, not at 03:00.
    whatsapp_send_start_hour: int = 9
    whatsapp_send_end_hour: int = 20
    # Process-wide send pacing. Meta's per-number ceiling (20/s under
    # coexistence) is far above anything one salon does; the limit that can
    # actually be tripped is the Graph API's app-level one, shared across every
    # tenant at once. So the tick paces itself rather than firing a claimed
    # batch as fast as the loop runs. Clamped by meta_limits.safe_sends_per_minute
    # so no configuration can drive a number past its Meta throughput.
    whatsapp_sends_per_minute: int = 60
    # Our own guard against Meta's per-user, cross-brand marketing cap (error
    # 131049): never send the same customer two marketing messages inside this
    # window. Seven days both keeps us well under Meta's undisclosed ceiling
    # and matches how often a salon should reasonably contact anyone.
    whatsapp_recipient_cooldown_hours: int = 168
    # Meta caps a Tech Provider's new-customer onboarding per rolling 7 days:
    # 10 until Access Verification is complete, 200 after. Flip this once it is.
    meta_access_verified: bool = False
    # OpenAI SIP routing
    openai_sip_project_id: str = ""
    openai_api_key: str = ""
    openai_realtime_model: str = "gpt-realtime"
    # OpenAI webhook signing secret (verify realtime.call.incoming when set)
    openai_webhook_secret: str = ""
    # Voice agent — OpenAI tool + event webhook bearer token
    openai_tool_secret: str = ""
    # Token meter
    voice_kairo_tokens_per_second: int = 18
    voice_min_session_reserve_tokens: int = 1500
    # Within this many hours of the slot, the agent can't self-serve a
    # reschedule/cancel — it must escalate to the salon.
    voice_cancellation_lead_time_hours: int = 2
    # Spawn a per-call server-side Realtime control WebSocket (greeting + voice
    # tool results). Off by default; enable per environment for live SIP calls.
    enable_call_supervisor: bool = False
    # Log full raw Realtime events (transcripts, MCP args/output) instead of
    # just type/tool/latency. Debug-only — off by default so real call logs
    # don't carry conversation content; flip on for a QA test session.
    call_supervisor_verbose_logging: bool = False
    # Fallback shop for a raw SIP test call with no X-Shop-Id header (Twilio
    # normally adds that header for us; a bare softphone dial has no such
    # translation). Empty by default — production calls without a shop id
    # are still rejected as unroutable; only set this on QA for manual testing.
    sip_test_fallback_shop_id: str = ""

    model_config = {"env_prefix": ""}


def get_settings() -> Settings:
    return Settings()
