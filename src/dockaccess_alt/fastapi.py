"""FastAPI (and Starlette) helpers: one generator per application, a dependency, an exception handler and
the endpoint dockaccess.org pushes edited alts to.

::

    from contextlib import asynccontextmanager
    from fastapi import FastAPI, UploadFile
    from dockaccess_alt import AltGeneratorError
    from dockaccess_alt.fastapi import AltGeneratorDep, alt_generator_exception_handler, alt_generator_lifespan

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with alt_generator_lifespan(app):
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_exception_handler(AltGeneratorError, alt_generator_exception_handler)

    @app.post("/images")
    async def upload(photo: UploadFile, generator: AltGeneratorDep):
        result = await generator.generate(photo, language="pl")
        return {"alt": result.text}
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio.to_thread
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .client import AsyncAltGenerator
from .config import Config
from .errors import AltGeneratorError, error_response
from .push import MAX_BODY, PushedAlts, PushError, handler_answer, parse_push

STATE = "dockaccess_alt"

PushHandler = Callable[[PushedAlts], Any | Awaitable[Any]]


@asynccontextmanager
async def alt_generator_lifespan(
    app: Any, config: Config | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[AsyncAltGenerator]:
    """Opens one :class:`AsyncAltGenerator` for the application (``app.state.dockaccess_alt``), configured from
    the environment unless ``config`` is given, and closes it on shutdown."""
    generator = AsyncAltGenerator(config or Config.from_env(), transport=transport)
    setattr(app.state, STATE, generator)
    try:
        yield generator
    finally:
        await generator.aclose()
        if getattr(app.state, STATE, None) is generator:
            delattr(app.state, STATE)


def get_alt_generator(request: Request) -> AsyncAltGenerator:
    """The dependency: the application's generator. In tests: ``generator = FakeAltApi("...").async_generator()``,
    then ``app.dependency_overrides[get_alt_generator] = lambda: generator``."""
    generator = getattr(request.app.state, STATE, None)
    if not isinstance(generator, AsyncAltGenerator):
        raise RuntimeError("No alt generator: open it in the application's lifespan with alt_generator_lifespan(app).")
    return generator


AltGeneratorDep = Annotated[AsyncAltGenerator, Depends(get_alt_generator)]


async def alt_generator_exception_handler(request: Request, error: Exception) -> JSONResponse:
    """``app.add_exception_handler(AltGeneratorError, alt_generator_exception_handler)``: a refused image is
    422, a spent limit 429, an unavailable generator 503 - with messages that reveal nothing about the account."""
    assert isinstance(error, AltGeneratorError)
    status, body, headers = error_response(error)
    return JSONResponse(body, status_code=status, headers=headers)


def alt_push_router(handler: PushHandler, *, path: str = "/dockaccess/alts", config: Config | None = None) -> APIRouter:
    """The endpoint dockaccess.org pushes alts edited in the account to (connect with
    ``--callback-url https://your.app/dockaccess/alts``).

    ``handler(pushed)`` finds the image (by ``pushed.image_uuid`` you stored, or ``pushed.image_url``), writes the
    alt(s) and returns how many it wrote - or ``None`` when no local image matches (404). It may be async; a sync
    one runs in a worker thread. The signature, the clock, the size and the site token are checked before it runs.
    """
    router = APIRouter()

    @router.post(path, include_in_schema=False)
    async def receive(request: Request) -> JSONResponse:
        settings = config or _config(request)
        try:
            body = await _body(request)
            pushed = parse_push(
                body, request.headers, secret=settings.connection_secret, site_token=settings.site_token
            )
            if inspect.iscoroutinefunction(handler):
                outcome = await handler(pushed)
            else:
                outcome = await anyio.to_thread.run_sync(handler, pushed)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            status, answer = handler_answer(outcome, pushed)
        except PushError as error:
            status, answer = error.status, error.to_response()
        return JSONResponse(answer, status_code=status, headers={"Cache-Control": "no-store"})

    return router


def _config(request: Request) -> Config:
    generator = getattr(request.app.state, STATE, None)
    return generator.config if isinstance(generator, AsyncAltGenerator) else Config.from_env()


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise PushError(401, "The request could not be verified.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise PushError(401, "The request could not be verified.")
    return bytes(body)
