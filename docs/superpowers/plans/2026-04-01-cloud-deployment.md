# Cloud Deployment Plan — AWS Lambda + Fly.io

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the Booking Engine to AWS Lambda (sub-second cold start, $0 idle) and the Voice Gateway to Fly.io (WebSocket support, auto-stop, free tier), removing all Databricks dependencies.

**Architecture:** The Booking Engine is wrapped with Mangum for Lambda and served via a Lambda Function URL (no API Gateway). The Voice Gateway runs as a Docker container on Fly.io with auto-stop machines. Both services are stateless; persistent state lives in Neon PostgreSQL (already hosted). The future Amplify frontend will call the Booking Engine's public Lambda Function URL directly.

**Tech Stack:** Python 3.12, FastAPI, Mangum (Lambda adapter), asyncpg, Docker, AWS Lambda (container image), AWS ECR, Fly.io, Neon PostgreSQL

---

## Architecture Diagram

```
     Phone call (future)
         │
         ▼
┌─────────────────┐
│     Twilio       │  (not yet initiated)
└────────┬────────┘
         │ WebSocket
         ▼
┌──────────────────────┐    HTTPS (public)     ┌──────────────────────┐
│  Voice Gateway       │ ────────────────────► │  Booking Engine      │
│  Fly.io (Frankfurt)  │                       │  AWS Lambda          │
│  auto-stop, free tier│◄──────────────────── │  Function URL        │
│  port 8080           │                       │  ~300ms cold start   │
└──────────────────────┘                       └────────┬─────────────┘
                                                        │
                                                 ┌──────┴──────┐
┌─────────────┐                                  │    Neon     │
│ AWS Amplify │──► Lambda Function URL (public)  │  PostgreSQL │
│ (Future UI) │                                  │  (free tier) │
└─────────────┘                                  └─────────────┘
```

## File Map

| Action | File | Purpose |
|--------|------|---------|
| **Create** | `lambda_handler.py` | Mangum entry point for AWS Lambda |
| **Create** | `booking_engine/Dockerfile` | Lambda container image |
| **Create** | `voice_gateway/Dockerfile` | Fly.io container image |
| **Create** | `fly.toml` | Fly.io app configuration |
| **Create** | `scripts/deploy-booking.sh` | AWS ECR + Lambda deployment |
| **Create** | `scripts/deploy-voice.sh` | Fly.io deployment |
| **Modify** | `voice_gateway/config.py` | Remove Databricks fields |
| **Modify** | `voice_gateway/api/app.py` | Remove Databricks SDK auto-detect (~35 lines) |
| **Modify** | `voice_gateway/clients/booking_client.py` | Remove auth_token parameter |
| **Modify** | `booking_engine/requirements.txt` | Add mangum |
| **Modify** | `.gitignore` | Fix malformed line, add patterns |
| **Modify** | `tests/voice_gateway/test_booking_client.py` | Update for removed auth_token |
| **Modify** | `tests/voice_gateway/test_routes/conftest.py` | Simplify fixtures |
| **Create** | `tests/booking_engine/test_lambda_handler.py` | Test Mangum handler |
| **Delete** | `.databricksignore` | No longer needed |
| **Delete** | `booking_engine/app.yaml` | Databricks Apps config, replaced by Lambda |

---

### Task 1: Clean Voice Gateway — Remove Databricks Auth Code

Remove all Databricks SDK auto-detection, auth tokens, and related config from the Voice Gateway. After this task, the Voice Gateway is a plain FastAPI app that takes `BOOKING_ENGINE_URL` and `OPENAI_KEY` from env vars.

**Files:**
- Modify: `voice_gateway/config.py`
- Modify: `voice_gateway/api/app.py`
- Modify: `voice_gateway/clients/booking_client.py`
- Modify: `tests/voice_gateway/test_booking_client.py`
- Modify: `tests/voice_gateway/test_routes/conftest.py`

- [ ] **Step 1: Update voice_gateway/config.py — remove Databricks fields**

Replace the entire file with:

