"""Error tracking → self-hosted GlitchTip (webapp repo: infra/glitchtip).

Called once from asgi.py, the production entrypoint; tests build the app with
create_app() and never init. No SENTRY_DSN (local) = SDK disabled.

The default integrations already cover what we need: FastAPI (unhandled
exceptions), logging (every ``logger.error`` becomes an event, without touching
the call sites), httpx (trace headers to our own services).
"""
from __future__ import annotations

import os
import re
from typing import Any

import sentry_sdk

# The SDK scrubs by key name, but frame locals hold the raw ASGI scope — headers as
# a list of byte pairs — so a bearer token or a salon's Meta token rides along by
# value. Anyone reading GlitchTip could act as the caller with it.
_CREDENTIAL = re.compile(r"(Bearer|Basic)\s+[\w.~+/=-]+|EAA[A-Za-z0-9]{20,}")


def redact_credentials(value: Any) -> Any:
    if isinstance(value, str):
        return _CREDENTIAL.sub("[Filtered]", value)
    if isinstance(value, dict):
        return {k: redact_credentials(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_credentials(v) for v in value]
    return value


def init() -> None:
    image = os.environ.get("FLY_IMAGE_REF", "")  # …:deployment-<id> — one release per deploy
    sentry_sdk.init(
        dsn=os.environ.get("SENTRY_DSN") or None,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "development"),
        release=f"voice-booking@{image.rsplit(':', 1)[-1] or 'local'}",
        # Errors only; 0 still continues the caller's trace id and passes it on.
        traces_sample_rate=0,
        # Full collection, by owner decision (2026-09-25): IPs, headers, cookies,
        # whole request bodies and every frame's local variables. The default
        # event_scrubber still filters credential-looking keys.
        send_default_pii=True,
        max_request_body_size="always",
        include_local_variables=True,
        # Trace headers to our own services only — never Twilio, Meta or OpenAI.
        trace_propagation_targets=[
            u for u in (os.environ.get("WEBAPP_BASE_URL"), os.environ.get("MARKET_INTEL_API_URL")) if u
        ],
        before_send=lambda event, _hint: redact_credentials(event),
    )
