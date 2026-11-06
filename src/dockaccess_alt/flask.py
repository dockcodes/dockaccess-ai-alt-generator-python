"""Flask helpers: one generator per application, configured from ``app.config`` and the environment, and the
endpoint dockaccess.org pushes edited alts to.

::

    from flask import Flask, request
    from dockaccess_alt.flask import DockAccessAlt, get_alt_generator

    app = Flask(__name__)
    DockAccessAlt(app)                       # DOCKACCESS_ALT_* from app.config, else the environment

    @app.post("/images")
    def upload():
        result = get_alt_generator().generate(request.files["photo"], language="pl")
        return {"alt": result.text}
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx
from flask import Blueprint, Flask, current_app, jsonify, request
from flask.typing import ResponseReturnValue

from .client import AltGenerator
from .config import Config
from .errors import AltGeneratorError, error_response
from .push import MAX_BODY, PushedAlts, PushError, handler_answer, parse_push

EXTENSION = "dockaccess_alt"


def config_from_flask(app: Flask) -> Config:
    """``DOCKACCESS_ALT_*`` keys of ``app.config`` over the environment variables of the same names."""
    environ = dict(os.environ)
    environ.update(
        {
            key: str(value)
            for key, value in app.config.items()
            if key.startswith("DOCKACCESS_ALT_") and value is not None
        }
    )
    return Config.from_env(environ)


class DockAccessAlt:
    """The Flask extension. Supports the application factory pattern (``ext.init_app(app)``)."""

    def __init__(
        self, app: Flask | None = None, *, config: Config | None = None, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        generator = AltGenerator(self._config or config_from_flask(app), transport=self._transport)
        app.extensions[EXTENSION] = generator

    @property
    def generator(self) -> AltGenerator:
        return get_alt_generator()


def get_alt_generator() -> AltGenerator:
    """The current application's generator."""
    generator = current_app.extensions.get(EXTENSION)
    if not isinstance(generator, AltGenerator):
        raise RuntimeError("No alt generator: initialise DockAccessAlt(app) first.")
    return generator


def register_error_handler(app: Flask) -> None:
    """Answers :class:`AltGeneratorError` raised in a view with JSON: 422 for a refused image, 429 for a spent
    limit, 503/502 when the generator is not available."""

    def handle(error: AltGeneratorError) -> ResponseReturnValue:
        status, body, headers = error_response(error)
        return jsonify(body), status, headers

    app.register_error_handler(AltGeneratorError, handle)


def alt_push_blueprint(
    handler: Callable[[PushedAlts], Any], *, url: str = "/dockaccess/alts", name: str = "dockaccess_alt_push"
) -> Blueprint:
    """The endpoint dockaccess.org pushes alts edited in the account to. ``handler(pushed)`` writes them and
    returns how many it wrote, or ``None`` when no local image matches. With Flask-WTF's CSRFProtect, exempt it:
    ``csrf.exempt(blueprint)``."""
    blueprint = Blueprint(name, __name__)

    @blueprint.post(url)
    def receive() -> ResponseReturnValue:
        generator = current_app.extensions.get(EXTENSION)
        settings = generator.config if isinstance(generator, AltGenerator) else config_from_flask(current_app)
        try:
            if (request.content_length or 0) > MAX_BODY:
                raise PushError(401, "The request could not be verified.")
            body = request.stream.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise PushError(401, "The request could not be verified.")
            pushed = parse_push(
                body, dict(request.headers), secret=settings.connection_secret, site_token=settings.site_token
            )
            status, answer = handler_answer(handler(pushed), pushed)
        except PushError as error:
            status, answer = error.status, error.to_response()
        return jsonify(answer), status, {"Cache-Control": "no-store"}

    return blueprint
