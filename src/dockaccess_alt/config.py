"""How the client talks to dockaccess.org, and what it generates by default."""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ._version import __version__
from .errors import ConfigurationError
from .image import ImagePolicy
from .options import GenerateOptions
from .secret import REDACTED, Secret

DEFAULT_BASE_URL = "https://alt.dockaccess.org"
#: The dockaccess.org integration API (connect, verify, disconnect, alt updates).
DEFAULT_CONNECTION_URL = "https://dockaccess.org/fast-api"

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_SITE_TOKEN = re.compile(r"^[^\s\x00-\x1f\x7f]{1,255}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class Config:
    """Immutable; validated on creation. Secure by default: HTTPS only, certificates verified, TLS 1.2 or
    newer, no redirects, bounded timeouts.

    Connection (what ``dockaccess-alt connect`` writes):

    - ``account_key`` - the key the generator is called with (``DOCKACCESS_ALT_KEY``),
    - ``site_token`` - the site in the account (``DOCKACCESS_ALT_SITE_TOKEN``),
    - ``connection_secret`` - authenticates the connection check and alt pushes (``DOCKACCESS_ALT_SECRET``).

    With the site token and the secret set, the generator asks dockaccess.org whether it may still generate
    before it sends an image, and remembers the answer for ``verify_ttl`` seconds (``check_connection``).
    """

    account_key: Secret | str | None = None
    site_token: str | None = None
    connection_secret: Secret | str | None = None
    defaults: GenerateOptions = field(default_factory=GenerateOptions)
    check_connection: bool = True
    verify_ttl: float = 300.0
    base_url: str = DEFAULT_BASE_URL
    connection_url: str = DEFAULT_CONNECTION_URL
    connect_timeout: float = 5.0
    #: Generation includes an AI model call; the API itself waits up to about a minute for it.
    timeout: float = 90.0
    connect_retries: int = 1
    proxy: Secret | str | None = None
    ca_bundle: str | None = None
    trust_env: bool = True
    user_agent_suffix: str = ""
    max_alt_length: int = 2000
    image_policy: ImagePolicy = field(default_factory=ImagePolicy)
    #: Lets base_url and connection_url use http:// on 127.0.0.1, ::1 or localhost. Tests only.
    allow_insecure_loopback: bool = False

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "account_key", Secret.of(self.account_key))
        set_(self, "connection_secret", Secret.of(self.connection_secret))
        set_(self, "proxy", Secret.of(self.proxy))

        if self.site_token is not None:
            token = self.site_token.strip() if isinstance(self.site_token, str) else ""
            if token == "":
                token = None  # type: ignore[assignment]
            elif not _SITE_TOKEN.match(token):
                raise ConfigurationError("The site token is not valid.")
            set_(self, "site_token", token)

        if not isinstance(self.defaults, GenerateOptions):
            raise ConfigurationError("defaults must be GenerateOptions.")
        if not isinstance(self.image_policy, ImagePolicy):
            raise ConfigurationError("image_policy must be an ImagePolicy.")

        set_(self, "base_url", _validated_url(self.base_url, self.allow_insecure_loopback))
        set_(self, "connection_url", _validated_url(self.connection_url, self.allow_insecure_loopback))

        connect, total = float(self.connect_timeout), float(self.timeout)
        if not 0.1 <= connect <= 60 or not connect <= total <= 300:
            raise ConfigurationError("Timeouts must satisfy 0.1 <= connect <= 60 and connect <= total <= 300 seconds.")
        set_(self, "connect_timeout", connect)
        set_(self, "timeout", total)

        if isinstance(self.connect_retries, bool) or self.connect_retries not in range(4):
            raise ConfigurationError("Connect retries must be between 0 and 3.")
        if not 0 <= float(self.verify_ttl) <= 86400:
            raise ConfigurationError("verify_ttl must be between 0 and 86400 seconds.")
        set_(self, "verify_ttl", float(self.verify_ttl))
        if isinstance(self.max_alt_length, bool) or not 16 <= int(self.max_alt_length) <= 2000:
            raise ConfigurationError("The alt length must be between 16 and 2000 characters.")

        if self.ca_bundle is not None and not (os.path.isfile(self.ca_bundle) and os.access(self.ca_bundle, os.R_OK)):
            raise ConfigurationError("The CA bundle is not a readable file.")

        suffix = re.sub(r"[^A-Za-z0-9 ._/+()-]", "", str(self.user_agent_suffix)).strip()[:120]
        set_(self, "user_agent_suffix", suffix)

    # -- construction -------------------------------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: str | os.PathLike[str] | None = None,
        **overrides: Any,
    ) -> Config:
        """Settings from environment variables (and, if given, a ``.env`` file; the environment wins).

        ============================== ==============================================================
        ``DOCKACCESS_ALT_KEY``         the account key
        ``DOCKACCESS_ALT_SITE_TOKEN``  the site token
        ``DOCKACCESS_ALT_SECRET``      the connection secret
        ``DOCKACCESS_ALT_LANGUAGE``    default language (a locale), ``en``
        ``DOCKACCESS_ALT_TONE``        default tone, ``neutral``
        ``DOCKACCESS_ALT_CONTEXT``     default context
        ``DOCKACCESS_ALT_VARIANTS``    default number of variants, ``1``
        ``DOCKACCESS_ALT_SET_REF``     the image set in the account; the site token when empty
        ``DOCKACCESS_ALT_VERIFY``      check the connection before generating, ``true``
        ``DOCKACCESS_ALT_VERIFY_TTL``  how long an answer is remembered, ``300`` seconds
        ``DOCKACCESS_ALT_TIMEOUT``     total timeout, ``90`` seconds
        ``DOCKACCESS_ALT_PROXY``       an outgoing proxy (``HTTPS_PROXY`` is honoured as well)
        ``DOCKACCESS_ALT_CA_BUNDLE``   a CA bundle (``SSL_CERT_FILE`` is honoured as well)
        ============================== ==============================================================
        """
        values: dict[str, str] = {}
        if env_file is not None:
            from .envfile import read_env_file

            values.update(read_env_file(env_file))
        values.update(os.environ if environ is None else environ)

        def get(name: str) -> str | None:
            value = values.get("DOCKACCESS_ALT_" + name)
            return value.strip() if isinstance(value, str) and value.strip() != "" else None

        settings: dict[str, Any] = {
            "account_key": get("KEY"),
            "site_token": get("SITE_TOKEN"),
            "connection_secret": get("SECRET"),
        }
        defaults: dict[str, Any] = {
            "language": get("LANGUAGE"),
            "tone": get("TONE"),
            "context": get("CONTEXT"),
            "set_ref": get("SET_REF"),
            "variants": _integer(get("VARIANTS"), "DOCKACCESS_ALT_VARIANTS"),
        }
        settings["defaults"] = GenerateOptions().replace(**defaults)
        if get("VERIFY") is not None:
            settings["check_connection"] = _boolean(get("VERIFY"), "DOCKACCESS_ALT_VERIFY")
        for name, key in (("VERIFY_TTL", "verify_ttl"), ("TIMEOUT", "timeout"), ("CONNECT_TIMEOUT", "connect_timeout")):
            if get(name) is not None:
                settings[key] = _number(get(name), "DOCKACCESS_ALT_" + name)
        for name, key in (("URL", "base_url"), ("CONNECTION_URL", "connection_url"), ("CA_BUNDLE", "ca_bundle")):
            if get(name) is not None:
                settings[key] = get(name)
        if get("PROXY") is not None:
            settings["proxy"] = get("PROXY")
        if get("INSECURE_LOOPBACK") is not None:
            settings["allow_insecure_loopback"] = _boolean(get("INSECURE_LOOPBACK"), "DOCKACCESS_ALT_INSECURE_LOOPBACK")
        settings.update(overrides)
        return cls(**settings)

    def replace(self, **changes: Any) -> Config:
        return dataclasses.replace(self, **changes)

    # -- what the rest of the package reads ----------------------------------------------------------------------

    def require_account_key(self) -> Secret:
        if not isinstance(self.account_key, Secret):
            raise ConfigurationError(
                "No account key is configured. Connect the application first: dockaccess-alt connect "
                "(or set DOCKACCESS_ALT_KEY)."
            )
        return self.account_key

    @property
    def is_connected(self) -> bool:
        return self.account_key is not None

    @property
    def guards_connection(self) -> bool:
        """Whether generation asks dockaccess.org first (a site token and a secret are set, and it is on)."""
        return bool(self.check_connection and self.site_token and self.connection_secret and self.verify_ttl >= 0)

    @property
    def user_agent(self) -> str:
        return f"dockaccess-alt-generator-python/{__version__} {self.user_agent_suffix}".strip()

    def default_options(self) -> GenerateOptions:
        """The defaults, with the image set of the site when no other is named."""
        if self.defaults.set_ref is None and self.defaults.set_uuid is None and self.site_token:
            return self.defaults.replace(set_ref=self.site_token)
        return self.defaults

    def __repr__(self) -> str:
        shown = []
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            if item.name in ("account_key", "connection_secret", "proxy"):
                value = None if value is None else REDACTED
            shown.append(f"{item.name}={value!r}")
        return f"Config({', '.join(shown)})"


def _validated_url(url: str, insecure_loopback: bool) -> str:
    if not isinstance(url, str):
        raise ConfigurationError("A service URL must be a string.")
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        raise ConfigurationError("A service URL must be absolute.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigurationError("A service URL must not carry credentials, a query or a fragment.")
    scheme, host = parts.scheme.lower(), parts.hostname.lower()
    if scheme != "https" and not (scheme == "http" and insecure_loopback and host in _LOOPBACK):
        raise ConfigurationError("A service URL must use HTTPS.")
    if not re.match(r"^[a-z0-9.\-:]+$", host):
        raise ConfigurationError("The host of a service URL is not valid.")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        raise ConfigurationError("The port of a service URL is not valid.") from None
    netloc = f"[{host}]" if ":" in host else host
    return f"{scheme}://{netloc}{port}{parts.path.rstrip('/')}"


def _boolean(value: str | None, name: str) -> bool:
    lowered = (value or "").lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _integer(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ConfigurationError(f"{name} must be a whole number.") from None


def _number(value: str | None, name: str) -> float:
    try:
        return float(value or "")
    except ValueError:
        raise ConfigurationError(f"{name} must be a number.") from None
