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