```python
"""Voice Gateway configuration."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    booking_engine_url: str = "http://localhost:8000"
    openai_key: str = ""

    model_config = {"env_prefix": ""}
```

- [ ] **Step 2: Rewrite voice_gateway/api/app.py — remove SDK auto-detect**

Replace the entire file with:

```python
"""Voice Gateway FastAPI application."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize booking client and OpenAI key."""
    from voice_gateway.config import Settings
    from voice_gateway.clients.booking_client import BookingClient

    settings = Settings()
    logger.info("Voice Gateway starting: booking_url=%s", settings.booking_engine_url)

    bc = BookingClient(base_url=settings.booking_engine_url)
    await bc.__aenter__()
    app.state.booking_client = bc
    app.state._openai_key = settings.openai_key
    logger.info("Booking client connected, OpenAI Realtime %s",
                "enabled" if settings.openai_key else "disabled")

    yield

    if hasattr(app.state, "booking_client") and app.state.booking_client:
        await app.state.booking_client.__aexit__(None, None, None)


def create_app() -> FastAPI:
    import pathlib

    app = FastAPI(title="Virtual Assistant Voice Gateway", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    from voice_gateway.api.routes import realtime
    app.include_router(realtime.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Serve test UI
    static_dir = pathlib.Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        from fastapi.responses import FileResponse

        @app.get("/")
        async def ui():
            return FileResponse(static_dir / "index.html")

    return app
```

- [ ] **Step 3: Simplify BookingClient — remove auth_token**

Replace the entire file `voice_gateway/clients/booking_client.py` with:

```python
"""Async HTTP client for the Booking Engine API."""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

import httpx


class BookingClient:
    """Thin async wrapper around the Booking Engine REST API."""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self._base = base_url.rstrip("/")
        if not self._base.startswith("http"):
            self._base = f"https://{self._base}"
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("BookingClient not entered as context manager")
        return self._client

    # ── Shops ──────────────────────────────────────────

    async def get_shop(self, shop_id: UUID) -> dict | None:
        r = await self.client.get(f"{self._base}/api/v1/shops/{shop_id}")
        return r.json() if r.status_code == 200 else None

    # ── Customers ──────────────────────────────────────

    async def find_customers_by_phone(self, shop_id: UUID, phone: str) -> list[dict]:
        r = await self.client.get(f"{self._base}/api/v1/shops/{shop_id}/customers", params={"phone": phone})
        return r.json() if r.status_code == 200 else []

    async def find_customer_by_name_phone(
        self, shop_id: UUID, name: str, phone: str
    ) -> list[dict]:
        r = await self.client.get(
            f"{self._base}/api/v1/shops/{shop_id}/customers", params={"name": name, "phone": phone}
        )
        return r.json() if r.status_code == 200 else []

    async def create_customer(
        self, shop_id: UUID, full_name: str, phone_number: str | None = None
    ) -> dict:
        r = await self.client.post(
            f"{self._base}/api/v1/shops/{shop_id}/customers",
            json={"full_name": full_name, "phone_number": phone_number},
        )
        return r.json()

    # ── Services & Staff ───────────────────────────────

    async def get_services(self, shop_id: UUID) -> list[dict]:
        r = await self.client.get(f"{self._base}/api/v1/shops/{shop_id}/services")
        return r.json()

    async def get_staff(self, shop_id: UUID) -> list[dict]:
        r = await self.client.get(f"{self._base}/api/v1/shops/{shop_id}/staff")
        return r.json()

    # ── Availability ───────────────────────────────────

    async def check_availability(
        self, shop_id: UUID, service_ids: list[UUID],
        start_date: date, end_date: date, staff_id: UUID | None = None,
    ) -> dict:
        params = {
            "service_ids": ",".join(str(s) for s in service_ids),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if staff_id:
            params["staff_id"] = str(staff_id)
        r = await self.client.get(f"{self._base}/api/v1/shops/{shop_id}/availability", params=params)
        return r.json()

    # ── Appointments ───────────────────────────────────

    async def book_appointment(
        self, shop_id: UUID, customer_id: UUID, service_ids: list[UUID],
        staff_id: UUID, start_time: datetime, notes: str | None = None,
    ) -> dict:
        r = await self.client.post(
            f"{self._base}/api/v1/shops/{shop_id}/appointments",
            json={
                "customer_id": str(customer_id),
                "service_ids": [str(s) for s in service_ids],
                "staff_id": str(staff_id),
                "start_time": start_time.isoformat() if hasattr(start_time, 'isoformat') else str(start_time),
                "notes": notes,
            },
        )
        return r.json()

    async def list_appointments(
        self, shop_id: UUID, customer_id: UUID, status: str | None = None,
    ) -> list[dict]:
        params = {"customer_id": str(customer_id)}
        if status:
            params["status"] = status
        r = await self.client.get(f"{self._base}/api/v1/shops/{shop_id}/appointments", params=params)
        return r.json()

    async def cancel_appointment(self, shop_id: UUID, appointment_id: UUID) -> dict:
        r = await self.client.patch(f"{self._base}/api/v1/shops/{shop_id}/appointments/{appointment_id}/cancel")
        return r.json()

    async def reschedule_appointment(
        self, shop_id: UUID, appointment_id: UUID,
        new_start_time: datetime, new_staff_id: UUID | None = None,
    ) -> dict:
        body = {"new_start_time": new_start_time.isoformat()}
        if new_staff_id:
            body["new_staff_id"] = str(new_staff_id)
        r = await self.client.patch(
            f"{self._base}/api/v1/shops/{shop_id}/appointments/{appointment_id}/reschedule",
            json=body,
        )
        return r.json()
```

