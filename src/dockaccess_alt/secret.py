"""A credential that does not print itself."""

from __future__ import annotations

import hmac
import re
from typing import Any, NoReturn

from .errors import ConfigurationError

REDACTED = "[redacted]"

_INVALID = re.compile(r"[\s\x00-\x1f\x7f]")


class Secret:
    """Holds an account key, a connection secret or a proxy URL.

    It shows as ``[redacted]`` in ``str()``, ``repr()``, f-strings, logging and debuggers' variable views,
    and refuses to be pickled, so it cannot slip into a Celery message, a cache or a session by accident.
    :meth:`reveal` hands the value out where it is really needed.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise ConfigurationError("A secret must be a string.")
        value = value.strip()
        if value == "" or len(value) > 4096 or _INVALID.search(value):
            raise ConfigurationError("The secret is empty, too long or contains whitespace or control characters.")
        self._value = value

    @classmethod
    def of(cls, value: Secret | str | None) -> Secret | None:
        """A Secret from a string, the same Secret, or None for an empty value."""
        if value is None or isinstance(value, Secret):
            return value
        if isinstance(value, str) and value.strip() == "":
            return None
        return cls(value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret('{REDACTED}')"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Secret:
        return self

    def __reduce__(self) -> NoReturn:
        raise TypeError("A Secret cannot be pickled: keep keys out of queues, caches and sessions.")


class Redactor:
    """Removes secrets (raw and URL-encoded) and URL queries from text that may reach a message."""

    _QUERY = re.compile(r"(https?://[^\s?#\"'<>]*)\?[^\s\"'<>]*", re.IGNORECASE)

    def __init__(self, *secrets: Secret | None) -> None:
        self._secrets = [s for s in secrets if s is not None]

    def __call__(self, text: str) -> str:
        from urllib.parse import quote, quote_plus

        for secret in self._secrets:
            value = secret.reveal()
            for form in {value, quote(value, safe=""), quote_plus(value)}:
                text = text.replace(form, REDACTED)
        return self._QUERY.sub(r"\1?" + REDACTED, text)
