"""Appointment CRUD routes."""
from uuid import UUID
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from booking_engine.api.models import AppointmentResponse, CreateAppointmentRequest, RescheduleRequest, ErrorResponse
from booking_engine.time import shop_time
from booking_engine.db.queries import create_appointment, list_appointments, cancel_appointment, reschedule_appointment, SlotConflictError

router = APIRouter(tags=["appointments"])

@router.post("/shops/{shop_id}/appointments", response_model=AppointmentResponse, status_code=201, responses={409: {"model": ErrorResponse}})
async def book_appointment(shop_id: UUID, body: CreateAppointmentRequest):
    # A naive start_time is what the caller said: the shop's wall clock -> UTC.
    start = shop_time.ensure_instant(body.start_time, await shop_time.shop_tz(shop_id))
    try:
        appt = await create_appointment(shop_id, body.customer_id, body.staff_id, body.service_ids, start, body.notes)
        return appt
    except SlotConflictError:
        return JSONResponse(status_code=409, content={"error": "slot_taken", "message": "Time slot is already booked"})

@router.get("/shops/{shop_id}/appointments", response_model=list[AppointmentResponse])
async def read_appointments(shop_id: UUID, customer_id: UUID | None = Query(None), status: str | None = Query(None)):
    return await list_appointments(shop_id, customer_id, status)

@router.patch("/shops/{shop_id}/appointments/{appointment_id}/cancel", response_model=AppointmentResponse, responses={409: {"model": ErrorResponse}})
async def cancel(shop_id: UUID, appointment_id: UUID):
    result = await cancel_appointment(shop_id, appointment_id)
    if not result:
        return JSONResponse(status_code=409, content={"error": "appointment_not_cancellable", "message": "Appointment cannot be cancelled"})
    return result

@router.patch("/shops/{shop_id}/appointments/{appointment_id}/reschedule", response_model=AppointmentResponse, responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def reschedule(shop_id: UUID, appointment_id: UUID, body: RescheduleRequest):
    new_start = shop_time.ensure_instant(body.new_start_time, await shop_time.shop_tz(shop_id))
    try:
        result = await reschedule_appointment(shop_id, appointment_id, new_start, body.new_staff_id)
        if not result:
            return JSONResponse(status_code=404, content={"error": "appointment_not_found", "message": "Appointment not found or not reschedulable"})
        return result
    except SlotConflictError:
        return JSONResponse(status_code=409, content={"error": "slot_taken", "message": "New time slot is already booked"})
