"""The application's connection to a dockaccess.org account (``/fast-api/integrations/*``) - the same
handshake the WordPress, PrestaShop, Joomla, Magento, Laravel and Symfony integrations use.

A connected application appears in the account (the *ALT generator* tab of the site) with its system and
the time it last checked in; the owner can disconnect it there.

The login and the password travel once, in the body of an HTTPS request that is never redirected and never
repeated after it may have reached the server, and are not kept.
"""

from __future__ import annotations

import platform
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import _protocol
from ._http import Answer, AsyncHttp, SyncHttp
from ._version import __version__
from .config import Config
from .errors import ConfigurationError, TransportError
from .options import is_uuid
from .results import Alt, Connection, Verification
from .secret import Redactor, Secret
from .text import clean_text

SERVICE = "dockaccess.org"
_SYSTEM = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
_TOKEN = re.compile(r"^[^\s\x00-\x1f\x7f]{1,255}$")

AltLike = Alt | tuple[str, str]


@dataclass(frozen=True)
class ConnectRequest:
    """What an application sends to connect itself.

    - ``site_token`` - in the account: My sites, the site, the *ALT generator* tab,
    - ``login`` and ``password`` - of the dockaccess.org account (not of your application); used once,
    - ``home_url`` - the application's public address; it has to be one of the site's domains
      (a local address such as ``http://localhost:8000`` is accepted too, for development),
    - ``system`` - what the account shows, ``python`` by default,
    - ``callback_url`` - where dockaccess.org may push alts edited in the account (optional),
    - ``overwrite`` - replace a connection that already exists for this address.
    """

    site_token: str
    login: str
    password: str = field(repr=False)
    home_url: str
    system: str = "python"
    system_version: str | None = field(default_factory=platform.python_version)
    module_version: str | None = __version__
    callback_url: str | None = None
    overwrite: bool = False

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        token = self.site_token.strip() if isinstance(self.site_token, str) else ""
        if not _TOKEN.match(token):
            raise ConfigurationError("The site token is empty or not valid.")
        set_(self, "site_token", token)
        login = self.login.strip() if isinstance(self.login, str) else ""
        if login == "" or not isinstance(self.password, str) or self.password == "":
            raise ConfigurationError("The login and the password of the dockaccess.org account are required.")
        set_(self, "login", login)
        system = self.system.strip().lower() if isinstance(self.system, str) else ""
        if not _SYSTEM.match(system):
            raise ConfigurationError('The system must be 2 to 40 characters from a-z, 0-9, ".", "_" and "-".')
        set_(self, "system", system)
        if not _is_http_url(self.home_url):
            raise ConfigurationError("The application address must be an absolute http(s) URL.")
        set_(self, "home_url", self.home_url.strip())
        if self.callback_url is not None and not _is_http_url(self.callback_url):
            raise ConfigurationError("The callback address must be an absolute http(s) URL.")
        set_(self, "system_version", _version(self.system_version))
        set_(self, "module_version", _version(self.module_version))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self.site_token,
            "login": self.login,
            "password": self.password,
            "system": self.system,
            "home_url": self.home_url,
        }
        for key in ("system_version", "module_version", "callback_url"):
            if getattr(self, key) is not None:
                payload[key] = getattr(self, key)
        if self.overwrite:
            payload["overwrite"] = True
        return payload


class _ConnectionBase:
    def __init__(self, config: Config | None) -> None:
        self._config = config or Config()

    def _url(self, path: str) -> str:
        return self._config.connection_url + path

    def _alts(self, alts: Iterable[AltLike]) -> list[dict[str, str]]:
        items = []
        for alt in alts:
            text, locale = (alt.text, alt.locale) if isinstance(alt, Alt) else alt
            cleaned = clean_text(text, self._config.max_alt_length)
            if cleaned:
                items.append({"alt": cleaned, "locale": locale})
        if not items:
            raise ConfigurationError("At least one non-empty alt is required.")
        if len(items) > 5:
            raise ConfigurationError("At most five alts (one per language) go in one update.")
        return items

    @staticmethod
    def _uuid(image_uuid: str) -> str:
        if not is_uuid(image_uuid):
            raise ConfigurationError("The image UUID is not valid.")
        return image_uuid.lower()

    @staticmethod
    def _transport_error(error: TransportError) -> TransportError:
        return TransportError(str(error).replace("The alt generator", SERVICE), error.retryable)


