"""Receiving alt texts that dockaccess.org pushes to a connected application.

One contract for every integration: ``POST <callback_url>`` with a JSON body, signed with the connection
secret (``X-Dockaccess-Signature: sha256=<hex hmac of "<timestamp>.<raw body>">``,
``X-Dockaccess-Timestamp``). :func:`parse_push` enforces every rule the other integrations enforce:

- nothing is read before the signature verifies; the comparison is constant time,
- the timestamp must be within 300 seconds of this server's clock,
- every rejection of the signature answers the same 401 with the same message,
- a site token that is not this site's answers 403, a request without usable alts 400,
- an application that is not connected answers 503.

Your handler writes the alts and answers :func:`push_response`; 404 when no local image matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import AltGeneratorError
from .language import normalize_language
from .options import is_uuid
from .results import Alt
from .secret import Secret
from .text import clean_text

MAX_BODY = 64 * 1024
MAX_SKEW = 300
MAX_ALTS = 5
_SIGNATURE_PREFIX = "sha256="
_REJECTED = "The request could not be verified."


class PushError(AltGeneratorError):
    """A push that must be refused; answer :attr:`status` with :meth:`to_response`."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.status, str(self)))

    def to_response(self) -> dict[str, Any]:
        return {"success": False, "message": str(self)}


@dataclass(frozen=True)
class PushedAlts:
    """A verified push: the image (by its UUID in the account and/or its public URL) and its alts, the most
    relevant language first. Alts are already plain text."""

    site_token: str
    image_uuid: str | None
    image_url: str | None
    alts: tuple[Alt, ...]

    def for_language(self, language: str) -> Alt:
        """The alt in ``language`` (a code or a locale), else the first one."""
        code = normalize_language(language, language)
        return next((alt for alt in self.alts if alt.locale == code), self.alts[0])


def parse_push(
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: Secret | str | None,
    site_token: str | None,
    now: float | None = None,
) -> PushedAlts:
    """Verifies and reads a push, or raises :class:`PushError` with the status to answer."""
    key = Secret.of(secret)
    if key is None or not site_token:
        raise PushError(503, "This site is not connected to dockaccess.org.")

    lowered = {name.lower(): value for name, value in headers.items()}
    timestamp = (lowered.get("x-dockaccess-timestamp") or "").strip()
    signature = (lowered.get("x-dockaccess-signature") or "").strip()
    if len(body) > MAX_BODY or not timestamp.isdigit() or not signature.startswith(_SIGNATURE_PREFIX):
        raise PushError(401, _REJECTED)
    moment = time.time() if now is None else now
    if abs(moment - int(timestamp)) > MAX_SKEW:
        raise PushError(401, _REJECTED)
    expected = hmac.new(key.reveal().encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature[len(_SIGNATURE_PREFIX) :].lower().encode(), expected.encode()):
        raise PushError(401, _REJECTED)

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PushError(400, "The body is not JSON.") from None
    if not isinstance(data, dict):
        raise PushError(400, "The body is not a JSON object.")
    if not isinstance(data.get("token"), str) or not hmac.compare_digest(data["token"].encode(), site_token.encode()):
        raise PushError(403, "The token does not belong to this site.")

    image_uuid = data.get("image_uuid") if is_uuid(data.get("image_uuid")) else None
    image_url = data.get("image_url") if isinstance(data.get("image_url"), str) and data["image_url"].strip() else None
    if image_uuid is None and image_url is None:
        raise PushError(400, "Neither image_uuid nor image_url names the image.")

    alts = []
    found = data.get("alts")
    items: list[Any] = found if isinstance(found, list) else []
    for item in items[:MAX_ALTS]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("alt"), str)
            or not isinstance(item.get("locale"), str)
        ):
            continue
        text = clean_text(item["alt"], 2000)
        if text:
            alts.append(Alt(text, normalize_language(item["locale"], item["locale"].strip().lower()[:12])))
    if not alts:
        raise PushError(400, "The request carries no usable alt text.")

    return PushedAlts(
        site_token=site_token,
        image_uuid=image_uuid.lower() if image_uuid else None,
        image_url=image_url.strip() if image_url else None,
        alts=tuple(alts),
    )


def push_response(updated: int, local_id: Any = None, image_uuid: str | None = None) -> dict[str, Any]:
    """The 200 answer after the alts were written."""
    answer: dict[str, Any] = {"success": True, "updated": updated}
    if local_id is not None:
        answer["local_id"] = local_id
    if image_uuid is not None:
        answer["image_uuid"] = image_uuid
    return answer


def handler_answer(outcome: Any, pushed: PushedAlts | None = None) -> tuple[int, dict[str, Any]]:
    """Turns what a push handler returned into the answer: the number of alts written (or a dict from
    :func:`push_response`) is 200, ``None`` is 404 (no local image matches), ``0`` is 400."""
    if isinstance(outcome, dict):
        return 200, outcome
    if outcome is None:
        error = not_found()
        return error.status, error.to_response()
    if isinstance(outcome, bool) or not isinstance(outcome, int):
        raise TypeError("A push handler returns the number of alts written, push_response(...) or None.")
    if outcome < 1:
        return 400, PushError(400, "No alt text matched a language of this site.").to_response()
    return 200, push_response(outcome, image_uuid=pushed.image_uuid if pushed else None)


def not_found() -> PushError:
    """Raise it from your handler when no local image matches: the sender stops retrying."""
    return PushError(404, "No local image matches.")


def sign_push(body: bytes, secret: Secret | str, timestamp: int | None = None) -> dict[str, str]:
    """The headers dockaccess.org sends with ``body`` - for testing your endpoint."""
    key = Secret.of(secret)
    if key is None:
        raise ValueError("A secret is required.")
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(key.reveal().encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Dockaccess-Timestamp": stamp,
        "X-Dockaccess-Signature": _SIGNATURE_PREFIX + digest,
    }
