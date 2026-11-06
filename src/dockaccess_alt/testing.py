"""A stand-in for dockaccess.org in your application's tests - no network, no key, no limit used.

::

    from dockaccess_alt.testing import FakeAltApi

    def test_upload_gets_an_alt(client):
        fake = FakeAltApi("A red cat on a windowsill")
        generator = fake.async_generator()
        app.dependency_overrides[get_alt_generator] = lambda: generator     # FastAPI
        ...
        fake.assert_generated(1)

Images are still verified and rebuilt as in production, so a test feeding a broken file still gets
:class:`InvalidImageError`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from .client import AltGenerator, AsyncAltGenerator
from .config import Config

Responder = Callable[[httpx.Request], httpx.Response]


class FakeAltApi:
    """Answers like ``alt.dockaccess.org`` and ``dockaccess.org/fast-api``.

    ``alts`` are the texts generations answer with, in turn (the last one repeats). Every request is kept in
    :attr:`requests`; :meth:`respond` replaces the answer for a path, e.g. to simulate a spent limit.
    """

    def __init__(self, *alts: str, used: int = 0, limit: int = 10, period: str = "day") -> None:
        self.alts = list(alts) or ["A generated alt text"]
        self.used, self.limit, self.period = used, limit, period
        self.requests: list[httpx.Request] = []
        self._overrides: dict[str, list[Any]] = {}
        self._served = 0
        self._set_uuid = str(uuid.uuid4())
        self.transport = httpx.MockTransport(self._handle)

    # -- configuring answers -------------------------------------------------------------------------------------

    def respond(self, path: str, answer: httpx.Response | Responder, *, times: int | None = None) -> FakeAltApi:
        """Answers requests to ``path`` (``/api/generate``, ``/api/user``, ``/fast-api/integrations/verify``...)
        with ``answer`` - always, or the next ``times`` requests."""
        self._overrides[path] = [answer, times]
        return self

    def reset(self) -> FakeAltApi:
        """Back to plain successful answers."""
        self._overrides.clear()
        return self

    def limit_reached(self, period: str = "day") -> FakeAltApi:
        window = "monthly" if period == "month" else "daily"
        detail = f"You've reached your limit. Your {window} limit is {self.limit} requests."
        headers = {"X-Alt-Reason": "limit_reached", "X-Alt-Daily-Limit": str(self.limit), "X-Alt-Limit-Period": period}
        return self.respond("/api/generate", httpx.Response(429, json={"detail": detail}, headers=headers))

    def rate_limited(self, retry_after: int = 1, *, times: int = 1) -> FakeAltApi:
        headers = {"X-Alt-Reason": "rate_limited", "Retry-After": str(retry_after)}
        return self.respond(
            "/api/generate", httpx.Response(429, json={"detail": "Rate limit exceeded"}, headers=headers), times=times
        )

    def invalid_key(self) -> FakeAltApi:
        return self.respond("/api/generate", httpx.Response(403, json={"detail": "Invalid key"}))

    # -- generators wired to it ----------------------------------------------------------------------------------

    def config(self, **overrides: Any) -> Config:
        settings: dict[str, Any] = {"account_key": "fake-account-key", "check_connection": False}
        settings.update(overrides)
        return Config(**settings)

    def generator(self, **overrides: Any) -> AltGenerator:
        return AltGenerator(self.config(**overrides), transport=self.transport)

    def async_generator(self, **overrides: Any) -> AsyncAltGenerator:
        return AsyncAltGenerator(self.config(**overrides), transport=self.transport)

    # -- assertions ------------------------------------------------------------------------------------------------

    def calls(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path.endswith(path)]

    @property
    def generated(self) -> int:
        return len(self.calls("/api/generate"))

    def assert_generated(self, count: int) -> None:
        assert self.generated == count, f"Expected {count} generations, got {self.generated}."

    # -- the fake service ------------------------------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for suffix, entry in list(self._overrides.items()):
            if path.endswith(suffix):
                answer, times = entry
                if times is not None:
                    entry[1] = times - 1
                    if entry[1] <= 0:
                        del self._overrides[suffix]
                if callable(answer):
                    response: httpx.Response = answer(request)
                    return response
                return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)
        if path.endswith("/api/generate"):
            return self._generate(request)
        if path.endswith("/api/user"):
            return httpx.Response(
                200, json={"requests": {"used": self.used, "limit": self.limit, "period": self.period}}
            )
        if path.endswith("/integrations/verify"):
            return httpx.Response(
                200,
                json={
                    "alt_enabled": True,
                    "token": {"alt_enabled": True, "alt_paused": False},
                    "integration": {"id": 1, "system": "python"},
                },
            )
        if path.endswith("/alts/update"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"detail": "Not found"})

    def _generate(self, request: httpx.Request) -> httpx.Response:
        if self.used >= self.limit:
            return httpx.Response(
                429,
                json={"detail": "You've reached your limit."},
                headers={"X-Alt-Reason": "limit_reached", "X-Alt-Limit-Period": self.period},
            )
        language = request.url.params.get("lang", "en")
        variants = int(request.url.params.get("variants", "1"))
        index = min(self._served, len(self.alts) - 1)
        self._served += 1
        texts = [self.alts[index]] + [f"{self.alts[index]} ({n})" for n in range(2, variants + 1)]
        self.used += 1
        body = {
            "data": {
                "set_uuid": self._set_uuid,
                "set_ref": request.url.params.get("set_ref"),
                "image": {"uuid": str(uuid.uuid4()), "alts": [{"alt": text, "locale": language} for text in texts]},
            }
        }
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
