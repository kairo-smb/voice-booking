"""Booking Engine configuration from environment variables."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""
    pool_min_size: int = 2
    pool_max_size: int = 10

    model_config = {"env_prefix": ""}
