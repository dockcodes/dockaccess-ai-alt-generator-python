from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest

from dockaccess_alt import (
    AsyncAltGenerator,
    Config,
    GeneratorUnavailableError,
    InvalidImageError,
    LimitReachedError,
    RateLimitedError,
)
from dockaccess_alt.testing import FakeAltApi

from .conftest import jpeg, png


@pytest.mark.anyio
async def test_async_generation_with_an_upload_object() -> None:
    class Upload:
        filename = "chair.png"

        def __init__(self) -> None:
            self.file = io.BytesIO(png(alpha=0))

        async def read(self) -> bytes:  # pragma: no cover - never awaited
            raise AssertionError

    fake = FakeAltApi("A green chair")
    async with fake.async_generator() as generator:
        result = await generator.generate(Upload(), language="de-AT")
        usage = await generator.usage()
    assert result.text == "A green chair" and result.language == "de"
    assert result.image.name == "chair.webp"
    assert usage.used == 1
    assert fake.calls("/api/generate")[0].url.params["lang"] == "de"


@pytest.mark.anyio
async def test_async_connection_check_and_refusal() -> None:
    fake = FakeAltApi("A")
    fake.respond(
        "/integrations/verify", httpx.Response(200, json={"alt_enabled": False, "token": {"alt_paused": True}})
    )
    generator = fake.async_generator(
        site_token="SiteTok3n", connection_secret="s3cret-value-123", check_connection=True
    )
    with pytest.raises(GeneratorUnavailableError):
        await generator.generate(jpeg())
    assert fake.generated == 0
    await generator.aclose()


def test_a_batch_reports_each_image_and_skips_refused_ones(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(jpeg())
    (tmp_path / "b.jpg").write_bytes(b"not an image")
    fake = FakeAltApi("First", "Second")
    generator = fake.generator()
    items = list(
        generator.generate_many(
            [("id-1", jpeg()), ("id-2", b"<svg/>"), str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")], within=tmp_path
        )
    )

    assert [item.key for item in items] == ["id-1", "id-2", str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")]
    assert [item.ok for item in items] == [True, False, True, False]
    assert items[0].result is not None and items[0].result.text == "First"
    assert isinstance(items[1].error, InvalidImageError) and not items[1].stopped
    assert fake.generated == 2


def test_a_batch_waits_out_the_per_minute_cap_and_stops_at_the_limit() -> None:
    fake = FakeAltApi("A", limit=2)
    fake.rate_limited(retry_after=7, times=1)
    waited: list[float] = []
    items = list(fake.generator().generate_many([jpeg(), jpeg(), jpeg(), jpeg()], sleep=waited.append))

    assert waited == [7]
    assert [item.ok for item in items] == [True, True, False]
    assert isinstance(items[2].error, LimitReachedError) and items[2].stopped


def test_a_batch_gives_up_after_too_many_waits() -> None:
    fake = FakeAltApi("A")
    fake.rate_limited(retry_after=1, times=10)
    items = list(fake.generator().generate_many([jpeg(), jpeg()], max_rate_limit_waits=2, sleep=lambda _: None))
    assert len(items) == 1 and isinstance(items[0].error, RateLimitedError)


@pytest.mark.anyio
async def test_the_async_batch_behaves_the_same() -> None:
    fake = FakeAltApi("A", limit=1)
    generator = fake.async_generator()
    items = [item async for item in generator.generate_many([("x", jpeg()), ("y", jpeg()), ("z", jpeg())])]
    assert [(item.key, item.ok, item.stopped) for item in items] == [("x", True, False), ("y", False, True)]


def test_the_fake_is_wired_to_a_config_without_network() -> None:
    fake = FakeAltApi("A")
    assert isinstance(fake.config(), Config)
    assert fake.config().check_connection is False
    fake.invalid_key()
    with pytest.raises(Exception, match="Invalid key"):
        fake.generator().generate(jpeg())
    fake.reset()
    assert fake.generator().generate(jpeg()).text == "A"
    fake.assert_generated(2)
    assert isinstance(fake.async_generator(), AsyncAltGenerator)
