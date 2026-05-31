"""Voice Gateway configuration."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    booking_engine_url: str = "http://localhost:8000"
    openai_key: str = ""
    database_url: str = ""
    openai_classifier_model: str = "gpt-4o-mini"

    model_config = {"env_prefix": ""}
