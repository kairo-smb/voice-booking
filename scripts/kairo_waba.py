#!/usr/bin/env python3
"""Set up and drive Kairo's OWN WhatsApp Business Account, via Meta Cloud API.

This is the experimental/company WABA — the one App Review videos are recorded
against and where templates get validated before they're ever pushed into a
customer's WABA. It talks to graph.facebook.com directly: no Twilio, no BSP.

Order of operations (each step prints what to set for the next):

    add-number  -> request-code -> otp -> verify -> register -> send-template

`otp` reads Meta's verification SMS straight off the Twilio REST API instead of
needing an inbound webhook — Twilio stores inbound messages whether or not a
webhook is bound, and this number only ever receives one code in its life.

Env:
    META_TOKEN    system-user or temporary token from App Dashboard > API Setup
    META_WABA_ID  the WABA id (App Dashboard > WhatsApp > API Setup)
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN   only for `otp`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import httpx

# ponytail: pinned, not discovered. Graph is versioned and silently changes
# shape across versions; bump this deliberately when Meta's changelog says so.
GRAPH = "https://graph.facebook.com/v26.0"

_CODE = re.compile(r"\b(\d{3})[- ]?(\d{3})\b")


def _api(method: str, path: str, **kwargs) -> dict:
    token = os.environ["META_TOKEN"]
    r = httpx.request(
        method,
        f"{GRAPH}/{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        **kwargs,
    )
    body = r.json()
    if r.status_code >= 400:
        sys.exit(f"HTTP {r.status_code}\n{json.dumps(body, indent=2)}")
    return body


def _waba() -> str:
    return os.environ["META_WABA_ID"]


def add_number(a) -> None:
    """Attach a phone number to the WABA. Returns the phone number id."""
    out = _api(
        "POST",
        f"{_waba()}/phone_numbers",
        json={"cc": a.cc, "phone_number": a.number, "verified_name": a.name},
    )
    print(json.dumps(out, indent=2))
    print(f"\nexport META_PHONE_ID={out.get('id')}")


def request_code(a) -> None:
    _api(
        "POST",
        f"{a.phone_id}/request_code",
        data={"code_method": a.method, "language": a.language},
    )
    print(f"code requested via {a.method}; now run: kairo_waba.py otp --number ...")


def otp(a) -> None:
    """Pull Meta's verification code out of Twilio's inbound message log."""
    from twilio.rest import Client

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    for msg in client.messages.list(to=a.number, limit=20):
        found = _CODE.search(msg.body or "")
        if found:
            print(f"{found.group(1)}{found.group(2)}   (from {msg.from_}, {msg.date_sent})")
            return
    sys.exit("no 6-digit code found in the last 20 inbound messages")


def verify(a) -> None:
    print(_api("POST", f"{a.phone_id}/verify_code", data={"code": a.code}))


def register(a) -> None:
    """Final step: without this the number is on the WABA but NOT on Cloud API."""
    print(
        _api(
            "POST",
            f"{a.phone_id}/register",
            json={"messaging_product": "whatsapp", "pin": a.pin},
        )
    )


def push_templates(a) -> None:
    """Submit the repo's own catalogue to this WABA, for approval."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from booking_engine.services.messaging.whatsapp_templates import CATALOGUE

    for key, tpl in CATALOGUE.items():
        payload = {
            "name": key,
            "language": tpl.language,
            "category": tpl.category,
            "components": [
                {
                    "type": "BODY",
                    "text": tpl.body,
                    # Meta rejects a body with variables and no example.
                    "example": {
                        "body_text": [
                            [tpl.sample[str(i)] for i in range(1, tpl.variables + 1)]
                        ]
                    },
                }
            ],
        }
        print(key, _api("POST", f"{_waba()}/message_templates", json=payload))


def templates(a) -> None:
    out = _api("GET", f"{_waba()}/message_templates", params={"limit": 50})
    for t in out.get("data", []):
        print(f"{t['status']:10} {t['category']:12} {t['name']} ({t['language']})")


def send_template(a) -> None:
    """The App Review video #1 shot: a real send, from our app, to a handset."""
    params = [{"type": "text", "text": v} for v in a.var]
    body = {
        "messaging_product": "whatsapp",
        "to": a.to,
        "type": "template",
        "template": {
            "name": a.template,
            "language": {"code": a.language},
            **({"components": [{"type": "body", "parameters": params}]} if params else {}),
        },
    }
    print(json.dumps(_api("POST", f"{a.phone_id}/messages", json=body), indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(required=True)
    phone = os.environ.get("META_PHONE_ID")

    c = sub.add_parser("add-number"); c.set_defaults(f=add_number)
    c.add_argument("--cc", required=True, help="country code, no +, e.g. 372")
    c.add_argument("--number", required=True, help="national number, no cc")
    c.add_argument("--name", required=True, help="display name shown to customers")

    c = sub.add_parser("request-code"); c.set_defaults(f=request_code)
    c.add_argument("--phone-id", default=phone, required=not phone)
    c.add_argument("--method", default="SMS", choices=["SMS", "VOICE"])
    c.add_argument("--language", default="en_US")

    c = sub.add_parser("otp"); c.set_defaults(f=otp)
    c.add_argument("--number", required=True, help="E.164, the Twilio number")

    c = sub.add_parser("verify"); c.set_defaults(f=verify)
    c.add_argument("--phone-id", default=phone, required=not phone)
    c.add_argument("--code", required=True)

    c = sub.add_parser("register"); c.set_defaults(f=register)
    c.add_argument("--phone-id", default=phone, required=not phone)
    c.add_argument("--pin", required=True, help="6 digits you choose and keep")

    sub.add_parser("push-templates").set_defaults(f=push_templates)
    sub.add_parser("templates").set_defaults(f=templates)

    c = sub.add_parser("send-template"); c.set_defaults(f=send_template)
    c.add_argument("--phone-id", default=phone, required=not phone)
    c.add_argument("--to", required=True, help="E.164 recipient")
    c.add_argument("--template", default="hello_world")
    c.add_argument("--language", default="en_US")
    c.add_argument("--var", action="append", default=[], help="repeat, in order")

    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
