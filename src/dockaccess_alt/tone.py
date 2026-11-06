"""Tones the generator writes in (``tone`` of ``POST /api/generate``)."""

from __future__ import annotations

from enum import Enum


class Tone(str, Enum):
    NEUTRAL = "neutral"
    CONCISE = "concise"
    DESCRIPTIVE = "descriptive"
    DIRECT = "direct"
    SIMPLIFIED = "simplified"
    FORMAL = "formal"
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    CASUAL = "casual"
    ENGAGING = "engaging"
    EMPATHETIC = "empathetic"
    DIPLOMATIC = "diplomatic"
    ACADEMIC = "academic"
    MIDDLE_SCHOOL = "middle school"
    HIGH_SCHOOL = "high school"
    VIVID = "vivid"
    LUXURY = "luxury"
    PERSUASIVE = "persuasive"
    CONFIDENT = "confident"

    def __str__(self) -> str:
        return self.value


#: Every tone, as the strings the API takes.
TONES: tuple[str, ...] = tuple(tone.value for tone in Tone)
