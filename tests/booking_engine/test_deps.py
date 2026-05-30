"""Tests for shared API dependencies."""
import pytest
from fastapi import HTTPException
from booking_engine.api.deps import require_control_plane_token


class _Req:
    def __init__(self, header: str | None):
        self.headers = {} if header is None else {"authorization": header}


def _settings(secret: str):
    class S:
        control_plane_secret = secret
    return S()


@pytest.mark.asyncio
async def test_missing_header_rejected():
    with pytest.raises(HTTPException) as exc:
        await require_control_plane_token(_Req(None), _settings("s"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_rejected():
    with pytest.raises(HTTPException) as exc:
        await require_control_plane_token(_Req("Bearer nope"), _settings("s"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_accepted():
    result = await require_control_plane_token(_Req("Bearer good"), _settings("good"))
    assert result is True


@pytest.mark.asyncio
async def test_empty_server_secret_rejects_all():
    with pytest.raises(HTTPException) as exc:
        await require_control_plane_token(_Req("Bearer anything"), _settings(""))
    assert exc.value.status_code == 503
