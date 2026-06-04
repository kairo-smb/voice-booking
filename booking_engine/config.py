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
    twilio_default_country: str = "IT"
    # OpenAI SIP routing
    openai_sip_project_id: str = ""
    # Token meter
    voice_kairo_tokens_per_second: int = 18
    voice_min_session_reserve_tokens: int = 1500
    voice_max_overage_tokens: int = 5000

    model_config = {"env_prefix": ""}


def get_settings() -> Settings:
    return Settings()
