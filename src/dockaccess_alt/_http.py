"""The HTTP layer shared by the sync and the async clients.

- HTTPS with certificate and host name verification, TLS 1.2 or newer; never a fallback to plain HTTP.
- Redirects are never followed: the key and the image go only to the configured host.
- Connect and total timeouts are always set; an answer above 1 MiB aborts the transfer.
- Only failures before anything was sent (DNS, a refused connection, a connect timeout) are retried, so an
  image is never submitted twice and the limit is not used twice.
- The account key is added to the URL by an ``httpx.Auth`` hook, so the requests this package holds do not
  carry it, and it is masked in the ``httpx`` logger's "HTTP Request: ..." lines.
"""

from __future__ import annotations

import logging
import os
import ssl
import time
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from .config import Config
from .errors import TransportError
from .secret import REDACTED, Redactor, Secret

MAX_RESPONSE_BYTES = 1024 * 1024
_ACCEPT = {"Accept": "application/json"}

logger = logging.getLogger("dockaccess_alt")


class _MaskKeyInLogs(logging.Filter):
    """httpx logs every request URL at INFO level; the generator's URL carries the key in its query."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(isinstance(arg, httpx.URL) for arg in record.args):
            record.args = tuple(
                arg.copy_set_param("key", REDACTED) if isinstance(arg, httpx.URL) and "key" in arg.params else arg
                for arg in record.args
            )
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(item, _MaskKeyInLogs) for item in _httpx_logger.filters):
    _httpx_logger.addFilter(_MaskKeyInLogs())


class KeyAuth(httpx.Auth):
    """Adds ``key=<account key>`` to the query at the last moment."""

    def __init__(self, key: Secret) -> None:
        self._key = key

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.url = request.url.copy_set_param("key", self._key.reveal())
        yield request

    def __repr__(self) -> str:
        return "KeyAuth([redacted])"


@dataclass(frozen=True)
class Answer:
    status: int
    headers: httpx.Headers
    body: bytes

    def header(self, name: str) -> str | None:
        value = self.headers.get(name)
        return value if isinstance(value, str) else None


def ssl_context(config: Config) -> ssl.SSLContext:
    cafile = config.ca_bundle
    capath = None
    if cafile is None and config.trust_env:
        cafile = os.environ.get("SSL_CERT_FILE") or None
        capath = os.environ.get("SSL_CERT_DIR") or None
    if cafile is None and capath is None:
        import certifi

        cafile = certifi.where()
    context = ssl.create_default_context(cafile=cafile, capath=capath)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def client_options(config: Config) -> dict[str, Any]:
    proxy = config.proxy.reveal() if isinstance(config.proxy, Secret) else None
    return {
        "timeout": httpx.Timeout(config.timeout, connect=config.connect_timeout),
        "follow_redirects": False,
        "verify": ssl_context(config),
        "proxy": proxy,
        "trust_env": config.trust_env,
        "headers": {"User-Agent": config.user_agent},
    }


def _describe(error: httpx.HTTPError, service: str, redact: Redactor) -> TransportError:
    detail = redact(str(error))[:200] or type(error).__name__
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return TransportError(f"{service} could not be reached: {detail}", retryable=True)
    if isinstance(error, httpx.TimeoutException):
        return TransportError(f"{service} did not answer in time.", retryable=False)
    return TransportError(f"{service} did not answer completely: {detail}", retryable=False)


class SyncHttp:
    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._client = httpx.Client(transport=transport, **client_options(config))

    def build(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        files: Any = None,
    ) -> httpx.Request:
        return self._client.build_request(method, url, params=params, json=json, files=files, headers=_ACCEPT)

    def send(
        self,
        request: httpx.Request,
        *,
        service: str,
        redact: Redactor,
        auth: httpx.Auth | None = None,
        retries: int = 0,
    ) -> Answer:
        attempt = 0
        while True:
            try:
                return self._once(request, service=service, redact=redact, auth=auth)
            except TransportError as error:
                if not error.retryable or attempt >= retries:
                    raise
                time.sleep(0.25 * 2**attempt)
                attempt += 1

    def _once(self, request: httpx.Request, *, service: str, redact: Redactor, auth: httpx.Auth | None) -> Answer:
        deadline = time.monotonic() + self._config.timeout
        try:
            response = self._client.send(request, auth=auth, stream=True)
        except httpx.HTTPError as error:
            raise _describe(error, service, redact) from None
        try:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise TransportError(f"{service} answered with more data than allowed.")
                if time.monotonic() > deadline:
                    raise TransportError(f"{service} did not answer in time.")
        except httpx.HTTPError as error:
            raise _describe(error, service, redact) from None
        finally:
            response.close()
        return Answer(response.status_code, response.headers, bytes(body))

    def close(self) -> None:
        self._client.close()


class AsyncHttp:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._client = httpx.AsyncClient(transport=transport, **client_options(config))

    def build(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        files: Any = None,
    ) -> httpx.Request:
        return self._client.build_request(method, url, params=params, json=json, files=files, headers=_ACCEPT)

    async def send(
        self,
        request: httpx.Request,
        *,
        service: str,
        redact: Redactor,
        auth: httpx.Auth | None = None,
        retries: int = 0,
    ) -> Answer:
        attempt = 0
        while True:
            try:
                with anyio.fail_after(self._config.timeout):
                    return await self._once(request, service=service, redact=redact, auth=auth)
            except TimeoutError:
                raise TransportError(f"{service} did not answer in time.") from None
            except TransportError as error:
                if not error.retryable or attempt >= retries:
                    raise
                await anyio.sleep(0.25 * 2**attempt)
                attempt += 1

    async def _once(self, request: httpx.Request, *, service: str, redact: Redactor, auth: httpx.Auth | None) -> Answer:
        try:
            response = await self._client.send(request, auth=auth, stream=True)
        except httpx.HTTPError as error:
            raise _describe(error, service, redact) from None
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise TransportError(f"{service} answered with more data than allowed.")
        except httpx.HTTPError as error:
            raise _describe(error, service, redact) from None
        finally:
            await response.aclose()
        return Answer(response.status_code, response.headers, bytes(body))

    async def aclose(self) -> None:
        await self._client.aclose()
