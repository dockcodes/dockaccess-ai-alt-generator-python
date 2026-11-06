from __future__ import annotations

import json
import time

import httpx
import pytest

from dockaccess_alt import (
    ConfigurationError,
    ConnectionClient,
    ConnectRequest,
    PushError,
    ServerError,
    SiteConnectionError,
    TransportError,
    parse_push,
)
from dockaccess_alt.push import handler_answer, sign_push

PASSWORD = "Owner-Password-1"
SECRET = "push-secret-0123456789abcdef0123456789abcdef"
CONNECTED = {
    "external_token": "external-key-0123456789abcdef",
    "integration": {
        "id": 12,
        "secret": SECRET,
        "system": "python",
        "home_url": "https://shop.example.com",
        "domain_verified": True,
    },
    "token": {"token": "SiteTok3n", "alt_enabled": True},
    "user": {"display_name": "Anna <b>Nowak</b>"},
}


def client(*responses: httpx.Response | Exception) -> tuple[ConnectionClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return ConnectionClient(transport=httpx.MockTransport(handle)), seen


def request(**changes: object) -> ConnectRequest:
    values: dict[str, object] = {
        "site_token": " SiteTok3n ",
        "login": "owner@example.com",
        "password": PASSWORD,
        "home_url": "https://shop.example.com",
    }
    values.update(changes)
    return ConnectRequest(**values)  # type: ignore[arg-type]


def test_connect_hands_back_the_keys_as_secrets() -> None:
    connections, seen = client(httpx.Response(200, json=CONNECTED))
    connection = connections.connect(request(callback_url="https://shop.example.com/dockaccess/alts"))

    assert connection.account_key.reveal() == "external-key-0123456789abcdef"
    assert connection.secret.reveal() == SECRET
    assert (connection.site_token, connection.integration_id, connection.account_name) == (
        "SiteTok3n",
        12,
        "Anna Nowak",
    )
    assert connection.alt_enabled and connection.domain_verified
    assert SECRET not in repr(connection) and "external-key" not in repr(connection)
    assert connection.environment()["DOCKACCESS_ALT_SECRET"] == SECRET

    sent = json.loads(seen[0].read())
    assert seen[0].url == "https://dockaccess.org/fast-api/integrations/connect"
    assert sent["token"] == "SiteTok3n" and sent["system"] == "python" and sent["password"] == PASSWORD
    assert sent["callback_url"] == "https://shop.example.com/dockaccess/alts" and "overwrite" not in sent
    assert sent["module_version"] and sent["system_version"]
    assert PASSWORD not in repr(request())


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"detail": "Invalid login or password.", "reason": "invalid_credentials"}, "invalid_credentials"),
        (409, {"detail": "Already connected.", "reason": "already_connected"}, "already_connected"),
        (403, {"detail": "Nope.", "reason": "domain_not_allowed"}, "domain_not_allowed"),
        (429, {"detail": "Slow down."}, "too_many_attempts"),
        (422, {"detail": {"home_url": "Invalid home url."}}, "invalid_request"),
        (403, {"detail": "Something else.", "reason": "made_up"}, "refused"),
    ],
)
def test_refusals_carry_a_reason_and_are_never_repeated(status: int, body: dict[str, object], reason: str) -> None:
    connections, seen = client(httpx.Response(status, json=body))
    with pytest.raises(SiteConnectionError) as caught:
        connections.connect(request())
    assert caught.value.reason == reason and len(seen) == 1
    assert str(caught.value).startswith(f"dockaccess.org refused the request (HTTP {status})")
    assert PASSWORD not in str(caught.value)


def test_a_failed_connect_is_not_retried_even_before_sending() -> None:
    connections, seen = client(httpx.ConnectError("refused"))
    with pytest.raises(TransportError, match=r"dockaccess\.org could not be reached"):
        connections.connect(request())
    assert len(seen) == 1
    connections, _ = client(httpx.Response(502, json={"detail": "down"}))
    with pytest.raises(ServerError, match=r"^dockaccess\.org refused"):
        connections.connect(request())


