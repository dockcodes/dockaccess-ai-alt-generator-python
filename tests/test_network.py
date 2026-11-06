"""Against the real services - run with DOCKACCESS_ALT_NETWORK_TESTS=1.

The generator is called with a made-up key, which the API refuses before it reads the image: nothing is
stored and no limit is used. Certificate checks run against badssl.com.
"""

from __future__ import annotations

import os

import pytest

from dockaccess_alt import (
    AltGenerator,
    AuthenticationError,
    Config,
    ConnectionClient,
    SiteConnectionError,
    TransportError,
)

from .conftest import jpeg

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("DOCKACCESS_ALT_NETWORK_TESTS") != "1", reason="set DOCKACCESS_ALT_NETWORK_TESTS=1"
    ),
]


def test_the_generator_refuses_a_made_up_key() -> None:
    config = Config(account_key="made-up-key-for-a-test", check_connection=False)
    with AltGenerator(config) as generator, pytest.raises(AuthenticationError):
        generator.generate(jpeg((64, 64)))


def test_dockaccess_refuses_an_unknown_connection() -> None:
    with ConnectionClient() as client, pytest.raises(SiteConnectionError) as caught:
        client.verify("made-up-site-token", "made-up-connection-secret")
    assert caught.value.is_connection_gone or caught.value.reason in ("invalid_request", "refused")


@pytest.mark.parametrize(
    "host", ["expired.badssl.com", "wrong.host.badssl.com", "self-signed.badssl.com", "untrusted-root.badssl.com"]
)
def test_bad_certificates_are_refused(host: str) -> None:
    with (
        AltGenerator(
            Config(account_key="made-up-key", base_url=f"https://{host}", check_connection=False)
        ) as generator,
        pytest.raises(TransportError),
    ):
        generator.usage()


def test_old_tls_is_refused() -> None:
    with (
        AltGenerator(
            Config(account_key="made-up-key", base_url="https://tls-v1-1.badssl.com:1011", check_connection=False)
        ) as generator,
        pytest.raises(TransportError),
    ):
        generator.usage()