class ConnectionClient(_ConnectionBase):
    """Connects, verifies and disconnects the application; sends alts edited in it back to the account.

    Nothing here is repeated automatically: a connect that reached the server rotates the connection
    secret, and a repeated failed login counts towards the lockout.
    """

    def __init__(self, config: Config | None = None, *, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(config)
        self._http = SyncHttp(self._config, transport)

    @classmethod
    def _sharing(cls, config: Config, http: SyncHttp) -> ConnectionClient:
        client = cls.__new__(cls)
        _ConnectionBase.__init__(client, config)
        client._http = http
        return client

    def connect(self, request: ConnectRequest) -> Connection:
        """Connects the application and hands back the account key and the connection secret.

        Raises :class:`SiteConnectionError` (see its ``reason``; ``already_connected`` asks for
        ``overwrite=True``), :class:`ServerError`, :class:`TransportError`."""
        answer = self._send("POST", "/integrations/connect", request.to_payload(), Redactor())
        data = _protocol.connection_json(answer, Redactor())
        return _protocol.connection(data, request.site_token, request.system, request.home_url, answer.status)

    def verify(self, site_token: str, secret: Secret | str, *, with_usage: bool = False) -> Verification:
        """Whether the site may generate now. Cheap (no login, no password); it also refreshes "last checked
        in" in the account. A disconnected site raises :class:`SiteConnectionError` with
        ``is_connection_gone``."""
        key = Secret.of(secret)
        if key is None:
            raise ConfigurationError("The connection secret is missing.")
        payload = {"token": site_token, "secret": key.reveal(), "usage": with_usage}
        answer = self._send("POST", "/integrations/verify", payload, Redactor(key))
        return _protocol.verification(_protocol.connection_json(answer, Redactor(key)))

    def disconnect(self, site_token: str, login: str, password: str, home_url: str) -> None:
        """Removes the connection. ``not_connected`` means it was gone already - treat that as done."""
        payload = {"token": site_token, "login": login, "password": password, "home_url": home_url}
        _protocol.connection_json(self._send("POST", "/integrations/disconnect", payload, Redactor()), Redactor())

    def update_alts(self, site_token: str, image_uuid: str, alts: Iterable[AltLike]) -> None:
        """Sends alts edited in the application (one per language, at most five) back to the account, so the
        account shows the same text. Needs the account key."""
        key = self._config.require_account_key()
        payload = {
            "image_uuid": self._uuid(image_uuid),
            "key": key.reveal(),
            "token": site_token,
            "alts": self._alts(alts),
        }
        _protocol.connection_json(self._send("PUT", "/alts/update", payload, Redactor(key)), Redactor(key))

    def _send(self, method: str, path: str, payload: dict[str, Any], redact: Redactor) -> Answer:
        request = self._http.build(method, self._url(path), json=payload)
        try:
            return self._http.send(request, service=SERVICE, redact=redact)
        except TransportError as error:
            raise self._transport_error(error) from None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ConnectionClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncConnectionClient(_ConnectionBase):
    """The asyncio/trio twin of :class:`ConnectionClient`."""

    def __init__(self, config: Config | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(config)
        self._http = AsyncHttp(self._config, transport)

    @classmethod
    def _sharing(cls, config: Config, http: AsyncHttp) -> AsyncConnectionClient:
        client = cls.__new__(cls)
        _ConnectionBase.__init__(client, config)
        client._http = http
        return client

    async def connect(self, request: ConnectRequest) -> Connection:
        answer = await self._send("POST", "/integrations/connect", request.to_payload(), Redactor())
        data = _protocol.connection_json(answer, Redactor())
        return _protocol.connection(data, request.site_token, request.system, request.home_url, answer.status)

    async def verify(self, site_token: str, secret: Secret | str, *, with_usage: bool = False) -> Verification:
        key = Secret.of(secret)
        if key is None:
            raise ConfigurationError("The connection secret is missing.")
        payload = {"token": site_token, "secret": key.reveal(), "usage": with_usage}
        answer = await self._send("POST", "/integrations/verify", payload, Redactor(key))
        return _protocol.verification(_protocol.connection_json(answer, Redactor(key)))

    async def disconnect(self, site_token: str, login: str, password: str, home_url: str) -> None:
        payload = {"token": site_token, "login": login, "password": password, "home_url": home_url}
        answer = await self._send("POST", "/integrations/disconnect", payload, Redactor())
        _protocol.connection_json(answer, Redactor())

    async def update_alts(self, site_token: str, image_uuid: str, alts: Iterable[AltLike]) -> None:
        key = self._config.require_account_key()
        payload = {
            "image_uuid": self._uuid(image_uuid),
            "key": key.reveal(),
            "token": site_token,
            "alts": self._alts(alts),
        }
        answer = await self._send("PUT", "/alts/update", payload, Redactor(key))
        _protocol.connection_json(answer, Redactor(key))

    async def _send(self, method: str, path: str, payload: dict[str, Any], redact: Redactor) -> Answer:
        request = self._http.build(method, self._url(path), json=payload)
        try:
            return await self._http.send(request, service=SERVICE, redact=redact)
        except TransportError as error:
            raise self._transport_error(error) from None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncConnectionClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def _is_http_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname) and not parts.username


def _version(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z._\- ]", "", str(value)).strip()[:40]
    return cleaned or None
