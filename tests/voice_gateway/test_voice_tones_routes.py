"""PATCH /voice/config validates tone_id against voice_tones table."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from booking_engine.api.app import create_app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_patch_accepts_valid_tone_id():
    shop_id = uuid4()
    tone_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_tone_by_id",
               new=AsyncMock(return_value={
                   "id": tone_id, "name": "professionale",
                   "system_prompt_instruction": "x",
                   "is_preset": True,
               })), \
         patch("booking_engine.api.routes.voice_config.upsert_config",
               new=AsyncMock(return_value={
                   "shop_id": shop_id, "tone_id": tone_id,
               })):
        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.patch(
                f"/api/v1/voice/config/{shop_id}",
                headers=AUTH,
                json={"tone_id": str(tone_id)},
            )
    assert r.status_code == 200
    assert r.json()["data"]["tone_id"] == str(tone_id)


@pytest.mark.asyncio
async def test_patch_rejects_unknown_tone_id():
    shop_id = uuid4()
    bogus = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_tone_by_id",
               new=AsyncMock(return_value=None)):
        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.patch(
                f"/api/v1/voice/config/{shop_id}",
                headers=AUTH,
                json={"tone_id": str(bogus)},
            )
    assert r.status_code == 400
    assert "tone" in r.text.lower()


@pytest.mark.asyncio
async def test_patch_accepts_null_tone_id_for_default():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.upsert_config",
               new=AsyncMock(return_value={
                   "shop_id": shop_id, "tone_id": None,
               })):
        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.patch(
                f"/api/v1/voice/config/{shop_id}",
                headers=AUTH,
                json={"tone_id": None},
            )
    assert r.status_code == 200