- [ ] **Step 4: Update BookingClient tests — remove auth_token references**

Replace `tests/voice_gateway/test_booking_client.py` with:

```python
"""Unit tests for BookingClient with mocked HTTP."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest

from voice_gateway.clients.booking_client import BookingClient

SHOP = UUID("a0000000-0000-0000-0000-000000000001")
STAFF = UUID("b0000000-0000-0000-0000-000000000001")
SVC = UUID("c0000000-0000-0000-0000-000000000001")
CUST = UUID("d0000000-0000-0000-0000-000000000001")
APPT = UUID("e0000000-0000-0000-0000-000000000001")
BASE = "http://test-booking:8000"


def _resp(status: int, json_data=None) -> httpx.Response:
    """Build a real httpx.Response with JSON body."""
    import json
    content = json.dumps(json_data).encode() if json_data is not None else b""
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


@pytest.fixture
async def bc():
    client = BookingClient(base_url=BASE)
    async with client:
        yield client


class TestBookingClientInit:
    def test_normalizes_url_without_scheme(self):
        bc = BookingClient(base_url="example.com")
        assert bc._base == "https://example.com"

    def test_strips_trailing_slash(self):
        bc = BookingClient(base_url="http://example.com/")
        assert bc._base == "http://example.com"


class TestBookingClientContextManager:
    async def test_raises_without_context(self):
        bc = BookingClient()
        with pytest.raises(RuntimeError, match="context manager"):
            _ = bc.client


class TestGetShop:
    async def test_returns_shop_on_200(self, bc):
        shop_data = {"id": str(SHOP), "name": "Salone"}
        bc._client.get = AsyncMock(return_value=_resp(200, shop_data))
        result = await bc.get_shop(SHOP)
        assert result == shop_data

    async def test_returns_none_on_404(self, bc):
        bc._client.get = AsyncMock(return_value=_resp(404, {"error": "not_found"}))
        result = await bc.get_shop(SHOP)
        assert result is None


class TestFindCustomersByPhone:
    async def test_returns_customers(self, bc):
        customers = [{"id": str(CUST), "full_name": "Anna"}]
        bc._client.get = AsyncMock(return_value=_resp(200, customers))
        result = await bc.find_customers_by_phone(SHOP, "+39123")
        assert len(result) == 1

    async def test_returns_empty_on_error(self, bc):
        bc._client.get = AsyncMock(return_value=_resp(500, None))
        result = await bc.find_customers_by_phone(SHOP, "+39123")
        assert result == []


class TestCreateCustomer:
    async def test_creates_customer(self, bc):
        customer = {"id": str(CUST), "full_name": "Anna"}
        bc._client.post = AsyncMock(return_value=_resp(201, customer))
        result = await bc.create_customer(SHOP, "Anna", "+39123")
        assert result["full_name"] == "Anna"


class TestGetServices:
    async def test_returns_services(self, bc):
        services = [{"id": str(SVC), "service_name": "Taglio"}]
        bc._client.get = AsyncMock(return_value=_resp(200, services))
        result = await bc.get_services(SHOP)
        assert len(result) == 1


class TestGetStaff:
    async def test_returns_staff(self, bc):
        staff = [{"id": str(STAFF), "full_name": "Maria"}]
        bc._client.get = AsyncMock(return_value=_resp(200, staff))
        result = await bc.get_staff(SHOP)
        assert len(result) == 1


class TestCheckAvailability:
    async def test_returns_availability(self, bc):
        avail = {"slots": [{"staff_id": str(STAFF)}]}
        bc._client.get = AsyncMock(return_value=_resp(200, avail))
        result = await bc.check_availability(SHOP, [SVC], date(2026, 4, 1), date(2026, 4, 1))
        assert len(result["slots"]) == 1


class TestBookAppointment:
    async def test_books_appointment(self, bc):
        appt = {"id": str(APPT), "status": "scheduled"}
        bc._client.post = AsyncMock(return_value=_resp(201, appt))
        result = await bc.book_appointment(SHOP, CUST, [SVC], STAFF, datetime(2026, 4, 1, 10, 0))
        assert result["status"] == "scheduled"


class TestListAppointments:
    async def test_lists_appointments(self, bc):
        appts = [{"id": str(APPT), "status": "scheduled"}]
        bc._client.get = AsyncMock(return_value=_resp(200, appts))
        result = await bc.list_appointments(SHOP, CUST)
        assert len(result) == 1


class TestCancelAppointment:
    async def test_cancels_appointment(self, bc):
        cancelled = {"id": str(APPT), "status": "cancelled"}
        bc._client.patch = AsyncMock(return_value=_resp(200, cancelled))
        result = await bc.cancel_appointment(SHOP, APPT)
        assert result["status"] == "cancelled"


class TestRescheduleAppointment:
    async def test_reschedules_appointment(self, bc):
        rescheduled = {"id": "new-id", "status": "scheduled"}
        bc._client.patch = AsyncMock(return_value=_resp(200, rescheduled))
        result = await bc.reschedule_appointment(SHOP, APPT, datetime(2026, 4, 2, 14, 0))
        assert result["status"] == "scheduled"
```

