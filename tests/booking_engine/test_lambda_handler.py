"""Tests for the AWS Lambda Mangum handler."""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch, AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _clean_lambda_module():
    sys.modules.pop("lambda_handler", None)
    yield
    sys.modules.pop("lambda_handler", None)


class TestLambdaHandler:
    def test_handler_is_callable(self):
        with (
            patch("booking_engine.api.app.Settings"),
            patch("booking_engine.api.app.init_connection", new_callable=AsyncMock),
            patch("booking_engine.api.app.close_connection", new_callable=AsyncMock),
        ):
            from lambda_handler import handler
            assert callable(handler)

    def test_health_endpoint_via_lambda(self):
        with (
            patch("booking_engine.api.app.Settings"),
            patch("booking_engine.api.app.init_connection", new_callable=AsyncMock),
            patch("booking_engine.api.app.close_connection", new_callable=AsyncMock),
        ):
            from lambda_handler import handler

            event = {
                "version": "2.0",
                "requestContext": {
                    "http": {
                        "method": "GET",
                        "path": "/health",
                        "sourceIp": "127.0.0.1",
                    },
                    "accountId": "123456789012",
                    "apiId": "test",
                    "stage": "$default",
                },
                "rawPath": "/health",
                "rawQueryString": "",
                "headers": {},
                "isBase64Encoded": False,
            }

            response = handler(event, {})
            assert response["statusCode"] == 200
