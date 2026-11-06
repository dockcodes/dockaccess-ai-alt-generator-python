from __future__ import annotations

import json
import logging
import pickle

import httpx
import pytest

from dockaccess_alt import (
    AltGenerator,
    AuthenticationError,
    Config,
    ConfigurationError,
    GenerateOptions,
    GeneratorUnavailableError,
    ImageRejectedError,
    InvalidImageError,
    LimitReachedError,
    RateLimitedError,
    ServerError,
    TransportError,
    UnexpectedResponseError,
)
from dockaccess_alt.errors import ApiError, error_response
from dockaccess_alt.testing import FakeAltApi

from .conftest import jpeg

KEY = "Acc0untKey-ShouldNeverLeak-123"
SECRET = "c0nnection-secret-0123456789abcdef"
IMAGE_UUID = "11111111-2222-4333-8444-555555555555"
SET_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def answer(status: int, body: object = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


def generator_answering(
    *responses: httpx.Response | Exception, **config: object
) -> tuple[AltGenerator, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    settings: dict[str, object] = {"account_key": KEY, "check_connection": False}
    settings.update(config)
    return AltGenerator(Config(**settings), transport=httpx.MockTransport(handle)), seen  # type: ignore[arg-type]


def ok(*alts: dict[str, str]) -> httpx.Response:
    return answer(
        200,
        {"data": {"set_uuid": SET_UUID, "set_ref": "site", "image": {"uuid": IMAGE_UUID.upper(), "alts": list(alts)}}},
    )


def test_generate_sends_the_rebuilt_image_and_the_options_and_returns_clean_alts() -> None:
    generator, seen = generator_answering(
        ok(
            {"alt": "<b>Oak</b> table‮​ in a  bright room", "locale": "pl"},
            {"alt": "", "locale": "pl"},
            {"alt": "Second", "locale": "zz"},
        )
    )
    result = generator.generate(
        jpeg(), language="pl_PL", tone="descriptive", context="Shop <script>x</script>", variants=2, name="Stół.JPG"
    )

    assert [a.text for a in result.alts] == ["Oak table in a bright room", "Second"]
    assert [a.locale for a in result.alts] == ["pl", "pl"]
    assert (result.image_uuid, result.set_uuid, result.set_ref, result.text) == (
        IMAGE_UUID,
        SET_UUID,
        "site",
        "Oak table in a bright room",
    )
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/generate"
    assert dict(request.url.params) == {
        "lang": "pl",
        "tone": "descriptive",
        "variants": "2",
        "context": "Shop x",
        "key": KEY,
    }
    body = request.read()
    assert b'filename="St.jpg"' in body and b"Content-Type: image/jpeg" in body
    assert request.headers["user-agent"].startswith("dockaccess-alt-generator-python/")


def test_defaults_come_from_the_config_and_the_site_token_names_the_set() -> None:
    generator, seen = generator_answering(
        ok({"alt": "A", "locale": "de"}), defaults=GenerateOptions(language="de", tone="formal"), site_token="SiteTok3n"
    )
    generator.generate(jpeg())
    assert dict(seen[0].url.params) | {"key": "-"} == {
        "lang": "de",
        "tone": "formal",
        "variants": "1",
        "set_ref": "SiteTok3n",
        "key": "-",
    }


@pytest.mark.parametrize(
    ("response", "error", "check"),
    [
        (answer(403, {"detail": "Invalid key"}), AuthenticationError, None),
        (answer(401, {"detail": "nope"}), AuthenticationError, None),
        (
            answer(
                429,
                {"detail": "You've reached your limit."},
                {"X-Alt-Reason": "limit_reached", "X-Alt-Daily-Limit": "2000", "X-Alt-Limit-Period": "month"},
            ),
            LimitReachedError,
            lambda e: (e.limit, e.period) == (2000, "month"),
        ),
        (answer(429, {"detail": "slow down"}, {"Retry-After": "17"}), RateLimitedError, lambda e: e.retry_after == 17),
        (
            answer(429, {"detail": "slow down"}, {"Retry-After": "soon"}),
            RateLimitedError,
            lambda e: e.retry_after == 60,
        ),
        (
            answer(400, {"detail": [{"msg": "Unsupported image", "loc": ["file"]}]}),
            ImageRejectedError,
            lambda e: e.api_message == "Unsupported image",
        ),
        (answer(413, {"detail": "Too large"}), ImageRejectedError, None),
        (answer(415, {"detail": "Bad type"}), ImageRejectedError, None),
        (answer(503, {"detail": "down"}), ServerError, None),
        (answer(404, {"detail": "?"}), ApiError, None),
        (httpx.Response(302, headers={"Location": "https://evil.example/steal"}), UnexpectedResponseError, None),
        (httpx.Response(200, content=b"<html>"), UnexpectedResponseError, None),
        (
            answer(200, {"data": {"set_uuid": SET_UUID, "image": {"uuid": "not-a-uuid", "alts": [{"alt": "a"}]}}}),
            UnexpectedResponseError,
            None,
        ),
        (ok({"alt": "   "}), UnexpectedResponseError, None),
    ],
)
def test_answers_map_to_errors(response: httpx.Response, error: type[Exception], check: object) -> None:
    generator, seen = generator_answering(response)
    with pytest.raises(error) as caught:
        generator.generate(jpeg())
    assert KEY not in str(caught.value)
    if check is not None:
        assert check(caught.value)  # type: ignore[operator]
    assert len(seen) == 1, "a refusal is never repeated"


def test_the_key_is_redacted_from_messages_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    generator, _ = generator_answering(
        answer(400, {"detail": f"bad key {KEY} at https://alt.dockaccess.org/api/generate?key={KEY}"})
    )
    with pytest.raises(ImageRejectedError) as caught:
        generator.generate(jpeg())
    assert KEY not in str(caught.value) and "[redacted]" in str(caught.value)
    assert KEY not in caplog.text
    assert "key=%5Bredacted%5D" in caplog.text or "key=[redacted]" in caplog.text
    assert KEY not in repr(generator.config) and KEY not in repr(generator)


def test_only_failures_before_sending_are_retried() -> None:
    refused = httpx.ConnectError("Connection refused")
    generator, seen = generator_answering(refused, ok({"alt": "A", "locale": "en"}))
    assert generator.generate(jpeg()).text == "A"
    assert len(seen) == 2

    generator, seen = generator_answering(httpx.ReadTimeout("read timed out"), connect_retries=3)
    with pytest.raises(TransportError) as caught:
        generator.generate(jpeg())
    assert not caught.value.retryable and len(seen) == 1

    generator, seen = generator_answering(refused, connect_retries=0)
    with pytest.raises(TransportError) as caught:
        generator.generate(jpeg())
    assert caught.value.retryable and len(seen) == 1


def test_an_oversized_answer_is_refused() -> None:
    generator, _ = generator_answering(httpx.Response(200, content=b"{" + b" " * (2 * 1024 * 1024)))
    with pytest.raises(TransportError, match="more data"):
        generator.generate(jpeg())


def test_a_bad_image_never_reaches_the_network(fake: FakeAltApi) -> None:
    generator = fake.generator()
    with pytest.raises(InvalidImageError):
        generator.generate(b"<?php echo 1; ?>")
    assert fake.requests == []


def test_usage_reads_the_window() -> None:
    generator, seen = generator_answering(
        answer(200, {"requests": {"used": 15, "limit": 2000, "period": "month", "resets_at": "2026-12-01T00:00:00Z"}})
    )
    usage = generator.usage()
    assert (usage.used, usage.limit, usage.remaining, usage.is_monthly, usage.is_exhausted) == (
        15,
        2000,
        1985,
        True,
        False,
    )
    assert usage.resets_at is not None and usage.resets_at.isoformat() == "2026-12-01T00:00:00+00:00"
    assert seen[0].url.path == "/api/user"

    generator, _ = generator_answering(answer(200, {"requests": {"used": 3}}))
    with pytest.raises(UnexpectedResponseError):
        generator.usage()


def test_without_a_key_nothing_is_sent() -> None:
    generator = AltGenerator(Config())
    with pytest.raises(ConfigurationError, match="dockaccess-alt connect"):
        generator.generate(jpeg())


def test_errors_survive_pickling_for_task_queues() -> None:
    for error in (
        LimitReachedError(429, "spent", 10, "day"),
        RateLimitedError(429, "slow", 30),
        InvalidImageError("malformed", "broken"),
        TransportError("down", True),
        GeneratorUnavailableError("alt_paused"),
    ):
        copy = pickle.loads(pickle.dumps(error))  # noqa: S301 - our own bytes
        assert type(copy) is type(error) and str(copy) == str(error) and copy.__dict__ == error.__dict__


def test_errors_map_to_answers_for_web_applications() -> None:
    assert error_response(InvalidImageError("malformed", "Broken."))[0] == 422
    assert error_response(LimitReachedError(429, "x", 10, "month"))[:2] == (
        429,
        {"detail": "The alt text limit of this month is used up.", "reason": "limit_reached"},
    )
    assert error_response(RateLimitedError(429, "x", 12))[2] == {"Retry-After": "12"}
    assert error_response(AuthenticationError(403, "Invalid key"))[0] == 503
    assert error_response(TransportError("x"))[0] == 502


# -- the connection check ------------------------------------------------------------------------------------------


def guarded(*verify: httpx.Response | Exception, ttl: float = 300) -> tuple[AltGenerator, FakeAltApi]:
    fake = FakeAltApi("A")
    queue = list(verify)

    def handle(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    fake.respond("/integrations/verify", handle)
    return fake.generator(site_token="SiteTok3n", connection_secret=SECRET, check_connection=True, verify_ttl=ttl), fake


ACTIVE = answer(
    200, {"alt_enabled": True, "token": {"alt_enabled": True}, "integration": {"id": 7, "system": "python"}}
)


def test_the_connection_is_checked_once_per_ttl() -> None:
    generator, fake = guarded(ACTIVE)
    generator.generate(jpeg())
    generator.generate(jpeg())
    assert len(fake.calls("/integrations/verify")) == 1
    assert fake.generated == 2
    sent = fake.calls("/integrations/verify")[0]
    assert json.loads(sent.read()) == {"token": "SiteTok3n", "secret": SECRET, "usage": False}


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (answer(200, {"alt_enabled": False, "token": {"alt_paused": True}}), "alt_paused"),
        (answer(200, {"alt_enabled": False, "token": {"alt_paused": False}}), "alt_disabled"),
        (answer(403, {"detail": "Disconnected.", "reason": "disconnected"}), "disconnected"),
        (answer(401, {"detail": "Unknown.", "reason": "invalid_credentials"}), "disconnected"),
    ],
)
def test_a_site_the_account_switched_off_sends_nothing(response: httpx.Response, reason: str) -> None:
    generator, fake = guarded(response)
    for _ in range(2):
        with pytest.raises(GeneratorUnavailableError) as caught:
            generator.generate(jpeg())
        assert caught.value.reason == reason
    assert fake.generated == 0
    assert len(fake.calls("/integrations/verify")) == 1, "the refusal is remembered for the ttl"


def test_an_unreachable_dockaccess_does_not_stop_generation(caplog: pytest.LogCaptureFixture) -> None:
    generator, _ = guarded(httpx.ConnectError("down"), ACTIVE)
    with caplog.at_level(logging.WARNING, "dockaccess_alt"):
        assert generator.generate(jpeg()).text == "A"
    assert "generating anyway" in caplog.text and SECRET not in caplog.text


def test_verify_and_update_alts_need_the_site() -> None:
    generator = FakeAltApi().generator()
    with pytest.raises(ConfigurationError):
        generator.verify()
    connected = FakeAltApi().generator(site_token="SiteTok3n", connection_secret=SECRET)
    assert connected.verify(with_usage=True).is_active


def test_update_alts_sends_the_edit_back() -> None:
    fake = FakeAltApi()
    generator = fake.generator(site_token="SiteTok3n", connection_secret=SECRET)
    generator.update_alts(IMAGE_UUID.upper(), [("Oak table <b>x</b>", "en"), ("Stół", "pl")])
    body = json.loads(fake.calls("/alts/update")[0].read())
    assert body == {
        "image_uuid": IMAGE_UUID,
        "key": "fake-account-key",
        "token": "SiteTok3n",
        "alts": [{"alt": "Oak table x", "locale": "en"}, {"alt": "Stół", "locale": "pl"}],
    }
    assert fake.calls("/alts/update")[0].method == "PUT"
    with pytest.raises(ConfigurationError):
        generator.update_alts("nope", [("a", "en")])
    with pytest.raises(ConfigurationError):
        generator.update_alts(IMAGE_UUID, [("  ", "en")])
