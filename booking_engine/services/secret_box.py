"""Encryption at rest for `whatsapp.senders.access_token`.

That column is the single most dangerous secret this service holds: full
authority over one salon's WhatsApp Business Account, with **no shared parent
credential behind it** to revoke. Twilio's subaccount token, which it replaced,
at least sat under Kairo's own account; a Meta business token does not. It was
stored in plaintext from 2026-08-24 and flagged as such in the same entry.

**Sealed values are prefixed, legacy ones are not**, which is what lets this
land without a migration or a backfill window: a row written before the key
existed still reads, and is re-sealed the next time anything writes it.

Deliberately Fernet and not pgcrypto. The key then lives in the app's own
secret store rather than in the database it protects, so a dump of the database
— the thing this exists to defend against — carries no way to read it.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Marks a value this module produced. Anything without it is a plaintext row
# from before the key was configured, and is returned untouched.
_PREFIX = "v1:"


class SecretBoxError(RuntimeError):
    """A sealed value that cannot be opened — wrong key, or no key at all."""


def _box(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretBoxError("WHATSAPP_TOKEN_KEY is not a valid Fernet key") from exc


def seal(value: str | None, key: str) -> str | None:
    """Encrypt for storage. Without a key, stores as before and says so.

    Unconfigured is not an error: it is the state every environment is in
    until the key is set, and refusing would take WhatsApp offline to fix a
    problem that is about a database dump. It is logged at every write, which
    is noisy on purpose — this is not a state to settle into.
    """
    if value is None or value == "":
        return value
    if not key:
        logger.warning("secret_box.unconfigured — storing a WhatsApp token in "
                       "plaintext; set WHATSAPP_TOKEN_KEY")
        return value
    if value.startswith(_PREFIX):
        return value
    return _PREFIX + _box(key).encrypt(value.encode()).decode()


def unseal(value: str | None, key: str) -> str | None:
    """Decrypt for use. Plaintext passes through; a sealed value must open.

    Failing loudly here is the point. A sealed token returned as its own
    ciphertext would reach Meta as a bearer token and come back as a generic
    auth error, which reads like an expired token and would send the salon
    through a reconnect that cannot fix it.
    """
    if value is None or not value.startswith(_PREFIX):
        return value
    if not key:
        raise SecretBoxError("WhatsApp token is sealed but WHATSAPP_TOKEN_KEY is unset")
    try:
        return _box(key).decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise SecretBoxError("WhatsApp token does not open with this key") from exc