- [ ] **Step 5: Run voice gateway tests to verify cleanup**

Run: `cd /Users/mirco.meazzo/virtual-assistant && python -m pytest tests/voice_gateway/ -v`

Expected: All 20+ tests PASS. No import errors related to Databricks.

- [ ] **Step 6: Commit**

```bash
git add voice_gateway/config.py voice_gateway/api/app.py voice_gateway/clients/booking_client.py \
  tests/voice_gateway/test_booking_client.py
git commit -m "refactor: remove Databricks auth from Voice Gateway

Strip SDK auto-detect, databricks_host/token config, and Bearer token
auth from BookingClient. Voice Gateway now takes only BOOKING_ENGINE_URL
and OPENAI_KEY from environment.

Co-authored-by: Isaac"
```

---

### Task 2: Add Mangum Lambda Handler for Booking Engine

Add the Mangum ASGI adapter so the existing FastAPI Booking Engine can run on AWS Lambda without any changes to the app code itself.

**Files:**
- Create: `lambda_handler.py`
- Modify: `booking_engine/requirements.txt`
- Create: `tests/booking_engine/test_lambda_handler.py`

- [ ] **Step 1: Write the test for the Lambda handler**

Create `tests/booking_engine/test_lambda_handler.py`:

```python
"""Tests for the AWS Lambda Mangum handler."""
from __future__ import annotations

from unittest.mock import patch, AsyncMock


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/mirco.meazzo/virtual-assistant && python -m pytest tests/booking_engine/test_lambda_handler.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'lambda_handler'` or `No module named 'mangum'`

