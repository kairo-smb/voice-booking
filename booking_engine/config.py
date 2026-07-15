"""Booking Engine configuration from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""
    pool_min_size: int = 2
    pool_max_size: int = 10
    control_plane_secret: str = ""
    # Public base URL used for constructing Telnyx webhook URLs
    public_base_url: str = ""
    # Telnyx
    telnyx_api_key: str = ""
    telnyx_public_key: str = ""
    telnyx_default_country: str = "IT"
    # OpenAI SIP routing
    openai_sip_project_id: str = ""
    # Voice agent — OpenAI tool + event webhook bearer token
    openai_tool_secret: str = ""
    # Token meter
    voice_kairo_tokens_per_second: int = 18
    voice_min_session_reserve_tokens: int = 1500
    # Within this many hours of the slot, the agent can't self-serve a
    # reschedule/cancel — it must escalate to the salon.
    voice_cancellation_lead_time_hours: int = 2

    model_config = {"env_prefix": ""}


def get_settings() -> Settings:
    return Settings()