def test_verify_reads_the_state_and_the_usage() -> None:
    connections, seen = client(
        httpx.Response(200, json={
            "alt_enabled": True, "token": {"alt_enabled": True}, "integration": {"id": 12, "system": "python"},
            "alt": {"requests": {"used": 3, "limit": 10, "period": "day"}, "websites": {"used": 1, "limit": 1}},
        })
    )  # fmt: skip
    verification = connections.verify("SiteTok3n", SECRET, with_usage=True)
    assert verification.is_active and verification.usage is not None and verification.usage.remaining == 7
    assert (verification.websites_used, verification.websites_limit, verification.system) == (1, 1, "python")
    assert json.loads(seen[0].read()) == {"token": "SiteTok3n", "secret": SECRET, "usage": True}

    connections, _ = client(httpx.Response(410, json={"detail": f"Gone for {SECRET}", "reason": "disconnected"}))
    with pytest.raises(SiteConnectionError) as caught:
        connections.verify("SiteTok3n", SECRET)
    assert caught.value.is_connection_gone and SECRET not in str(caught.value)


def test_connect_requests_are_validated() -> None:
    for changes in (
        {"site_token": ""},
        {"password": ""},
        {"home_url": "shop.example.com"},
        {"system": "Py thon!"},
        {"callback_url": "ftp://x"},
    ):
        with pytest.raises(ConfigurationError):
            request(**changes)
    assert request(system=" Django ").system == "django"
    assert request(overwrite=True).to_payload()["overwrite"] is True


# -- pushes -----------------------------------------------------------------------------------------------------------

PUSH = {
    "token": "SiteTok3n",
    "image_uuid": "11111111-2222-4333-8444-555555555555",
    "image_url": "https://shop.example.com/media/cat.jpg",
    "alts": [{"alt": "A ginger cat <b>basking</b>", "locale": "en"}, {"alt": "Rudy kot", "locale": "pl_PL"}],
}


def signed(payload: object = PUSH, secret: str = SECRET, at: int | None = None) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, sign_push(body, secret, at)


def test_a_signed_push_is_read() -> None:
    body, headers = signed()
    pushed = parse_push(body, headers, secret=SECRET, site_token="SiteTok3n")
    assert pushed.image_uuid == PUSH["image_uuid"] and pushed.image_url == PUSH["image_url"]
    assert [(a.text, a.locale) for a in pushed.alts] == [("A ginger cat basking", "en"), ("Rudy kot", "pl")]
    assert pushed.for_language("pl_PL").text == "Rudy kot" and pushed.for_language("de").text == "A ginger cat basking"
    assert handler_answer(2, pushed) == (200, {"success": True, "updated": 2, "image_uuid": PUSH["image_uuid"]})
    assert handler_answer(None, pushed)[0] == 404 and handler_answer(0, pushed)[0] == 400


def test_every_signature_problem_answers_the_same_401() -> None:
    body, headers = signed()
    now = int(headers["X-Dockaccess-Timestamp"])
    cases = [
        (body, {**headers, "X-Dockaccess-Signature": "sha256=" + "0" * 64}),
        (body + b" ", headers),
        (body, {k: v for k, v in headers.items() if k != "X-Dockaccess-Signature"}),
        (body, {**headers, "X-Dockaccess-Signature": headers["X-Dockaccess-Signature"].replace("sha256=", "md5=")}),
        signed(at=now - 301),
        signed(at=now + 301),
        signed(secret="another-secret-value"),
        (b"x" * (64 * 1024 + 1), headers),
    ]
    messages = set()
    for raw, sent in cases:
        with pytest.raises(PushError) as caught:
            parse_push(raw, sent, secret=SECRET, site_token="SiteTok3n", now=now)
        assert caught.value.status == 401
        messages.add(str(caught.value))
    assert len(messages) == 1


def test_a_push_for_another_site_or_without_alts_is_refused() -> None:
    body, headers = signed({**PUSH, "token": "Other"})
    with pytest.raises(PushError) as caught:
        parse_push(body, headers, secret=SECRET, site_token="SiteTok3n")
    assert caught.value.status == 403

    for payload in (
        {**PUSH, "alts": []},
        {**PUSH, "alts": [{"alt": " ", "locale": "en"}]},
        {"token": "SiteTok3n", "alts": PUSH["alts"]},
    ):
        body, headers = signed(payload)
        with pytest.raises(PushError) as caught:
            parse_push(body, headers, secret=SECRET, site_token="SiteTok3n")
        assert caught.value.status == 400

    body, headers = signed()
    with pytest.raises(PushError) as caught:
        parse_push(body, headers, secret=None, site_token=None)
    assert caught.value.status == 503


def test_headers_are_matched_case_insensitively() -> None:
    body, headers = signed()
    lowered = {name.lower(): value for name, value in headers.items()}
    assert parse_push(body, lowered, secret=SECRET, site_token="SiteTok3n", now=time.time()).alts