- [ ] **Step 3: Add mangum to requirements**

Append to `booking_engine/requirements.txt`:

```
pydantic-settings
asyncpg
fastapi
uvicorn
mangum
```

Install: `pip install mangum`

- [ ] **Step 4: Create the Lambda handler**

Create `lambda_handler.py` in the project root:

```python
"""AWS Lambda entry point — wraps the FastAPI app with Mangum."""
from mangum import Mangum

from booking_engine.api.app import create_app

app = create_app()
handler = Mangum(app, lifespan="auto")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /Users/mirco.meazzo/virtual-assistant && python -m pytest tests/booking_engine/test_lambda_handler.py -v`

Expected: PASS — both tests green.

- [ ] **Step 6: Run full booking engine test suite to verify no regressions**

Run: `cd /Users/mirco.meazzo/virtual-assistant && python -m pytest tests/booking_engine/ tests/integration/ -v`

Expected: All existing tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add lambda_handler.py booking_engine/requirements.txt tests/booking_engine/test_lambda_handler.py
git commit -m "feat: add Mangum Lambda handler for Booking Engine

Wrap FastAPI app with Mangum ASGI adapter for AWS Lambda deployment.
lifespan='auto' ensures asyncpg pool is created on cold start and
reused across warm invocations.

Co-authored-by: Isaac"
```

---

### Task 3: Create Dockerfiles

Two Dockerfiles: one for the Booking Engine (AWS Lambda container image) and one for the Voice Gateway (Fly.io).

**Files:**
- Create: `booking_engine/Dockerfile`
- Create: `voice_gateway/Dockerfile`

- [ ] **Step 1: Create Booking Engine Dockerfile (Lambda container)**

Create `booking_engine/Dockerfile`:

```dockerfile
FROM public.ecr.aws/lambda/python:3.12

# Install dependencies
COPY booking_engine/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy application code
COPY booking_engine/ ${LAMBDA_TASK_ROOT}/booking_engine/
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_handler.handler"]
```

- [ ] **Step 2: Build and verify the Booking Engine image**

Run:
```bash
cd /Users/mirco.meazzo/virtual-assistant
docker build -f booking_engine/Dockerfile -t booking-engine:test .
```

Expected: Build succeeds. Image is created.

Verify the handler is importable:
```bash
docker run --rm booking-engine:test python -c "from lambda_handler import handler; print('OK')"
```

Expected: Prints `OK`.

- [ ] **Step 3: Create Voice Gateway Dockerfile (Fly.io)**

Create `voice_gateway/Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY voice_gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY voice_gateway/ ./voice_gateway/

EXPOSE 8080

CMD ["uvicorn", "voice_gateway.api.app:create_app", "--host", "0.0.0.0", "--port", "8080", "--factory"]
```

- [ ] **Step 4: Build and verify the Voice Gateway image**

Run:
```bash
cd /Users/mirco.meazzo/virtual-assistant
docker build -f voice_gateway/Dockerfile -t voice-gateway:test .
```

Expected: Build succeeds. Image is created.

Verify the app is importable:
```bash
docker run --rm voice-gateway:test python -c "from voice_gateway.api.app import create_app; print('OK')"
```

Expected: Prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add booking_engine/Dockerfile voice_gateway/Dockerfile
git commit -m "feat: add Dockerfiles for Lambda and Fly.io deployment

booking_engine/Dockerfile: AWS Lambda Python 3.12 container with Mangum handler.
voice_gateway/Dockerfile: Python 3.12-slim for Fly.io with uvicorn on port 8080.
Both built from project root as context.

Co-authored-by: Isaac"
```

---

### Task 4: Fly.io Configuration

Create `fly.toml` to configure the Voice Gateway on Fly.io with auto-stop machines (scale to zero).

**Files:**
- Create: `fly.toml`

- [ ] **Step 1: Create fly.toml**

Create `fly.toml` in the project root:

