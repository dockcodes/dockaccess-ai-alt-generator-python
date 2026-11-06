"""Reading the services' answers - shared by the sync and the async clients, no I/O here."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ._http import Answer
from .errors import (
    ApiError,
    AuthenticationError,
    DockaccessServerError,
    ImageRejectedError,
    LimitReachedError,
    RateLimitedError,
    ServerError,
    SiteConnectionError,
    UnexpectedResponseError,
)
from .image import PreparedImage
from .language import normalize_language
from .options import GenerateOptions, is_uuid
from .results import DAY, MONTH, Alt, Connection, GenerationResult, Usage, Verification
from .secret import Redactor, Secret
from .text import clean_text

MAX_MESSAGE = 300
DEFAULT_RETRY_AFTER = 60
_MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


def decode(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


# -- the generator (alt.dockaccess.org) -------------------------------------------------------------------------


def generator_json(answer: Answer, redact: Redactor) -> dict[str, Any]:
    """A successful answer as JSON, a refusal as the error that names it."""
    status = answer.status
    if 200 <= status < 300:
        data = decode(answer.body)
        if data is None:
            raise UnexpectedResponseError("The alt generator answered with something that is not JSON.", status)
        return data

    message = _generator_message(answer, redact)
    if status == 429:
        if answer.header("X-Alt-Reason") == "limit_reached":
            limit = answer.header("X-Alt-Daily-Limit")
            raise LimitReachedError(
                status,
                message,
                int(limit) if limit is not None and limit.isdigit() else None,
                answer.header("X-Alt-Limit-Period"),
            )
        raise RateLimitedError(status, message, _retry_after(answer.header("Retry-After")))
    if status == 401 or (status == 403 and "invalid key" in message.lower()):
        raise AuthenticationError(status, message)
    if status in (400, 413, 415):
        raise ImageRejectedError(status, message)
    if 500 <= status < 600:
        raise ServerError(status, message)
    if 400 <= status < 500:
        raise ApiError(status, message)
    raise UnexpectedResponseError(f"The alt generator answered with HTTP {status}.", status)


def generation_result(
    data: dict[str, Any], options: GenerateOptions, prepared: PreparedImage, status: int, max_length: int
) -> GenerationResult:
    payload = data.get("data")
    image = payload.get("image") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(image, dict):
        raise UnexpectedResponseError("The answer has no image.", status)

    image_uuid, set_uuid, set_ref = image.get("uuid"), payload.get("set_uuid"), payload.get("set_ref")
    if not is_uuid(image_uuid) or not is_uuid(set_uuid):
        raise UnexpectedResponseError("The answer lacks valid image or set identifiers.", status)

    alts = []
    for item in _list(image.get("alts")):
        if not isinstance(item, dict) or not isinstance(item.get("alt"), str):
            continue
        text = clean_text(item["alt"], max_length)
        locale = item.get("locale")
        language = normalize_language(locale, options.language) if isinstance(locale, str) else options.language
        if text:
            alts.append(Alt(text, language))
    if not alts:
        raise UnexpectedResponseError("The generator returned no usable alt text.", status)

    return GenerationResult(
        image_uuid=str(image_uuid).lower(),
        set_uuid=str(set_uuid).lower(),
        set_ref=set_ref if isinstance(set_ref, str) else None,
        alts=tuple(alts),
        image=prepared,
    )


def usage(data: dict[str, Any], status: int) -> Usage:
    requests = data.get("requests")
    found = _usage(requests)
    if found is None:
        raise UnexpectedResponseError("The usage answer lacks its numbers.", status)
    return found


def _usage(requests: Any) -> Usage | None:
    if not isinstance(requests, dict):
        return None
    used, limit = requests.get("used"), requests.get("limit")
    if not isinstance(used, int) or not isinstance(limit, int) or isinstance(used, bool) or isinstance(limit, bool):
        return None
    return Usage(
        used=max(0, used),
        limit=max(0, limit),
        period=MONTH if requests.get("period") == MONTH else DAY,
        resets_at=_moment(requests.get("resets_at")),
    )


def _generator_message(answer: Answer, redact: Redactor) -> str:
    """The API's ``detail`` - a string, or FastAPI's list of validation errors - cleaned for a message."""
    data = decode(answer.body)
    detail = data.get("detail") if data else None
    if isinstance(detail, list) and detail and isinstance(detail[0], dict) and isinstance(detail[0].get("msg"), str):
        detail = detail[0]["msg"]
    if not isinstance(detail, str) or not detail.strip():
        return "No details given."
    return clean_text(redact(detail), MAX_MESSAGE) or "No details given."


def _retry_after(value: str | None) -> int:
    if value is None or not value.strip().isdigit():
        return DEFAULT_RETRY_AFTER
    return max(1, min(3600, int(value.strip())))


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not _MOMENT.match(value):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# -- dockaccess.org (/fast-api/integrations, /fast-api/alts) ------------------------------------------------------


def connection_json(answer: Answer, redact: Redactor) -> dict[str, Any]:
    status = answer.status
    data = decode(answer.body)
    if 200 <= status < 300:
        if data is None:
            raise UnexpectedResponseError("dockaccess.org answered with something that is not JSON.", status)
        return data

    message = _connection_message(data, redact)
    reason = data.get("reason") if data else None
    if 500 <= status < 600:
        raise DockaccessServerError(status, message)
    if 400 <= status < 500:
        if isinstance(reason, str) and reason in SiteConnectionError.REASONS:
            known = reason
        elif status == 429:
            known = SiteConnectionError.TOO_MANY_ATTEMPTS
        elif status in (400, 422):
            known = SiteConnectionError.INVALID_REQUEST
        else:
            known = SiteConnectionError.REFUSED
        raise SiteConnectionError(status, message, known)
    raise UnexpectedResponseError(f"dockaccess.org answered with HTTP {status}.", status)


def connection(data: dict[str, Any], site_token: str, system: str, home_url: str, status: int) -> Connection:
    account_key = data.get("external_token")
    integration = _dict(data.get("integration"))
    token = _dict(data.get("token"))
    user = _dict(data.get("user"))
    secret = integration.get("secret")

    if not isinstance(account_key, str) or account_key == "":
        raise UnexpectedResponseError("The account has no generator key yet. Try again in a minute.", status)
    if not isinstance(secret, str) or not _is_int(integration.get("id")):
        raise UnexpectedResponseError("The connection answer lacks the connection secret.", status)
    try:
        key, connection_secret = Secret(account_key), Secret(secret)
    except ValueError:
        raise UnexpectedResponseError(
            "The connection answer carries keys in a form the client does not accept.", status
        ) from None

    return Connection(
        site_token=token["token"] if isinstance(token.get("token"), str) else site_token,
        account_key=key,
        secret=connection_secret,
        integration_id=integration["id"],
        system=integration["system"] if isinstance(integration.get("system"), str) else system,
        home_url=integration["home_url"] if isinstance(integration.get("home_url"), str) else home_url,
        alt_enabled=token.get("alt_enabled") is True,
        domain_verified=integration.get("domain_verified") is True,
        account_name=clean_text(user["display_name"] if isinstance(user.get("display_name"), str) else "", 190),
    )


def verification(data: dict[str, Any]) -> Verification:
    token = _dict(data.get("token"))
    integration = _dict(data.get("integration"))
    alt = _dict(data.get("alt"))
    if data.get("alt_enabled") is True:
        state = Verification.ACTIVE
    elif token.get("alt_paused") is True:
        state = Verification.PAUSED
    else:
        state = Verification.DISABLED
    websites = _dict(alt.get("websites"))
    return Verification(
        state=state,
        integration_id=integration["id"] if _is_int(integration.get("id")) else 0,
        system=integration["system"] if isinstance(integration.get("system"), str) else "",
        usage=_usage(alt.get("requests")),
        websites_used=websites["used"] if _is_int(websites.get("used")) else None,
        websites_limit=websites["limit"] if _is_int(websites.get("limit")) else None,
    )


def _connection_message(data: dict[str, Any] | None, redact: Redactor) -> str:
    """``detail`` is a sentence, or an object of field => sentence for a request that failed validation."""
    detail = data.get("detail") if data else None
    if isinstance(detail, dict):
        detail = " ".join(value for value in detail.values() if isinstance(value, str))
    elif isinstance(detail, list):
        detail = " ".join(value for value in detail if isinstance(value, str))
    if not isinstance(detail, str) or not detail.strip():
        return "No details given."
    return clean_text(redact(detail), MAX_MESSAGE) or "No details given."
