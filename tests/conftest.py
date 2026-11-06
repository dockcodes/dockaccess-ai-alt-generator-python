from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image

from dockaccess_alt.testing import FakeAltApi


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake() -> FakeAltApi:
    return FakeAltApi("A red square on a white background")


def jpeg(size: tuple[int, int] = (640, 480), color: tuple[int, int, int] = (200, 30, 30), **save: object) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", **save)
    return out.getvalue()


def photo_with_metadata() -> bytes:
    """A 4000x3000 photo turned by EXIF (orientation 6) that carries GPS, an owner, a comment and an ICC profile."""
    image = Image.new("RGB", (4000, 3000), (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90 CW
    exif[0x013B] = "Jan Kowalski"  # artist
    exif[0xA430] = "Camera Owner"  # camera owner name
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (54.0, 21.0, 3.0)
    gps[3], gps[4] = "E", (18.0, 38.0, 46.0)
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes(), comment=b"secret project", icc_profile=b"\0" * 128)
    return out.getvalue() + b"appended after the end"


def png(size: tuple[int, int] = (800, 600), alpha: int | None = 0, **save: object) -> bytes:
    out = io.BytesIO()
    if alpha is None:
        Image.new("RGB", size, (0, 128, 0)).save(out, "PNG", **save)
    else:
        image = Image.new("RGBA", size, (0, 128, 0, 255))
        image.paste((0, 0, 0, alpha), (0, 0, size[0] // 2, size[1] // 2))
        image.save(out, "PNG", **save)
    return out.getvalue()


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_declaring(width: int, height: int) -> bytes:
    """A PNG header that claims huge dimensions in a few bytes (a decompression bomb)."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(b"\0" * 64))
        + png_chunk(b"IEND", b"")
    )


def other(format: str, size: tuple[int, int] = (300, 200), mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, 120).save(out, format)
    return out.getvalue()