```toml
app = 'virtual-assistant-voice'
primary_region = 'fra'

[build]
  dockerfile = 'voice_gateway/Dockerfile'

[env]
  BOOKING_ENGINE_URL = ''
  # OPENAI_KEY set via: fly secrets set OPENAI_KEY=sk-...

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = 'stop'
  auto_start_machines = true
  min_machines_running = 0

  [http_service.concurrency]
    type = 'requests'
    hard_limit = 250
    soft_limit = 200

[[vm]]
  memory = '256mb'
  cpu_kind = 'shared'
  cpus = 1
```

- [ ] **Step 2: Validate fly.toml syntax**

Run:
```bash
cd /Users/mirco.meazzo/virtual-assistant
# If fly CLI is installed:
fly config validate 2>/dev/null || echo "Install flyctl to validate — syntax looks correct from manual review"
```

Expected: Valid config (or a note to install flyctl).

- [ ] **Step 3: Commit**

```bash
git add fly.toml
git commit -m "feat: add Fly.io configuration for Voice Gateway

Frankfurt region (fra), auto-stop machines for $0 idle cost,
256MB shared-cpu VM. Secrets (OPENAI_KEY, BOOKING_ENGINE_URL)
set via fly secrets.

Co-authored-by: Isaac"
```

---

### Task 5: Deployment Scripts

Create shell scripts for deploying both services. These handle first-time setup (create resources) and subsequent updates (push new code).

**Files:**
- Create: `scripts/deploy-booking.sh`
- Create: `scripts/deploy-voice.sh`

- [ ] **Step 1: Create AWS Lambda deployment script**

Create `scripts/deploy-booking.sh`:

```bash
#!/usr/bin/env bash
#
# Deploy Booking Engine to AWS Lambda (container image).
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Docker installed and running
#
# First run creates ECR repo, IAM role, Lambda function, and Function URL.
# Subsequent runs just build, push, and update.
#
# Usage:
#   AWS_REGION=eu-central-1 DATABASE_URL=postgresql://... ./scripts/deploy-booking.sh
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-eu-central-1}"
ECR_REPO="booking-engine"
LAMBDA_FUNCTION="booking-engine-api"
ROLE_NAME="booking-engine-lambda-role"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "=== Booking Engine Lambda Deployment ==="
echo "Region: ${AWS_REGION}"
echo "Account: ${ACCOUNT_ID}"
echo ""

# ── Step 1: Ensure ECR repository exists ──
if ! aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" &>/dev/null; then
    echo "Creating ECR repository: ${ECR_REPO}"
    aws ecr create-repository \
        --repository-name "${ECR_REPO}" \
        --region "${AWS_REGION}" \
        --image-scanning-configuration scanOnPush=true
else
    echo "ECR repository exists: ${ECR_REPO}"
fi

# ── Step 2: Build Docker image ──
echo ""
echo "Building Docker image..."
docker build -f booking_engine/Dockerfile -t "${ECR_REPO}:latest" --platform linux/amd64 .

# ── Step 3: Push to ECR ──
echo ""
echo "Pushing to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${ECR_URI}"
docker tag "${ECR_REPO}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

# ── Step 4: Ensure IAM role exists ──
if ! aws iam get-role --role-name "${ROLE_NAME}" &>/dev/null; then
    echo ""
    echo "Creating IAM role: ${ROLE_NAME}"
    aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }'
    aws iam attach-role-policy \
        --role-name "${ROLE_NAME}" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    echo "Waiting 10s for IAM propagation..."
    sleep 10
fi
ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)

# ── Step 5: Create or update Lambda function ──
if ! aws lambda get-function --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}" &>/dev/null; then
    echo ""
    echo "Creating Lambda function: ${LAMBDA_FUNCTION}"
    aws lambda create-function \
        --function-name "${LAMBDA_FUNCTION}" \
        --package-type Image \
        --code "ImageUri=${ECR_URI}:latest" \
        --role "${ROLE_ARN}" \
        --timeout 30 \
        --memory-size 256 \
        --environment "Variables={DATABASE_URL=${DATABASE_URL:-},POOL_MIN_SIZE=1,POOL_MAX_SIZE=3}" \
        --region "${AWS_REGION}"

    echo "Waiting for function to be Active..."
    aws lambda wait function-active-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"

    echo "Creating Function URL (public, no auth)..."
    aws lambda create-function-url-config \
        --function-name "${LAMBDA_FUNCTION}" \
        --auth-type NONE \
        --region "${AWS_REGION}"

    # Allow public invocation
    aws lambda add-permission \
        --function-name "${LAMBDA_FUNCTION}" \
        --statement-id "FunctionURLAllowPublicAccess" \
        --action "lambda:InvokeFunctionUrl" \
        --principal "*" \
        --function-url-auth-type NONE \
        --region "${AWS_REGION}"
else
    echo ""
    echo "Updating Lambda function code..."
    aws lambda update-function-code \
        --function-name "${LAMBDA_FUNCTION}" \
        --image-uri "${ECR_URI}:latest" \
        --region "${AWS_REGION}"

    echo "Waiting for update to complete..."
    aws lambda wait function-updated-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"
fi

# ── Step 6: Print result ──
echo ""
echo "=== Deployment complete ==="
FUNC_URL=$(aws lambda get-function-url-config \
    --function-name "${LAMBDA_FUNCTION}" \
    --region "${AWS_REGION}" \
    --query 'FunctionUrl' --output text 2>/dev/null || echo "N/A")
echo "Function URL: ${FUNC_URL}"
echo ""
echo "To update DATABASE_URL:"
echo "  aws lambda update-function-configuration \\"
echo "    --function-name ${LAMBDA_FUNCTION} \\"
echo "    --environment 'Variables={DATABASE_URL=postgresql://...,POOL_MIN_SIZE=1,POOL_MAX_SIZE=3}' \\"
echo "    --region ${AWS_REGION}"
```

