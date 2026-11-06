"""Turns text from outside (the API, a user, a push) into one safe line of plain text."""

from __future__ import annotations

import re

# C0/C1 controls, bidi embeddings, overrides and isolates, zero-width characters, BOM and
# interlinear annotations. Line and paragraph separators become spaces instead (they separate words).
_INVISIBLE = re.compile(
    r"[\u0000-\u0008\u000e-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]"
)
_SEPARATORS = re.compile(r"[\u2028\u2029]")
_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_TAG = re.compile(r"<[A-Za-z/!?][^>]*(?:>|$)", re.DOTALL)
_SPACE = re.compile(r"\s+")


def clean_text(text: str | bytes, max_length: int = 2000) -> str:
    """One line of plain text: no markup, control characters, invisible direction overrides ("Trojan
    Source"), zero-width characters or invalid UTF-8, at most ``max_length`` characters.

    This is not HTML escaping: escape the result wherever it is printed (Jinja2 and Django templates do).
    Alts returned by the generator have been through it already; use it for alts that arrive another way.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="ignore")
    # Lone surrogates cannot be encoded and are no text anyway.
    text = text.encode("utf-8", errors="ignore").decode("utf-8")
    text = _COMMENT.sub("", text)
    text = _TAG.sub("", text)
    text = _SEPARATORS.sub(" ", text)
    text = _INVISIBLE.sub("", text)
    text = _SPACE.sub(" ", text).strip()
    if max_length <= 0:
        return ""
    return text[:max_length].rstrip() if len(text) > max_length else text
