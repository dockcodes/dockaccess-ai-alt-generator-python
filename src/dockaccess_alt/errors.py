"""Every error the package raises derives from :class:`AltGeneratorError`.

None of them carries the account key, the connection secret, a password, the query of a URL or the
bytes of an image, so they can be logged and reported as they are. They also survive pickling with
their attributes, so Celery, RQ and multiprocessing pass them on intact.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AltGeneratorError(Exception):
    """Base class of every error raised by dockaccess_alt."""


class ConfigurationError(AltGeneratorError, ValueError):
    """A setting or an option is not valid, or the generator is not connected yet."""


class InvalidImageError(AltGeneratorError):
    """The image was refused locally; nothing was sent. Do not retry it, show :attr:`reason`."""

    NOT_ALLOWED = "not_allowed"
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    MALFORMED = "malformed"
    BAD_DIMENSIONS = "bad_dimensions"
    CANNOT_SANITIZE = "cannot_sanitize"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.reason, str(self)))


class ApiError(AltGeneratorError):
    """The service answered with a refusal. :attr:`api_message` is its own explanation, as plain text."""

    service = "The alt generator"

    def __init__(self, status: int, api_message: str) -> None:
        super().__init__(f"{self.service} refused the request (HTTP {status}): {api_message}")
        self.status = status
        self.api_message = api_message

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.status, self.api_message))


class AuthenticationError(ApiError):
    """The account key is unknown or disabled. Connect the application again."""


class LimitReachedError(ApiError):
    """The plan's limit is used up: the UTC day's on the free plan, the UTC month's on paid plans.

    Stop until the window renews (:meth:`AltGenerator.usage` tells when) or upgrade the plan.
    """

    def __init__(self, status: int, api_message: str, limit: int | None = None, period: str | None = None) -> None:
        super().__init__(status, api_message)
        self.limit = limit
        self.period = period if period in ("day", "month") else None

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.status, self.api_message, self.limit, self.period))


class RateLimitedError(ApiError):
    """Too many requests in a minute. Wait :attr:`retry_after` seconds and try again."""

    def __init__(self, status: int, api_message: str, retry_after: int = 60) -> None:
        super().__init__(status, api_message)
        self.retry_after = retry_after

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.status, self.api_message, self.retry_after))


class ImageRejectedError(ApiError):
    """The generator refused the image (HTTP 400, 413 or 415). Do not retry it."""


class ServerError(ApiError):
    """The service failed (5xx). Try again later, with a backoff."""


class DockaccessServerError(ServerError):
    """dockaccess.org (not the generator) failed."""

    service = "dockaccess.org"


class SiteConnectionError(ApiError):
    """dockaccess.org refused to connect, verify or disconnect the site; :attr:`reason` says why."""

    service = "dockaccess.org"

    INVALID_CREDENTIALS = "invalid_credentials"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    TOKEN_NOT_OWNED = "token_not_owned"  # noqa: S105 - a reason, not a password
    DOMAIN_NOT_ALLOWED = "domain_not_allowed"
    ALT_DISABLED = "alt_disabled"
    ALT_PAUSED = "alt_paused"
    ALREADY_CONNECTED = "already_connected"
    DISCONNECTED = "disconnected"
    NOT_CONNECTED = "not_connected"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    INVALID_REQUEST = "invalid_request"
    REFUSED = "refused"

    REASONS = frozenset(
        {
            INVALID_CREDENTIALS,
            EMAIL_NOT_VERIFIED,
            TOKEN_NOT_OWNED,
            DOMAIN_NOT_ALLOWED,
            ALT_DISABLED,
            ALT_PAUSED,
            ALREADY_CONNECTED,
            DISCONNECTED,
            NOT_CONNECTED,
            TOO_MANY_ATTEMPTS,
        }
    )

    def __init__(self, status: int, api_message: str, reason: str = REFUSED) -> None:
        super().__init__(status, api_message)
        self.reason = reason

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.status, self.api_message, self.reason))

    @property
    def is_connection_gone(self) -> bool:
        """After verify() or disconnect(): the account no longer knows this connection; forget it."""
        return self.reason in (self.DISCONNECTED, self.INVALID_CREDENTIALS, self.NOT_CONNECTED)


class GeneratorUnavailableError(AltGeneratorError):
    """The account says no before anything is sent: the site was disconnected, or the generator was
    switched off (``alt_disabled``) or paused by the plan (``alt_paused``)."""

    DISCONNECTED = "disconnected"
    DISABLED = "alt_disabled"
    PAUSED = "alt_paused"

    _MESSAGES: ClassVar[dict[str, str]] = {
        DISCONNECTED: "The site was disconnected in the dockaccess.org account. Connect it again.",
        DISABLED: "The alt generator is switched off for this site in the dockaccess.org account.",
        PAUSED: "The plan paused the alt generator for this site. Upgrade the plan or free up a site.",
    }

    def __init__(self, reason: str) -> None:
        super().__init__(self._MESSAGES.get(reason, "The alt generator is not available for this site."))
        self.reason = reason

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.reason,))


class TransportError(AltGeneratorError):
    """No complete answer arrived (DNS, TLS, timeout, an oversized answer).

    :attr:`retryable` is true only when the request certainly never reached the service, so a retry
    cannot be charged twice.
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (str(self), self.retryable))


class UnexpectedResponseError(AltGeneratorError):
    """An answer the client does not understand (a redirect, invalid JSON, missing fields). Log it."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (str(self), self.status))


def error_response(error: AltGeneratorError) -> tuple[int, dict[str, Any], dict[str, str]]:
    """What a web application can answer its own client when generation fails: ``(status, body, headers)``.

    Refusals of the image are the client's to fix (422); a spent limit is 429; everything that is the
    server's or dockaccess.org's business is 503/502 with a message that reveals nothing about the account.
    """
    if isinstance(error, InvalidImageError):
        return 422, {"detail": str(error), "reason": error.reason}, {}
    if isinstance(error, ImageRejectedError):
        return 422, {"detail": "The alt generator could not read this image.", "reason": "image_rejected"}, {}
    if isinstance(error, LimitReachedError):
        window = "month" if error.period == "month" else "day"
        return 429, {"detail": f"The alt text limit of this {window} is used up.", "reason": "limit_reached"}, {}
    if isinstance(error, RateLimitedError):
        detail = {"detail": "Too many alt texts at once. Try again shortly.", "reason": "rate_limited"}
        return 429, detail, {"Retry-After": str(error.retry_after)}
    if isinstance(error, (GeneratorUnavailableError, AuthenticationError, ConfigurationError)):
        return 503, {"detail": "The alt generator is not available.", "reason": "unavailable"}, {}
    return 502, {"detail": "The alt generator could not be reached. Try again later.", "reason": "upstream"}, {}


__all__ = [
    "AltGeneratorError",
    "ApiError",
    "AuthenticationError",
    "ConfigurationError",
    "GeneratorUnavailableError",
    "ImageRejectedError",
    "InvalidImageError",
    "LimitReachedError",
    "RateLimitedError",
    "ServerError",
    "SiteConnectionError",
    "TransportError",
    "UnexpectedResponseError",
    "error_response",
]