- [ ] **Step 2: Create Fly.io deployment script**

Create `scripts/deploy-voice.sh`:

```bash
#!/usr/bin/env bash
#
# Deploy Voice Gateway to Fly.io.
#
# Prerequisites:
#   - flyctl installed (brew install flyctl)
#   - Authenticated (fly auth login)
#
# First run creates the app. Subsequent runs just deploy.
#
# Usage:
#   ./scripts/deploy-voice.sh
#
#   # Set secrets (first time):
#   fly secrets set OPENAI_KEY=sk-... BOOKING_ENGINE_URL=https://xxx.lambda-url.eu-central-1.on.aws/
#
set -euo pipefail

APP_NAME="virtual-assistant-voice"

echo "=== Voice Gateway Fly.io Deployment ==="
echo "App: ${APP_NAME}"
echo ""

# Check if app exists
if ! fly apps list 2>/dev/null | grep -q "${APP_NAME}"; then
    echo "Creating Fly.io app: ${APP_NAME}"
    fly apps create "${APP_NAME}"
fi

echo "Deploying..."
fly deploy

echo ""
echo "=== Deployment complete ==="
echo "URL: https://${APP_NAME}.fly.dev"
echo ""
echo "Check status:  fly status"
echo "View logs:     fly logs"
echo "Set secrets:   fly secrets set OPENAI_KEY=sk-... BOOKING_ENGINE_URL=https://..."
```

- [ ] **Step 3: Make scripts executable**

```bash
chmod +x scripts/deploy-booking.sh scripts/deploy-voice.sh
```

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy-booking.sh scripts/deploy-voice.sh
git commit -m "feat: add deployment scripts for AWS Lambda and Fly.io

deploy-booking.sh: Builds Docker image, pushes to ECR, creates/updates
Lambda function with Function URL (no API Gateway). Handles first-time
setup (IAM role, ECR repo) and subsequent deploys.

deploy-voice.sh: Deploys Voice Gateway to Fly.io using fly.toml config.
Secrets (OPENAI_KEY, BOOKING_ENGINE_URL) managed via fly secrets.

