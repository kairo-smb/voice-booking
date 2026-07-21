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
    # across every provisioned DID — see
    # docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md
    twilio_bundle_sid: str = ""
    twilio_address_sid: str = ""
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

    model_config = {"env_prefix": ""}


def get_settings() -> Settings:
    return Settings()
