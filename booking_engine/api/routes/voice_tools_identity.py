"""Voice tool endpoints — identity (lookup, create, update)."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    CreateCustomerIn, CreatedCustomerOut, CustomerSummary, Envelope,
    LookupCustomerIn, UpdateCustomerIn,
)
from booking_engine.db.voice_calls_queries import get_call
from booking_engine.db.voice_tool_queries import (
    attach_customer_to_call, find_customers_by_phone, get_customer_shop_id,
    insert_customer_from_call, update_customer_field,
)
from booking_engine.services.phone_normalize import digits_only

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-identity"])


@router.post("/lookup_customer")
async def lookup_customer(
    body: LookupCustomerIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[CustomerSummary]]:
    rows = await find_customers_by_phone(
        shop_id=x_shop_id, phone_digits=digits_only(body.phone),
    )
    summaries = [CustomerSummary(
        customer_id=r["id"], first_name=r["first_name"] or "",
        last_name=r.get("last_name"), last_visit_at=r.get("last_visit_at"),
        preferred_staff_id=r.get("preferred_staff_id"),
        notes_tags=r.get("notes_tags") or [], verified=r.get("verified", True),
    ) for r in rows]
    return Envelope[list[CustomerSummary]](ok=True, data=summaries)


@router.post("/create_customer_from_call")
async def create_customer(
    body: CreateCustomerIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[CreatedCustomerOut]:
    new_id = await insert_customer_from_call(
        shop_id=x_shop_id, phone=body.phone, first_name=body.first_name,
        last_name=body.last_name,
        phone_verified=(body.phone_source == "caller_id"),
        created_by_call_id=x_call_id,
    )
    await attach_customer_to_call(call_id=x_call_id, created_customer_id=new_id)
    return Envelope[CreatedCustomerOut](
        ok=True, data=CreatedCustomerOut(customer_id=new_id),
    )


@router.post("/update_customer_from_call")
async def update_customer(
    body: UpdateCustomerIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    """Edit a customer this session's shop actually owns.

    Server-side trust boundary, the same shape as
    `booking_authz.authorize_booking_change`: the shop is read off the **call
    row** we wrote ourselves, not off the X-Shop-Id header, and the customer's
    owning shop comes from the database. `customer_id` arrives as a tool
    argument from the model and is untrusted input like any other.

    Until now there was no check at all: any validly minted token could edit
    any customer's email/tags in any shop (flagged 2026-07-17). Sessions are
    now minted on a second channel (WhatsApp), so this is closed.
    """
    call = await get_call(x_call_id)
    if not call:
        return Envelope[dict](ok=False, error="call_not_found")
    owner_shop_id = await get_customer_shop_id(customer_id=body.customer_id)
    if owner_shop_id is None:
        return Envelope[dict](ok=False, error="customer_not_found")
    if str(owner_shop_id) != str(call["shop_id"]):
        return Envelope[dict](ok=False, error="wrong_shop")

    ok = await update_customer_field(
        customer_id=body.customer_id, field=body.field, value=body.value,
    )
    return Envelope[dict](ok=ok, data={"updated": ok})