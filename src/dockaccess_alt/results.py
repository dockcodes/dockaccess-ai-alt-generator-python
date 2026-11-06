"""What the services answer, as immutable objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .image import PreparedImage
from .secret import Secret

DAY = "day"
MONTH = "month"


@dataclass(frozen=True)
class Alt:
    """One alt text: one line of plain text, no markup or invisible characters - still to be escaped where
    it is printed (templates do that)."""

    text: str
    locale: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class GenerationResult:
    """What the generator made of one image.

    Keep :attr:`image_uuid` with your image to send an edited alt back later (:meth:`update_alts`).
    """

    image_uuid: str
    set_uuid: str
    set_ref: str | None
    alts: tuple[Alt, ...]
    image: PreparedImage = field(repr=False)

    @property
    def alt(self) -> Alt:
        """The first alt - the one to use when only one is wanted."""
        return self.alts[0]

    @property
    def text(self) -> str:
        """The first alt's text."""
        return self.alts[0].text

    @property
    def language(self) -> str:
        return self.alts[0].locale

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready summary (without the image bytes) for logs, queues and pipeline output."""
        return {
            "alt": self.text,
            "language": self.language,
            "alts": [{"text": alt.text, "locale": alt.locale} for alt in self.alts],
            "image_uuid": self.image_uuid,
            "set_uuid": self.set_uuid,
            "set_ref": self.set_ref,
            "image": self.image.to_dict(),
        }


@dataclass(frozen=True)
class Usage:
    """Generations in the current window against the limit. The free plan counts the UTC day, paid plans
    the UTC calendar month (a whole month's images may go in one day)."""

    used: int
    limit: int
    period: str = DAY
    resets_at: datetime | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def is_exhausted(self) -> bool:
        return self.used >= self.limit

    @property
    def is_monthly(self) -> bool:
        return self.period == MONTH

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "period": self.period,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
        }


@dataclass(frozen=True)
class Verification:
    """The answer of ``integrations/verify``: may this site generate now?"""

    ACTIVE = "active"
    PAUSED = "alt_paused"
    DISABLED = "alt_disabled"

    state: str
    integration_id: int = 0
    system: str = ""
    usage: Usage | None = None
    websites_used: int | None = None
    websites_limit: int | None = None

    @property
    def is_active(self) -> bool:
        return self.state == self.ACTIVE


@dataclass(frozen=True)
class Connection:
    """A site connected to a dockaccess.org account - what ``integrations/connect`` hands back.

    Store the three values the integration needs later, all as secrets (an environment file, a secrets
    manager, encrypted settings - never a public config file): :attr:`site_token`, :attr:`account_key`
    (``DOCKACCESS_ALT_KEY``) and :attr:`secret` (``DOCKACCESS_ALT_SECRET``).
    """

    site_token: str
    account_key: Secret
    secret: Secret
    integration_id: int
    system: str
    home_url: str
    alt_enabled: bool
    domain_verified: bool
    account_name: str

    def environment(self) -> dict[str, str]:
        """The values as environment variables (revealed - write them only where secrets belong)."""
        return {
            "DOCKACCESS_ALT_KEY": self.account_key.reveal(),
            "DOCKACCESS_ALT_SITE_TOKEN": self.site_token,
            "DOCKACCESS_ALT_SECRET": self.secret.reveal(),
        }
