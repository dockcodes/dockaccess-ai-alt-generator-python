"""What to generate: language, tone, context, variants and the image set."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any

from .errors import ConfigurationError
from .language import try_normalize_language
from .text import clean_text
from .tone import TONES, Tone

#: The API refuses longer contexts; a longer one is cut, not refused - it is a hint, not content.
MAX_CONTEXT_LENGTH = 500
MAX_SET_REF_LENGTH = 255

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.match(value) is not None


@dataclass(frozen=True)
class GenerateOptions:
    """Immutable, validated on creation.

    - ``language`` - a generator code or any locale (``pl_PL``, ``en-GB``); an unsupported one is refused,
      use :func:`normalize_language` for site locales that may be anything,
    - ``tone`` - one of :class:`Tone`,
    - ``context`` - a hint about the site or the image ("an online shop with handmade ceramics"), plain text,
      cut to 500 characters,
    - ``variants`` - 1 to 3 alternatives; the image counts once against the limit whatever the number,
    - ``set_ref`` - your own identifier (a site token, a shop id) that keeps the images in one set of the
      account; the same file name and bytes under the same ref replace that image's alts,
    - ``set_uuid`` - adds the image to an existing set of the account.
    """

    language: str = "en"
    tone: Tone | str = Tone.NEUTRAL
    context: str = ""
    variants: int = 1
    set_ref: str | None = None
    set_uuid: str | None = None

    def __post_init__(self) -> None:
        language = try_normalize_language(self.language) if isinstance(self.language, str) else None
        if language is None:
            raise ConfigurationError(f"The alt generator does not write in this language: {self.language!r}.")
        object.__setattr__(self, "language", language)

        tone = self.tone.value if isinstance(self.tone, Tone) else self.tone
        if tone not in TONES:
            raise ConfigurationError(f"Unknown tone {tone!r}; use one of {', '.join(TONES)}.")
        object.__setattr__(self, "tone", tone)

        if not isinstance(self.context, str):
            raise ConfigurationError("The context must be text.")
        object.__setattr__(self, "context", clean_text(self.context, MAX_CONTEXT_LENGTH))

        if isinstance(self.variants, bool) or not isinstance(self.variants, int) or not 1 <= self.variants <= 3:
            raise ConfigurationError("Variants must be between 1 and 3.")

        if self.set_ref is not None:
            ref = self.set_ref.strip() if isinstance(self.set_ref, str) else ""
            if ref == "" or len(ref.encode()) > MAX_SET_REF_LENGTH or _CONTROL.search(ref):
                raise ConfigurationError("The set ref must be 1 to 255 bytes without control characters.")
            object.__setattr__(self, "set_ref", ref)

        if self.set_uuid is not None:
            if not is_uuid(self.set_uuid):
                raise ConfigurationError("The set uuid is not a UUID.")
            object.__setattr__(self, "set_uuid", self.set_uuid.lower())

    def replace(self, **changes: Any) -> GenerateOptions:
        """A copy with the given fields changed; ``None`` values are ignored."""
        changes = {key: value for key, value in changes.items() if value is not None}
        return dataclasses.replace(self, **changes) if changes else self

    def to_query(self) -> dict[str, str]:
        """The query parameters of ``POST /api/generate``, without the key."""
        query = {"lang": self.language, "tone": str(self.tone), "variants": str(self.variants)}
        if self.context:
            query["context"] = self.context
        if self.set_ref is not None:
            query["set_ref"] = self.set_ref
        if self.set_uuid is not None:
            query["set_uuid"] = self.set_uuid
        return query
