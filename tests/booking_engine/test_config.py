import pytest
from booking_engine.config import Settings


def test_settings_from_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    s = Settings()
    assert s.database_url == "postgresql://user:pass@host/db"


def test_settings_default_pool_sizes():
    s = Settings(database_url="postgresql://localhost/test")
    assert s.pool_min_size == 2
    assert s.pool_max_size == 10
