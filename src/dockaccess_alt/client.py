"""The alt generator: :class:`AltGenerator` (sync) and :class:`AsyncAltGenerator` (asyncio and trio)."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.to_thread
import httpx

from . import _protocol
from ._http import AsyncHttp, KeyAuth, SyncHttp
from .config import Config
from .connection import AltLike, AsyncConnectionClient, ConnectionClient
from .errors import (
    AltGeneratorError,
    AuthenticationError,
    ConfigurationError,
    GeneratorUnavailableError,
    LimitReachedError,
    RateLimitedError,
    ServerError,
    SiteConnectionError,
    TransportError,
    UnexpectedResponseError,
)
from .image import ImageLike, ImageSource, PreparedImage, prepare_image
from .options import GenerateOptions
from .results import GenerationResult, Usage, Verification
from .secret import Redactor
from .tone import Tone

logger = logging.getLogger("dockaccess_alt")

SERVICE = "The alt generator"

#: Errors after which a batch stops: nothing that follows could succeed.
FATAL = (LimitReachedError, AuthenticationError, GeneratorUnavailableError, ConfigurationError)

#: How long the generator keeps going without a connection check when dockaccess.org cannot be reached.
UNREACHABLE_GRACE = 60.0


@dataclass(frozen=True)
class BatchItem:
    """One image of :meth:`AltGenerator.generate_many`: its key and either the result or the error."""

    key: Any
    result: GenerationResult | None = None
    error: AltGeneratorError | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None

    @property
    def stopped(self) -> bool:
        """The batch stopped after this image (the limit is spent, the key was refused, the site is off)."""
        return isinstance(self.error, FATAL)


class _Base:
    def __init__(self, config: Config) -> None:
        if not isinstance(config, Config):
            raise ConfigurationError("AltGenerator takes a Config; use AltGenerator.from_env() for the environment.")
        self._config = config
        self._redact = Redactor(config.account_key, config.connection_secret)  # type: ignore[arg-type]
        self._verified_until = 0.0
        self._refusal: GeneratorUnavailableError | None = None

    @property
    def config(self) -> Config:
        return self._config

    def _options(self, options: GenerateOptions | None, overrides: dict[str, Any]) -> GenerateOptions:
        base = options if options is not None else self._config.default_options()
        if not isinstance(base, GenerateOptions):
            raise ConfigurationError("options must be GenerateOptions.")
        return base.replace(**overrides)

    def _source(self, image: ImageLike, name: str | None) -> ImageSource:
        return ImageSource.of(image, name, max_bytes=self._config.image_policy.max_bytes)

    def _generate_request(
        self, http: SyncHttp | AsyncHttp, options: GenerateOptions, prepared: PreparedImage
    ) -> httpx.Request:
        return http.build(
            "POST",
            self._config.base_url + "/api/generate",
            params=options.to_query(),
            files={"file": (prepared.name, prepared.data, prepared.mime_type)},
        )

    def _site(self) -> tuple[str, Any]:
        if not self._config.site_token or self._config.connection_secret is None:
            raise ConfigurationError("No site token or connection secret is configured. Run dockaccess-alt connect.")
        return self._config.site_token, self._config.connection_secret

    # -- the connection check: shared bookkeeping --------------------------------------------------------------

    def _needs_check(self) -> bool:
        if not self._config.guards_connection:
            return False
        if time.monotonic() < self._verified_until:
            if self._refusal is not None:
                raise self._refusal
            return False
        return True

    def _remember(self, verification: Verification | None, error: Exception | None) -> None:
        ttl = self._config.verify_ttl
        if verification is not None:
            self._refusal = None if verification.is_active else GeneratorUnavailableError(verification.state)
            self._verified_until = time.monotonic() + ttl
        elif isinstance(error, SiteConnectionError) and error.is_connection_gone:
            self._refusal = GeneratorUnavailableError(GeneratorUnavailableError.DISCONNECTED)
            self._verified_until = time.monotonic() + ttl
        elif isinstance(error, SiteConnectionError) and error.reason in (
            SiteConnectionError.ALT_DISABLED,
            SiteConnectionError.ALT_PAUSED,
        ):
            self._refusal = GeneratorUnavailableError(error.reason)
            self._verified_until = time.monotonic() + ttl
        else:
            # dockaccess.org could not tell: generate anyway - the generator itself still enforces the limits.
            logger.warning("Could not check the dockaccess.org connection, generating anyway: %s", error)
            self._refusal = None
            self._verified_until = time.monotonic() + min(ttl, UNREACHABLE_GRACE)
        if self._refusal is not None:
            raise self._refusal

    @staticmethod
    def _batch_items(images: Iterable[Any], within: Any) -> Iterator[tuple[Any, Any]]:
        for item in images:
            key, image = item if isinstance(item, tuple) and len(item) == 2 else (item, item)
            if within is not None and isinstance(image, (str, os.PathLike)):
                yield key, ("path", image)
            else:
                yield key, ("image", image)

    def _read(self, entry: tuple[str, Any], within: Any) -> ImageSource:
        kind, image = entry
        if kind == "path":
            return ImageSource.from_path(image, within=within, max_bytes=self._config.image_policy.max_bytes)
        return self._source(image, None)


class AltGenerator(_Base):
    """The dockaccess.org alt generator, synchronous. Thread-safe; open one per process and reuse it::

        generator = AltGenerator.from_env()
        result = generator.generate(upload, language="pl", context="Product: oak dining table")
        print(result.text)

    Every image is verified and rebuilt from its pixels before it is sent (see :func:`prepare_image`);
    a file that is not a genuine image never leaves the server.
    """

    def __init__(self, config: Config, *, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(config)
        self._http = SyncHttp(config, transport)
        self._connections = ConnectionClient._sharing(config, self._http)
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        environ: Any = None,
        *,
        env_file: str | os.PathLike[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        **overrides: Any,
    ) -> AltGenerator:
        """Configured from ``DOCKACCESS_ALT_*`` variables (see :meth:`Config.from_env`)."""
        return cls(Config.from_env(environ, env_file=env_file, **overrides), transport=transport)

    def prepare(self, image: ImageLike, name: str | None = None) -> PreparedImage:
        """Verifies and rebuilds an image without sending it - to refuse a bad file at upload time or before
        queueing a batch. Needs no key and no network."""
        return prepare_image(self._source(image, name), self._config.image_policy)

    def generate(
        self,
        image: ImageLike,
        options: GenerateOptions | None = None,
        *,
        name: str | None = None,
        language: str | None = None,
        tone: Tone | str | None = None,
        context: str | None = None,
        variants: int | None = None,
        set_ref: str | None = None,
        set_uuid: str | None = None,
    ) -> GenerationResult:
        """Generates alt text for one image; counts against the account's limit when it succeeds.

        ``image`` is bytes, a binary file, a Pillow image, an :class:`ImageSource` or a framework's upload.
        Keyword options override ``options`` (or the configured defaults) for this call.
        """
        opts = self._options(
            options,
            {
                "language": language,
                "tone": tone,
                "context": context,
                "variants": variants,
                "set_ref": set_ref,
                "set_uuid": set_uuid,
            },
        )
        key = self._config.require_account_key()
        prepared = self.prepare(image, name)
        self._check_connection()
        answer = self._http.send(
            self._generate_request(self._http, opts, prepared),
            service=SERVICE,
            redact=self._redact,
            auth=KeyAuth(key),
            retries=self._config.connect_retries,
        )
        data = _protocol.generator_json(answer, self._redact)
        return _protocol.generation_result(data, opts, prepared, answer.status, self._config.max_alt_length)

    def generate_many(
        self,
        images: Iterable[Any],
        options: GenerateOptions | None = None,
        *,
        within: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | None = None,
        max_rate_limit_waits: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        **overrides: Any,
    ) -> Iterator[BatchItem]:
        """Generates one image after another, the way the CMS plugins' bulk runs do.

        ``images`` holds images or ``(key, image)`` pairs - the key (a database id, a path) comes back in each
        :class:`BatchItem`. With ``within``, paths are read from inside that directory. A refused image is
        reported and skipped; the per-minute cap is waited out; a spent limit, a refused key or a disabled
        site stop the batch after reporting it (:attr:`BatchItem.stopped`).
        """
        waits = 0
        for key, entry in self._batch_items(images, within):
            while True:
                try:
                    result = self.generate(self._read(entry, within), options, **overrides)
                except RateLimitedError as error:
                    if waits >= max_rate_limit_waits:
                        yield BatchItem(key, error=error)
                        return
                    waits += 1
                    sleep(error.retry_after)
                    continue
                except AltGeneratorError as error:
                    yield BatchItem(key, error=error)
                    if isinstance(error, FATAL):
                        return
                    break
                yield BatchItem(key, result=result)
                break

    def usage(self) -> Usage:
        """The account's usage in the current window (the UTC day on the free plan, the month on paid ones)."""
        key = self._config.require_account_key()
        request = self._http.build("GET", self._config.base_url + "/api/user")
        answer = self._http.send(
            request, service=SERVICE, redact=self._redact, auth=KeyAuth(key), retries=self._config.connect_retries
        )
        return _protocol.usage(_protocol.generator_json(answer, self._redact), answer.status)

    def verify(self, *, with_usage: bool = False) -> Verification:
        """Asks dockaccess.org whether this site may generate now (and, if asked, the account's usage)."""
        token, secret = self._site()
        return self._connections.verify(token, secret, with_usage=with_usage)

    def update_alts(self, image_uuid: str, alts: Iterable[AltLike]) -> None:
        """Sends alts edited in your application back to the account: ``[("Oak table", "en"), ...]``."""
        token, _ = self._site()
        self._connections.update_alts(token, image_uuid, alts)

    def _check_connection(self) -> None:
        if not self._needs_check():
            return
        with self._lock:
            if not self._needs_check():
                return
            token, secret = self._site()
            try:
                verification = self._connections.verify(token, secret)
            except (SiteConnectionError, TransportError, ServerError, UnexpectedResponseError) as error:
                self._remember(None, error)
                return
            self._remember(verification, None)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AltGenerator:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncAltGenerator(_Base):
    """The dockaccess.org alt generator for asyncio and trio (FastAPI, Starlette, Litestar, Quart, aiohttp).

    Open one in the application's lifespan and reuse it::

        async with AsyncAltGenerator.from_env() as generator:
            result = await generator.generate(upload, language="pl")

    Images are verified and rebuilt in a worker thread, so decoding never blocks the event loop.
    """

    def __init__(self, config: Config, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(config)
        self._http = AsyncHttp(config, transport)
        self._connections = AsyncConnectionClient._sharing(config, self._http)
        self._lock: anyio.Lock | None = None

    @classmethod
    def from_env(
        cls,
        environ: Any = None,
        *,
        env_file: str | os.PathLike[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **overrides: Any,
    ) -> AsyncAltGenerator:
        return cls(Config.from_env(environ, env_file=env_file, **overrides), transport=transport)

    async def prepare(self, image: ImageLike, name: str | None = None) -> PreparedImage:
        source = await self._source_async(image, name)
        return await anyio.to_thread.run_sync(prepare_image, source, self._config.image_policy)

    async def generate(
        self,
        image: ImageLike,
        options: GenerateOptions | None = None,
        *,
        name: str | None = None,
        language: str | None = None,
        tone: Tone | str | None = None,
        context: str | None = None,
        variants: int | None = None,
        set_ref: str | None = None,
        set_uuid: str | None = None,
    ) -> GenerationResult:
        """See :meth:`AltGenerator.generate`. Takes FastAPI's ``UploadFile`` as it is."""
        opts = self._options(
            options,
            {
                "language": language,
                "tone": tone,
                "context": context,
                "variants": variants,
                "set_ref": set_ref,
                "set_uuid": set_uuid,
            },
        )
        key = self._config.require_account_key()
        prepared = await self.prepare(image, name)
        await self._check_connection()
        answer = await self._http.send(
            self._generate_request(self._http, opts, prepared),
            service=SERVICE,
            redact=self._redact,
            auth=KeyAuth(key),
            retries=self._config.connect_retries,
        )
        data = _protocol.generator_json(answer, self._redact)
        return _protocol.generation_result(data, opts, prepared, answer.status, self._config.max_alt_length)

    async def generate_many(
        self,
        images: Iterable[Any],
        options: GenerateOptions | None = None,
        *,
        within: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | None = None,
        max_rate_limit_waits: int = 5,
        **overrides: Any,
    ) -> AsyncIterator[BatchItem]:
        """See :meth:`AltGenerator.generate_many`; ``async for item in generator.generate_many(...)``."""
        waits = 0
        for key, entry in self._batch_items(images, within):
            while True:
                try:
                    source = await anyio.to_thread.run_sync(self._read, entry, within)
                    result = await self.generate(source, options, **overrides)
                except RateLimitedError as error:
                    if waits >= max_rate_limit_waits:
                        yield BatchItem(key, error=error)
                        return
                    waits += 1
                    await anyio.sleep(error.retry_after)
                    continue
                except AltGeneratorError as error:
                    yield BatchItem(key, error=error)
                    if isinstance(error, FATAL):
                        return
                    break
                yield BatchItem(key, result=result)
                break

    async def usage(self) -> Usage:
        key = self._config.require_account_key()
        request = self._http.build("GET", self._config.base_url + "/api/user")
        answer = await self._http.send(
            request, service=SERVICE, redact=self._redact, auth=KeyAuth(key), retries=self._config.connect_retries
        )
        return _protocol.usage(_protocol.generator_json(answer, self._redact), answer.status)

    async def verify(self, *, with_usage: bool = False) -> Verification:
        token, secret = self._site()
        return await self._connections.verify(token, secret, with_usage=with_usage)

    async def update_alts(self, image_uuid: str, alts: Iterable[AltLike]) -> None:
        token, _ = self._site()
        await self._connections.update_alts(token, image_uuid, alts)

    async def _source_async(self, image: ImageLike, name: str | None) -> ImageSource:
        # Starlette's UploadFile reads its spooled file in a thread as well.
        return await anyio.to_thread.run_sync(self._source, image, name)

    async def _check_connection(self) -> None:
        if not self._needs_check():
            return
        if self._lock is None:
            self._lock = anyio.Lock()
        async with self._lock:
            if not self._needs_check():
                return
            token, secret = self._site()
            try:
                verification = await self._connections.verify(token, secret)
            except (SiteConnectionError, TransportError, ServerError, UnexpectedResponseError) as error:
                self._remember(None, error)
                return
            self._remember(verification, None)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncAltGenerator:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
