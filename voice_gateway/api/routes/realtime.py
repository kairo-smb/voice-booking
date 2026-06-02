"""Realtime API: token generation + function call proxy for booking actions."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/realtime", tags=["realtime"])

OPENAI_REALTIME_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_MODEL = "gpt-realtime"

logger = logging.getLogger(__name__)


@router.post("/token")
async def get_realtime_token(request: Request, shop_id: str = Query(...)):
    """Generate an ephemeral OpenAI Realtime API token with session config."""
    app = request.app
    openai_key = app.state._openai_key
    if not openai_key:
        return JSONResponse(status_code=500, content={"error": "OpenAI key not configured"})

    # Load shop data for the system prompt
    booking = app.state.booking_client
    shop = await booking.get_shop(shop_id)
    if not shop:
        return JSONResponse(status_code=404, content={"error": "Shop not found"})

    services = await booking.get_services(shop_id)
    staff = await booking.get_staff(shop_id)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    services_str = ", ".join(s.get("service_name", "") for s in services)
    staff_str = ", ".join(s.get("full_name", "") for s in staff)
    voice = shop.get("voice") or "alloy"
    language = shop.get("language") or "it"

    instructions = (
        f"{shop.get('personality', '')}\n"
        f"{shop.get('tone_instructions', '')}\n\n"
        f"Data e ora corrente: {now}\n"
        f"Servizi disponibili: {services_str}\n"
        f"Staff disponibile: {staff_str}\n\n"
        "REGOLE:\n"
        f"- Rispondi SEMPRE in {language}\n"
        "- Sii breve e naturale, come una vera telefonata\n"
        "- Tono allegro, solare e accogliente\n"
        "- NON usare emoji o simboli\n"
        "- Se il cliente dice il suo nome, salutalo e chiedi come puoi aiutare\n"
        "- Se chiede servizi, elencali brevemente\n"
        "- Se vuole prenotare o sapere la disponibilità, usa lo strumento check_availability\n"
        "- Se il cliente saluta per andarsene, salutalo calorosamente e chiudi"
    )

    # Define tools for function calling
    tools = [
        {
            "type": "function",
            "name": "check_availability",
            "description": "Controlla la disponibilità per un servizio in una data specifica",
            "parameters": {
                "type": "object",
                "properties": {
                    "services": {"type": "array", "items": {"type": "string"}, "description": "Nomi dei servizi richiesti"},
                    "date": {"type": "string", "description": "Data in formato YYYY-MM-DD"},
                    "staff_name": {"type": "string", "description": "Nome dello staff preferito (opzionale)"},
                },
                "required": ["services"],
            },
        },
        {
            "type": "function",
            "name": "get_services",
            "description": "Ottieni la lista completa dei servizi con prezzi e durata",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "create_customer",
            "description": "Registra un nuovo cliente con nome e telefono",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nome completo del cliente"},
                    "phone": {"type": "string", "description": "Numero di telefono (opzionale)"},
                },
                "required": ["name"],
            },
        },
        {
            "type": "function",
            "name": "book_appointment",
            "description": "Prenota un appuntamento per un cliente. Usa dopo aver verificato la disponibilità.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string", "description": "Nome del cliente"},
                    "service_name": {"type": "string", "description": "Nome del servizio"},
                    "staff_name": {"type": "string", "description": "Nome dello staff"},
                    "date": {"type": "string", "description": "Data in formato YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Ora in formato HH:MM"},
                },
                "required": ["customer_name", "service_name", "staff_name", "date", "time"],
            },
        },
        {
            "type": "function",
            "name": "list_appointments",
            "description": "Mostra gli appuntamenti di un cliente",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string", "description": "Nome del cliente"},
                },
                "required": ["customer_name"],
            },
        },
    ]

    # Request ephemeral client_secret from OpenAI Realtime API.
    # Endpoint and body shape per https://platform.openai.com/docs/api-reference/realtime
    # (replaces the legacy /v1/realtime/sessions endpoint deprecated in 2025).
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            OPENAI_REALTIME_URL,
            headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
            json={
                "session": {
                    "type": "realtime",
                    "model": OPENAI_MODEL,
                    "instructions": instructions,
                    "tools": tools,
                    "audio": {
                        "input": {
                            "transcription": {"model": "gpt-4o-mini-transcribe"},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 800,
                                "create_response": True,
                            },
                        },
                        "output": {"voice": voice},
                    },
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()

    # Create and persist a CallSession for this call
    from voice_gateway.call_lifecycle import CallSession
    caller_number = request.headers.get("x-caller-number") or "+0000"
    twilio_sid = request.headers.get("x-twilio-call-sid")
    sess = CallSession(shop_id=UUID(shop_id), caller_number=caller_number, twilio_call_sid=twilio_sid)
    try:
        await sess.start()
    except Exception as e:
        logger.warning("CallSession.start failed: %s", e)
        sess.id = None
    if sess.id is not None:
        request.app.state.call_sessions[str(sess.id)] = sess

    # The new /client_secrets endpoint returns {value, expires_at} at top level;
    # tolerate both shapes for forward/backward compat with the legacy /sessions endpoint.
    _secret_obj = data.get("client_secret", data)
    return {
        "token": _secret_obj.get("value") or data.get("value"),
        "expires_at": _secret_obj.get("expires_at") or data.get("expires_at"),
        "model": data.get("model") or OPENAI_MODEL,
        "call_id": str(sess.id) if sess.id else None,
        "shop": {
            "id": shop_id,
            "name": shop.get("name"),
            "welcome_message": shop.get("welcome_message", "Ciao, benvenuto!"),
        },
        "services": [{"id": s.get("id"), "name": s.get("service_name"), "duration": s.get("duration_minutes"), "price": float(s.get("price_eur", 0))} for s in services],
        "staff": [{"id": s.get("id"), "name": s.get("full_name")} for s in staff],
    }


class FunctionCallRequest(BaseModel):
    shop_id: str
    function_name: str
    arguments: dict
    call_id: str | None = None


@router.post("/action")
async def execute_action(body: FunctionCallRequest, request: Request):
    """Proxy function calls from the Realtime API to the booking engine."""
    app = request.app
    booking = app.state.booking_client
    logger.info("Action: %s args=%s shop=%s", body.function_name, body.arguments, body.shop_id)

    # Resolve session if call_id provided
    sess = None
    if body.call_id:
        sess = request.app.state.call_sessions.get(body.call_id)
    if sess:
        try:
            await sess.log_event("function_call",
                                 {"name": body.function_name, "args": body.arguments})
        except Exception as e:
            logger.warning("log_event(function_call) failed: %s", e)

    try:
        if body.function_name == "check_availability":
            service_names = body.arguments.get("services", [])
            services_list = await booking.get_services(body.shop_id)
            # Resolve service names to IDs
            svc_ids = []
            for name in service_names:
                nl = name.lower()
                for svc in services_list:
                    if nl in svc.get("service_name", "").lower():
                        svc_ids.append(svc["id"])
                        break
            if not svc_ids:
                if sess:
                    try:
                        await sess.log_event("function_result", {"name": body.function_name, "slots": []})
                    except Exception as e:
                        logger.warning("log_event(function_result) failed: %s", e)
                return {"slots": [], "message": "Servizio non trovato"}

            date_str = body.arguments.get("date")
            if date_str:
                start = date.fromisoformat(date_str)
            else:
                start = date.today() + timedelta(days=1)
            end = start

            staff_name = body.arguments.get("staff_name")
            staff_id = None
            if staff_name:
                staff_list = await booking.get_staff(body.shop_id)
                nl = staff_name.lower()
                for s in staff_list:
                    if nl in s.get("full_name", "").lower():
                        staff_id = s["id"]
                        break

            result = await booking.check_availability(body.shop_id, svc_ids, start, end, staff_id)
            if sess:
                try:
                    await sess.log_event("function_result", {"name": body.function_name})
                except Exception as e:
                    logger.warning("log_event(function_result) failed: %s", e)
            return result

        elif body.function_name == "get_services":
            services = await booking.get_services(body.shop_id)
            if sess:
                try:
                    await sess.log_event("function_result", {"name": body.function_name})
                except Exception as e:
                    logger.warning("log_event(function_result) failed: %s", e)
            return {"services": [{"name": s.get("service_name"), "duration": s.get("duration_minutes"), "price": float(s.get("price_eur", 0))} for s in services]}

        elif body.function_name == "create_customer":
            name = body.arguments.get("name", "")
            phone = body.arguments.get("phone")
            new_customer = await booking.create_customer(body.shop_id, name, phone)
            if sess and new_customer and new_customer.get("id"):
                try:
                    await sess.attach_new_customer(UUID(new_customer["id"]))
                    await sess.log_event("function_result", {"name": "create_customer", "customer_id": new_customer["id"]})
                except Exception as e:
                    logger.warning("session hook for create_customer failed: %s", e)
            elif sess:
                try:
                    await sess.log_event("function_result", {"name": body.function_name})
                except Exception as e:
                    logger.warning("log_event(function_result) failed: %s", e)
            return {"created": True, "name": name, "id": new_customer.get("id") if new_customer else None}

        elif body.function_name == "book_appointment":
            # Resolve customer, service, staff by name
            customer_name = body.arguments.get("customer_name", "")
            service_name = body.arguments.get("service_name", "")
            staff_name_arg = body.arguments.get("staff_name", "")
            date_str = body.arguments.get("date", "")
            time_str = body.arguments.get("time", "")

            # Find or create customer
            customers = await booking.find_customers_by_phone(body.shop_id, "")
            customer_id = None
            # Search by name in existing customers
            all_customers_resp = await booking.find_customer_by_name_phone(body.shop_id, customer_name, "")
            if all_customers_resp:
                customer_id = all_customers_resp[0].get("id")
            if not customer_id:
                new_cust = await booking.create_customer(body.shop_id, customer_name)
                customer_id = new_cust.get("id") if new_cust else None

            if not customer_id:
                if sess:
                    try:
                        await sess.log_event("function_result", {"name": body.function_name, "error": "customer not found or created"})
                    except Exception as e:
                        logger.warning("log_event(function_result) failed: %s", e)
                return {"error": "Impossibile trovare o creare il cliente"}

            # Resolve service
            services_list = await booking.get_services(body.shop_id)
            service_id = None
            for svc in services_list:
                if service_name.lower() in svc.get("service_name", "").lower():
                    service_id = svc["id"]
                    break
            if not service_id:
                if sess:
                    try:
                        await sess.log_event("function_result", {"name": body.function_name, "error": f"service not found: {service_name}"})
                    except Exception as e:
                        logger.warning("log_event(function_result) failed: %s", e)
                return {"error": f"Servizio '{service_name}' non trovato"}

            # Resolve staff
            staff_list = await booking.get_staff(body.shop_id)
            staff_id = None
            for s in staff_list:
                if staff_name_arg.lower() in s.get("full_name", "").lower():
                    staff_id = s["id"]
                    break
            if not staff_id:
                if sess:
                    try:
                        await sess.log_event("function_result", {"name": body.function_name, "error": f"staff not found: {staff_name_arg}"})
                    except Exception as e:
                        logger.warning("log_event(function_result) failed: %s", e)
                return {"error": f"Staff '{staff_name_arg}' non trovato"}

            # Build start_time
            start_time = f"{date_str}T{time_str}:00+01:00"

            appt = await booking.book_appointment(
                shop_id=body.shop_id,
                customer_id=customer_id,
                service_ids=[service_id],
                staff_id=staff_id,
                start_time=start_time,
            )
            if appt:
                if sess:
                    try:
                        sess.set_appointment(UUID(appt["id"]))
                        await sess.log_event("function_result", {"name": "book_appointment", "appointment_id": appt["id"]})
                    except Exception as e:
                        logger.warning("session hook for book_appointment failed: %s", e)
                return {"booked": True, "appointment_id": appt.get("id"), "start_time": start_time, "staff": staff_name_arg, "service": service_name}
            if sess:
                try:
                    await sess.log_event("function_result", {"name": body.function_name, "error": "booking failed"})
                except Exception as e:
                    logger.warning("log_event(function_result) failed: %s", e)
            return {"error": "Errore nella prenotazione"}

        elif body.function_name == "list_appointments":
            customer_name = body.arguments.get("customer_name", "")
            # Find customer by name
            customers = await booking.find_customer_by_name_phone(body.shop_id, customer_name, "")
            if not customers:
                if sess:
                    try:
                        await sess.log_event("function_result", {"name": body.function_name, "appointments": []})
                    except Exception as e:
                        logger.warning("log_event(function_result) failed: %s", e)
                return {"appointments": [], "message": "Cliente non trovato"}
            customer_id = customers[0].get("id")
            appts = await booking.list_appointments(body.shop_id, customer_id)
            if sess:
                try:
                    await sess.log_event("function_result", {"name": body.function_name})
                except Exception as e:
                    logger.warning("log_event(function_result) failed: %s", e)
            return {"appointments": [{"id": a.get("id"), "start_time": str(a.get("start_time")), "status": a.get("status"), "staff": a.get("staff_name")} for a in appts]}

        else:
            if sess:
                try:
                    await sess.log_event("function_result", {"name": body.function_name, "error": "unknown function"})
                except Exception as e:
                    logger.warning("log_event(function_result) failed: %s", e)
            return {"error": f"Unknown function: {body.function_name}"}

    except Exception as exc:
        if sess:
            try:
                await sess.log_event("error", {"name": body.function_name, "detail": str(exc)})
            except Exception as e:
                logger.warning("log_event(error) failed: %s", e)
        return {"error": str(exc)}


class TurnIn(BaseModel):
    call_id: str
    role: str          # 'caller' | 'assistant' | 'system'
    text: str


@router.post("/transcript")
async def post_transcript(request: Request, body: TurnIn):
    sess = request.app.state.call_sessions.get(body.call_id)
    if not sess:
        return {"ok": False}
    try:
        await sess.append_turn(role=body.role, text=body.text,
                               at=datetime.now(timezone.utc))
    except Exception as e:
        logger.warning("append_turn failed: %s", e)
        return {"ok": False}
    return {"ok": True}


class EndIn(BaseModel):
    call_id: str


@router.post("/end")
async def end_call(request: Request, body: EndIn):
    from voice_gateway.clients.openai_classifier import classify_call
    sess = request.app.state.call_sessions.pop(body.call_id, None)
    if not sess:
        return {"ok": False}
    try:
        await sess.finalize(
            classifier=classify_call,
            api_key=request.app.state._openai_key,
            model=getattr(request.app.state, "_classifier_model", "gpt-4o-mini"),
        )
    except Exception as e:
        try:
            await sess.log_event("error", {"phase": "finalize", "detail": str(e)})
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    return {"ok": True}
