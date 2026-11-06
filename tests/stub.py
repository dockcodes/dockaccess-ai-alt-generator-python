"""A local HTTP stand-in for alt.dockaccess.org and dockaccess.org/fast-api, for tests that go through the real
httpx transport (and the CLI). Answers follow the real contracts; every call is recorded."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

KEY = "external-key-0123456789abcdef"
SECRET = "push-secret-0123456789abcdef0123456789abcdef"
TOKEN = "SiteTok3n"
LOGIN = "owner@example.com"
PASSWORD = "Owner-Password-1"


class Stub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode: dict[str, Any] = {}
        self.used = 0
        self.limit = 10
        self.connected = False
        self.lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"stub": self})
        self.server = _QuietServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def env(self) -> dict[str, str]:
        return {
            "DOCKACCESS_ALT_URL": self.url,
            "DOCKACCESS_ALT_CONNECTION_URL": self.url + "/fast-api",
            "DOCKACCESS_ALT_INSECURE_LOOPBACK": "1",
        }

    def called(self, path: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["path"] == path]

    def reset(self) -> None:
        self.calls.clear()
        self.mode.clear()
        self.used, self.limit, self.connected = 0, 10, False

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that hung up on purpose (the oversized answer) is not an error here


class _Handler(BaseHTTPRequestHandler):
    stub: Stub

    def log_message(self, *_: Any) -> None:
        pass

    def reply(self, status: int, body: Any, headers: dict[str, str] | None = None, raw: bytes | None = None) -> None:
        data = raw if raw is not None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _record(self) -> tuple[str, dict[str, str], bytes]:
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        with self.stub.lock:
            self.stub.calls.append(
                {"method": self.command, "path": url.path, "query": query, "body": body, "headers": dict(self.headers)}
            )
        return url.path, query, body

    def do_GET(self) -> None:
        path, query, _ = self._record()
        if path == "/api/user":
            if query.get("key") != KEY:
                return self.reply(403, {"detail": "Invalid key"})
            return self.reply(
                200,
                {
                    "requests": {
                        "used": self.stub.used,
                        "limit": self.stub.limit,
                        "period": "day",
                        "resets_at": "2026-11-07T00:00:00Z",
                    }
                },
            )
        return self.reply(404, {"detail": "Not Found"})

    def do_PUT(self) -> None:
        path, _, _ = self._record()
        if path == "/fast-api/alts/update":
            return self.reply(200, {"success": True})
        return self.reply(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        path, query, body = self._record()
        mode = self.stub.mode
        if path == "/api/generate":
            if mode.get("redirect"):
                return self.reply(302, {}, {"Location": "https://evil.example/collect"})
            if mode.get("huge"):
                return self.reply(200, None, raw=b'{"x": "' + b"a" * (2 * 1024 * 1024) + b'"}')
            if query.get("key") != KEY:
                return self.reply(403, {"detail": "Invalid key"})
            if self.stub.used >= self.stub.limit:
                return self.reply(
                    429,
                    {"detail": "You've reached your limit."},
                    {
                        "X-Alt-Reason": "limit_reached",
                        "X-Alt-Daily-Limit": str(self.stub.limit),
                        "X-Alt-Limit-Period": "day",
                    },
                )
            self.stub.used += 1
            texts = mode.get("texts") or []
            text = (
                texts[(self.stub.used - 1) % len(texts)]
                if texts
                else f"Generated alt {self.stub.used} ({query.get('lang')})"
            )
            return self.reply(
                200,
                {
                    "data": {
                        "set_uuid": str(uuid.uuid4()),
                        "set_ref": query.get("set_ref"),
                        "image": {"uuid": str(uuid.uuid4()), "alts": [{"alt": text, "locale": query.get("lang")}]},
                    }
                },
            )
        if path == "/fast-api/integrations/connect":
            data = json.loads(body or b"{}")
            if data.get("login") != LOGIN or data.get("password") != PASSWORD:
                return self.reply(401, {"detail": "Invalid login or password.", "reason": "invalid_credentials"})
            if self.stub.connected and not data.get("overwrite"):
                return self.reply(409, {"detail": "This address is connected already.", "reason": "already_connected"})
            self.stub.connected = True
            return self.reply(200, {
                "external_token": KEY,
                "integration": {"id": 5, "secret": SECRET, "system": data.get("system"), "home_url": data.get("home_url"), "domain_verified": "localhost" not in data.get("home_url", "")},
                "token": {"token": data.get("token"), "alt_enabled": True},
                "user": {"display_name": "Owner"},
            })  # fmt: skip
        if path == "/fast-api/integrations/verify":
            data = json.loads(body or b"{}")
            if not self.stub.connected or data.get("secret") != SECRET:
                return self.reply(401, {"detail": "Unknown connection.", "reason": "invalid_credentials"})
            answer: dict[str, Any] = {
                "alt_enabled": not mode.get("paused"),
                "token": {"alt_paused": bool(mode.get("paused"))},
                "integration": {"id": 5, "system": "python"},
            }
            if data.get("usage"):
                answer["alt"] = {
                    "requests": {"used": self.stub.used, "limit": self.stub.limit, "period": "day"},
                    "websites": {"used": 1, "limit": 1},
                }
            return self.reply(200, answer)
        if path == "/fast-api/integrations/disconnect":
            if not self.stub.connected:
                return self.reply(404, {"detail": "Not connected.", "reason": "not_connected"})
            self.stub.connected = False
            return self.reply(200, {"success": True})
        return self.reply(404, {"detail": "Not Found"})
