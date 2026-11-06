"""Languages the generator writes in, and how locales map onto them."""

from __future__ import annotations

import re

#: Codes the API accepts (``lang`` of ``POST /api/generate``).
LANGUAGES: tuple[str, ...] = (
    "af", "sq", "ar", "hy", "az", "eu", "be", "bn", "bs", "bg", "my", "ca", "zh-Hans", "zh-Hant", "hr", "cs",
    "da", "nl", "en", "eo", "et", "fil", "fi", "fr", "gl", "ka", "de", "el", "gu", "he", "hi", "hu", "is", "id",
    "ga", "it", "ja", "kn", "kk", "km", "ko", "ku", "ky", "lo", "la", "lv", "lt", "mk", "ms", "ml", "mr", "mn",
    "ne", "no", "fa", "pl", "csb", "szl", "pt", "pa", "ro", "ru", "gd", "sr", "sd", "si", "sk", "sl", "so", "es",
    "sw", "sv", "ta", "te", "th", "tr", "uk", "ur", "uz", "vi", "cy", "xh", "yo", "zu",
)  # fmt: skip

# Locales whose primary subtag alone would pick the wrong script or language.
_ALIASES = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "nb": "no",
    "nn": "no",
    "iw": "he",
    "in": "id",
    "tl": "fil",
}
_BY_LOWERCASE = {code.lower(): code for code in LANGUAGES}
_TAG = re.compile(r"^[a-z0-9-]{1,35}$")


def try_normalize_language(locale: str) -> str | None:
    """The generator code for a locale (``pl_PL``, ``en-GB``, ``zh-TW``), or None when it has none.

    An exact code wins, then language and region, then the primary subtag - the API's own rule.
    """
    if not isinstance(locale, str):
        return None
    tag = locale.strip().replace("_", "-").lower()
    if not _TAG.match(tag):
        return None
    parts = tag.split("-")
    candidates = [tag, "-".join(parts[:2]), parts[0]]
    for candidate in candidates:
        if candidate in _ALIASES:
            return _ALIASES[candidate]
        if candidate in _BY_LOWERCASE:
            return _BY_LOWERCASE[candidate]
    return None


def normalize_language(locale: str, fallback: str = "en") -> str:
    """The generator code for a locale, or ``fallback`` - for site locales that may be anything."""
    return try_normalize_language(locale) or fallback
