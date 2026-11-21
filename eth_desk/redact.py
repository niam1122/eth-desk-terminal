from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Field names that must never be written to a log line.
SECRET_KEY = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret|passphrase|private[_-]?key|password|mnemonic|credential)",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"


def secrets_from_env(env: Mapping[str, str]) -> tuple[str, ...]:
    values = []
    for name, value in env.items():
        if value and SECRET_KEY.search(name):
            values.append(value)
    return tuple(values)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Return a copy with secret-like fields and known secret strings removed."""

    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact(item, secrets)
        return cleaned
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secrets) for item in value)
    if isinstance(value, str):
        cleaned = value
        for secret in secrets:
            if secret and len(secret) >= 8 and secret in cleaned:
                cleaned = cleaned.replace(secret, REDACTED)
        return cleaned
    return value
