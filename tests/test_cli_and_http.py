"""The CLI end to end, and the real httpx transport against a local server."""

from __future__ import annotations

import json
import os
import socket
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from dockaccess_alt import AltGenerator, Config, TransportError, UnexpectedResponseError
from dockaccess_alt.cli import main
from dockaccess_alt.envfile import read_env_file

from . import stub as stubs
from .conftest import jpeg, png


@pytest.fixture(scope="module")
def server() -> Iterator[stubs.Stub]:
    stub = stubs.Stub()
    yield stub
    stub.close()


@pytest.fixture
def stub(server: stubs.Stub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> stubs.Stub:
    server.reset()
    for name in list(os.environ):
        if name.startswith("DOCKACCESS_"):
            monkeypatch.delenv(name)
    for name, value in server.env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    return server


def connect(stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    monkeypatch.setenv("DOCKACCESS_PASSWORD", stubs.PASSWORD)
    return main(
        [
            "connect",
            "--no-input",
            "--token",
            stubs.TOKEN,
            "--login",
            stubs.LOGIN,
            "--url",
            "https://shop.example.com",
            *extra,
        ]
    )


def test_connect_writes_a_private_env_file_and_status_reads_it(
    stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert connect(stub, monkeypatch) == 0
    env = read_env_file(".env")
    assert env == {
        "DOCKACCESS_ALT_KEY": stubs.KEY,
        "DOCKACCESS_ALT_SITE_TOKEN": stubs.TOKEN,
        "DOCKACCESS_ALT_SECRET": stubs.SECRET,
        "DOCKACCESS_ALT_HOME_URL": "https://shop.example.com",
    }
    assert stat.S_IMODE(os.stat(".env").st_mode) == 0o600
    sent = json.loads(stub.called("/fast-api/integrations/connect")[0]["body"])
    assert sent["system"] == "python" and sent["home_url"] == "https://shop.example.com"
    output = capsys.readouterr()
    assert stubs.PASSWORD not in output.out + output.err and stubs.KEY not in output.out + output.err

    assert main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        report["state"] == "active"
        and report["usage"]["remaining"] == 10
        and report["sites"] == {"used": 1, "limit": 1}
    )
    assert report["site_token"] == "Site*****"


def test_connect_explains_refusals_and_asks_before_replacing(
    stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOCKACCESS_PASSWORD", "wrong")
    assert (
        main(
            [
                "connect",
                "--no-input",
                "--token",
                stubs.TOKEN,
                "--login",
                stubs.LOGIN,
                "--url",
                "https://shop.example.com",
            ]
        )
        == 3
    )
    assert "password" in capsys.readouterr().err
    assert not Path(".env").exists()

    assert connect(stub, monkeypatch) == 0
    assert connect(stub, monkeypatch) == 3
    assert "--overwrite" in capsys.readouterr().err
    assert connect(stub, monkeypatch, "--overwrite", "--show") == 0
    assert f"DOCKACCESS_ALT_KEY={stubs.KEY}" in capsys.readouterr().out


def test_connect_without_input_names_what_is_missing(stub: stubs.Stub, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "--no-input", "--token", stubs.TOKEN]) == 2
    assert "required" in capsys.readouterr().err


def test_generate_writes_a_manifest_and_skips_unchanged_images(
    stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connect(stub, monkeypatch)
    stub.mode["texts"] = ["A red square", "A green field", "A blue sky"]
    images = Path("static/img")
    (images / "nested").mkdir(parents=True)
    (images / "a.jpg").write_bytes(jpeg())
    (images / "logo.png").write_bytes(png())
    (images / "nested" / "b.jpg").write_bytes(jpeg(color=(1, 2, 3)))
    (images / "notes.txt").write_text("not an image")
    (images / "broken.jpg").write_bytes(b"\xff\xd8\xff broken")
    capsys.readouterr()

    code = main(["generate", "static/img", "-r", "--lang", "pl", "--manifest", "alts.json", "--format", "jsonl"])
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 1  # the broken file
    assert {r["path"]: r["status"] for r in records} == {
        "static/img/a.jpg": "generated",
        "static/img/broken.jpg": "failed",
        "static/img/logo.png": "generated",
        "static/img/nested/b.jpg": "generated",
    }
    manifest = json.loads(Path("alts.json").read_text())
    assert manifest["version"] == 1 and set(manifest["images"]) == {
        "static/img/a.jpg",
        "static/img/logo.png",
        "static/img/nested/b.jpg",
    }
    entry = manifest["images"]["static/img/a.jpg"]
    assert entry["languages"]["pl"]["alt"] == "A red square" and len(entry["sha256"]) == 64
    assert [call["query"]["set_ref"] for call in stub.called("/api/generate")] == [stubs.TOKEN] * 3

    (images / "broken.jpg").unlink()
    (images / "a.jpg").write_bytes(jpeg(color=(9, 9, 9)))
    assert main(["generate", "static/img", "-r", "--lang", "pl", "--manifest", "alts.json"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "static/img/a.jpg: A red square" in lines
    assert "static/img/logo.png: A green field (cached)" in lines
    assert len(stub.called("/api/generate")) == 4, "only the changed image was generated again"

    assert main(["generate", "static/img", "-r", "--lang", "en", "--manifest", "alts.json", "--max", "1"]) == 0
    manifest = json.loads(Path("alts.json").read_text())
    assert sum("en" in item["languages"] for item in manifest["images"].values()) == 1
    assert "2 not attempted" in capsys.readouterr().err


def test_generate_stops_at_the_limit_with_its_own_exit_code(
    stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connect(stub, monkeypatch)
    stub.limit = 1
    for name in ("a", "b", "c"):
        Path(f"{name}.jpg").write_bytes(jpeg(color=(len(name), 0, 0)))
    assert main(["generate", "a.jpg", "b.jpg", "c.jpg"]) == 4
    err = capsys.readouterr().err
    assert "1 generated, 1 failed, 1 not attempted" in err and "limit" in err


def test_generate_refuses_when_the_account_paused_the_site(
    stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connect(stub, monkeypatch)
    stub.mode["paused"] = True
    Path("a.jpg").write_bytes(jpeg())
    assert main(["generate", "a.jpg"]) == 3
    assert "paused" in capsys.readouterr().err
    assert stub.called("/api/generate") == []


def test_check_and_dry_run_send_nothing(stub: stubs.Stub, capsys: pytest.CaptureFixture[str]) -> None:
    Path("a.jpg").write_bytes(jpeg((4000, 1000)))
    Path("bad.png").write_bytes(b"\x89PNG\r\n\x1a\nbroken")
    assert main(["check", "a.jpg", "bad.png"]) == 1
    out = capsys.readouterr().out
    assert "a.jpg: ok, would send 2048x512 JPEG" in out and "bad.png: FAILED" in out
    assert main(["generate", "a.jpg", "--dry-run", "--format", "jsonl"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert stub.calls == []


def test_generate_without_a_connection_says_how_to_connect(
    stub: stubs.Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    Path("a.jpg").write_bytes(jpeg())
    assert main(["generate", "a.jpg"]) == 2
    assert "dockaccess-alt connect" in capsys.readouterr().err
    assert main(["generate", "missing.jpg", "--dry-run"]) == 2


def test_disconnect_removes_the_keys(
    stub: stubs.Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connect(stub, monkeypatch)
    Path(".env").write_text(Path(".env").read_text() + "OTHER=1\n")
    assert main(["disconnect", "--no-input", "--login", stubs.LOGIN]) == 0
    assert read_env_file(".env") == {"OTHER": "1"}
    assert (
        json.loads(stub.called("/fast-api/integrations/disconnect")[0]["body"])["home_url"]
        == "https://shop.example.com"
    )
    assert main(["status"]) == 3


# -- the real transport -------------------------------------------------------------------------------------------


def loopback(stub: stubs.Stub, **settings: object) -> AltGenerator:
    values: dict[str, object] = {
        "account_key": stubs.KEY,
        "check_connection": False,
        "base_url": stub.url,
        "allow_insecure_loopback": True,
    }
    values.update(settings)
    return AltGenerator(Config(**values))  # type: ignore[arg-type]


def test_a_redirect_is_never_followed(stub: stubs.Stub) -> None:
    stub.mode["redirect"] = True
    with pytest.raises(UnexpectedResponseError):
        loopback(stub).generate(jpeg())
    assert len(stub.called("/api/generate")) == 1


def test_an_oversized_answer_aborts_the_transfer(stub: stubs.Stub) -> None:
    stub.mode["huge"] = True
    with pytest.raises(TransportError, match="more data than allowed"):
        loopback(stub).generate(jpeg())


def test_a_refused_connection_is_retried_and_explained(stub: stubs.Stub) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    generator = loopback(stub, base_url=f"http://127.0.0.1:{port}", connect_retries=1)
    with pytest.raises(TransportError) as caught:
        generator.generate(jpeg())
    assert caught.value.retryable and "could not be reached" in str(caught.value)
    assert stubs.KEY not in str(caught.value)


def test_the_real_transport_generates_and_reads_usage(stub: stubs.Stub) -> None:
    with loopback(stub) as generator:
        assert generator.generate(jpeg(), language="fr").text == "Generated alt 1 (fr)"
        usage = generator.usage()
    assert usage.used == 1 and usage.resets_at is not None
    assert stub.called("/api/generate")[0]["headers"]["Content-Type"].startswith("multipart/form-data")