Co-authored-by: Isaac"
```

---

### Task 6: Project Cleanup

Fix the malformed `.gitignore`, remove Databricks-specific files, and delete the old `app.yaml` deployment configs.

**Files:**
- Modify: `.gitignore`
- Delete: `.databricksignore`
- Delete: `booking_engine/app.yaml`
- Delete: `voice_gateway/app.yaml` (if present on disk)

- [ ] **Step 1: Fix .gitignore**

Replace the entire `.gitignore` with:

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
dist/
build/
.venv/

# Environment
.env
.env.*

# IDE
.vscode/
.idea/

# OS
.DS_Store

# Test
.pytest_cache/

# Databricks (legacy)
.databricks/

# Docker
*.tar

# Fly.io
.fly/
```

- [ ] **Step 2: Remove Databricks-specific files**

```bash
cd /Users/mirco.meazzo/virtual-assistant
rm -f .databricksignore
rm -f booking_engine/app.yaml
rm -f voice_gateway/app.yaml
```

- [ ] **Step 3: Run full test suite to confirm nothing is broken**

Run:
```bash
cd /Users/mirco.meazzo/virtual-assistant
python -m pytest tests/ -v --ignore=tests/live_db
```

Expected: All tests PASS. No imports reference deleted files.

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git rm --cached .databricksignore 2>/dev/null; git rm -f .databricksignore 2>/dev/null || true
git rm --cached booking_engine/app.yaml 2>/dev/null; git rm -f booking_engine/app.yaml 2>/dev/null || true
git commit -m "chore: fix .gitignore, remove Databricks deployment artifacts

Fix malformed '=0.29.0' line in .gitignore. Add patterns for .venv,
.env.*, .databricks/, .fly/. Remove .databricksignore and app.yaml
files (replaced by Lambda + Fly.io deployment).

Co-authored-by: Isaac"
```

---

## Deployment Runbook (Post-Implementation)

### First-Time Setup

**1. Deploy Booking Engine to AWS Lambda:**
```bash
export AWS_REGION=eu-central-1
export DATABASE_URL="postgresql://neondb_owner:npg_2oGETYCN3nsl@ep-weathered-term-agsfwl6w-pooler.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
./scripts/deploy-booking.sh
# Note the Function URL printed at the end
```

**2. Deploy Voice Gateway to Fly.io:**
```bash
# Install flyctl if needed: brew install flyctl
fly auth login
./scripts/deploy-voice.sh

# Set secrets (use the Lambda Function URL from step 1)
fly secrets set \
  OPENAI_KEY="sk-..." \
  BOOKING_ENGINE_URL="https://xxx.lambda-url.eu-central-1.on.aws/"
```

**3. Verify:**
```bash
# Booking Engine health check
curl https://xxx.lambda-url.eu-central-1.on.aws/health
# → {"status": "ok"}

# Voice Gateway health check
curl https://virtual-assistant-voice.fly.dev/health
# → {"status": "ok"}

# End-to-end: list services via Lambda
curl https://xxx.lambda-url.eu-central-1.on.aws/api/v1/shops/a0000000-0000-0000-0000-000000000001/services
```

### Subsequent Deploys

```bash
# Booking Engine
./scripts/deploy-booking.sh

# Voice Gateway
./scripts/deploy-voice.sh
# or simply: fly deploy
```

### Update Environment Variables

```bash
# Lambda (DATABASE_URL)
aws lambda update-function-configuration \
  --function-name booking-engine-api \
  --environment 'Variables={DATABASE_URL=postgresql://...,POOL_MIN_SIZE=1,POOL_MAX_SIZE=3}' \
  --region eu-central-1

# Fly.io (OPENAI_KEY)
fly secrets set OPENAI_KEY=sk-new-key
```

### Estimated Costs

| Component | Idle | Active (low traffic) |
|-----------|------|---------------------|
| Lambda (Booking Engine) | $0 | $0-1/mo |
| ECR (container image) | $0 (500MB free) | $0 |
| Fly.io (Voice Gateway) | $0 (free tier) | $0 |
| Neon (PostgreSQL) | $0 (free tier) | $0 |
| **Total infrastructure** | **$0** | **$0-1/mo** |

Plus usage-based costs: Twilio (per-minute), OpenAI Realtime (per-minute).
