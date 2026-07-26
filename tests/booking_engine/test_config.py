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


def test_control_plane_secret_loaded(monkeypatch):
    from booking_engine.config import Settings
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret-123")
    s = Settings()
    assert s.control_plane_secret == "test-secret-123"


def test_control_plane_secret_default_empty(monkeypatch):
    from booking_engine.config import Settings
    monkeypatch.delenv("CONTROL_PLANE_SECRET", raising=False)
    s = Settings()
    assert s.control_plane_secret == ""


def test_enable_call_supervisor_defaults_false(monkeypatch):
    monkeypatch.delenv("ENABLE_CALL_SUPERVISOR", raising=False)
    from booking_engine.config import Settings
    assert Settings().enable_call_supervisor is False


def test_enable_call_supervisor_reads_env(monkeypatch):
    monkeypatch.setenv("ENABLE_CALL_SUPERVISOR", "true")
    from booking_engine.config import Settings
    assert Settings().enable_call_supervisor is True
