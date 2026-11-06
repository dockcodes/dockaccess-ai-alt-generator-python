from __future__ import annotations

import copy
import os
import pickle
import stat
from pathlib import Path

import pytest

from dockaccess_alt import (
    LANGUAGES,
    TONES,
    Config,
    ConfigurationError,
    GenerateOptions,
    Secret,
    Tone,
    clean_text,
    normalize_language,
    try_normalize_language,
)
from dockaccess_alt.envfile import read_env_file, update_env_file


@pytest.mark.parametrize(
    ("locale", "code"),
    [
        ("pl_PL", "pl"),
        ("en-GB", "en"),
        ("zh-TW", "zh-Hant"),
        ("zh_CN", "zh-Hans"),
        ("zh", "zh-Hans"),
        ("nb_NO", "no"),
        ("ZH-hant", "zh-Hant"),
        ("fil", "fil"),
        ("tl", "fil"),
    ],
)
def test_locales_map_onto_the_generators_languages(locale: str, code: str) -> None:
    assert try_normalize_language(locale) == code


def test_unknown_locales_fall_back_or_are_refused() -> None:
    assert try_normalize_language("xx_YY") is None and try_normalize_language("../etc") is None
    assert normalize_language("tlh", "en") == "en"
    with pytest.raises(ConfigurationError):
        GenerateOptions(language="klingon")
    assert len(LANGUAGES) == 84 and len(TONES) == 19


def test_options_are_validated_and_cleaned() -> None:
    options = GenerateOptions(
        language="de_AT",
        tone=Tone.MIDDLE_SCHOOL,
        context="<p>Shop</p>\n" + "x" * 600,
        variants=3,
        set_ref=" ref ",
        set_uuid="AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
    )
    assert options.to_query() == {
        "lang": "de", "tone": "middle school", "variants": "3", "context": ("Shop " + "x" * 600)[:500],
        "set_ref": "ref", "set_uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    }  # fmt: skip
    for bad in (
        {"tone": "angry"},
        {"variants": 4},
        {"variants": True},
        {"set_ref": "a\nb"},
        {"set_ref": "x" * 256},
        {"set_uuid": "nope"},
    ):
        with pytest.raises(ConfigurationError):
            GenerateOptions(**bad)  # type: ignore[arg-type]
    assert options.replace(language=None, tone="vivid").tone == "vivid"


def test_text_is_cleaned_to_one_safe_line() -> None:
    assert (
        clean_text("A\u202ecat\u200b <script>alert(1)</script>on\u2028a\x00 mat<!-- x -->") == "Acat alert(1)on a mat"
    )
    assert clean_text(b"caf\xc3\xa9 \xff\xfe") == "café"
    assert clean_text("a < b and c > d") == "a < b and c > d"
    assert clean_text("abcdef", 3) == "abc" and clean_text("x", 0) == ""


def test_secrets_do_not_print_or_pickle() -> None:
    secret = Secret("s3cret-value")
    assert str(secret) == f"{secret}" == "[redacted]" and "s3cret" not in repr(secret)
    assert secret == Secret("s3cret-value") and secret.reveal() == "s3cret-value"
    assert copy.deepcopy(secret) is secret
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    for bad in ("", "with space", "a\nb"):
        with pytest.raises(ConfigurationError):
            Secret(bad)
    assert Secret.of("  ") is None and Secret.of(None) is None


def test_the_config_is_secure_by_default() -> None:
    for url in (
        "http://alt.dockaccess.org",
        "https://user:pass@alt.dockaccess.org",
        "https://alt.dockaccess.org/?x=1",
        "ftp://x",
        "alt.dockaccess.org",
    ):
        with pytest.raises(ConfigurationError):
            Config(base_url=url)
    with pytest.raises(ConfigurationError):
        Config(base_url="http://example.com", allow_insecure_loopback=True)
    assert Config(base_url="http://127.0.0.1:9000/", allow_insecure_loopback=True).base_url == "http://127.0.0.1:9000"
    assert Config(base_url="http://[::1]:9000", allow_insecure_loopback=True).base_url == "http://[::1]:9000"
    for bad in (
        {"timeout": 500},
        {"connect_timeout": 0},
        {"connect_timeout": 10, "timeout": 5},
        {"connect_retries": 4},
        {"max_alt_length": 10},
        {"ca_bundle": "/nope"},
        {"site_token": "a b"},
    ):
        with pytest.raises(ConfigurationError):
            Config(**bad)  # type: ignore[arg-type]
    config = Config(account_key="key-1", proxy="http://user:pw@proxy:3128", user_agent_suffix="myshop/1.2 <script>")
    assert "key-1" not in repr(config) and "pw@" not in repr(config)
    assert config.user_agent.endswith("myshop/1.2 script")


def test_the_config_reads_the_environment_and_a_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DOCKACCESS_ALT_KEY=from-file\nexport DOCKACCESS_ALT_SITE_TOKEN="Tok3n"\nDOCKACCESS_ALT_TONE=vivid # comment\n'
    )
    config = Config.from_env(
        {
            "DOCKACCESS_ALT_KEY": "from-env",
            "DOCKACCESS_ALT_LANGUAGE": "pl_PL",
            "DOCKACCESS_ALT_VERIFY": "false",
            "DOCKACCESS_ALT_VARIANTS": "2",
        },
        env_file=env_file,
    )
    assert config.account_key == Secret("from-env") and config.site_token == "Tok3n"
    assert (config.defaults.language, config.defaults.tone, config.defaults.variants) == ("pl", "vivid", 2)
    assert config.check_connection is False and not config.guards_connection
    assert config.default_options().set_ref == "Tok3n"
    with pytest.raises(ConfigurationError):
        Config.from_env({"DOCKACCESS_ALT_VERIFY": "maybe"})
    with pytest.raises(ConfigurationError):
        Config.from_env({"DOCKACCESS_ALT_TIMEOUT": "soon"})
    assert Config.from_env({}).account_key is None


def test_env_files_are_updated_in_place_and_created_private(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    update_env_file(path, {"DOCKACCESS_ALT_KEY": "k1", "DOCKACCESS_ALT_SECRET": 'with "quotes" and spaces'})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_env_file(path) == {"DOCKACCESS_ALT_KEY": "k1", "DOCKACCESS_ALT_SECRET": 'with "quotes" and spaces'}

    path.write_text("# app\nDEBUG=1\nDOCKACCESS_ALT_KEY=old\n")
    os.chmod(path, 0o640)
    update_env_file(path, {"DOCKACCESS_ALT_KEY": "new", "DOCKACCESS_ALT_SITE_TOKEN": "T"})
    assert path.read_text() == "# app\nDEBUG=1\nDOCKACCESS_ALT_KEY=new\n\nDOCKACCESS_ALT_SITE_TOKEN=T\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    update_env_file(path, {"DOCKACCESS_ALT_KEY": None, "DOCKACCESS_ALT_SITE_TOKEN": None})
    assert path.read_text() == "# app\nDEBUG=1\n"
    update_env_file(path, {"DEBUG": None})
    update_env_file(path, {"X": None})
    assert path.read_text() == "# app\n"
    assert read_env_file(tmp_path / "missing") == {}
