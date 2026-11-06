from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image, features

from dockaccess_alt import ConfigurationError, ImagePolicy, ImageSource, InvalidImageError, prepare_image
from dockaccess_alt.image import sniff, upload_name

from .conftest import jpeg, other, photo_with_metadata, png, png_chunk, png_declaring


def prepared(data: bytes, name: str = "image", policy: ImagePolicy | None = None):
    return prepare_image(ImageSource.from_bytes(data, name), policy)


def reason(data: bytes, policy: ImagePolicy | None = None) -> str:
    with pytest.raises(InvalidImageError) as caught:
        prepared(data, policy=policy)
    return caught.value.reason


def test_a_photo_is_sent_upright_scaled_down_and_without_any_metadata() -> None:
    original = photo_with_metadata()
    image = prepared(original, "IMG_0001.JPG")

    assert (image.format, image.width, image.height) == ("jpeg", 1536, 2048)  # turned upright, longest side 2048
    assert image.name == "IMG_0001.jpg"
    assert image.source_format == "jpeg" and image.source_size == len(original)
    for marker in (
        b"Exif",
        b"Kowalski",
        b"Camera Owner",
        b"secret project",
        b"ICC_PROFILE",
        b"appended",
        b"http://ns.adobe.com",
    ):
        assert marker not in image.data
    with Image.open(io.BytesIO(image.data)) as sent:
        assert not sent.getexif()
        assert "icc_profile" not in sent.info and "comment" not in sent.info


def test_transparency_is_kept_as_webp_and_an_opaque_alpha_channel_is_dropped() -> None:
    transparent = prepared(png(alpha=0), "logo.png")
    assert (transparent.format, transparent.name, transparent.mime_type) == ("webp", "logo.webp", "image/webp")
    with Image.open(io.BytesIO(transparent.data)) as sent:
        assert sent.mode == "RGBA"

    opaque = prepared(png(alpha=255), "banner.png")
    assert opaque.format == "jpeg" and opaque.name == "banner.jpg"


@pytest.mark.parametrize(
    ("data", "format"),
    [
        (other("GIF"), "gif"),
        (other("WEBP"), "webp"),
        (other("PNG", mode="P"), "png"),
        (other("PNG", mode="L"), "png"),
        (other("PNG", mode="I;16"), "png"),
        (other("JPEG", mode="CMYK"), "jpeg"),
        pytest.param(
            "AVIF", "avif", marks=pytest.mark.skipif(not features.check("avif"), reason="this Pillow has no AVIF")
        ),
    ],
)
def test_every_supported_format_is_rebuilt(data: bytes | str, format: str) -> None:
    image = prepared(other(data) if isinstance(data, str) else data)
    assert image.source_format == format
    assert image.format in ("jpeg", "webp") and (image.width, image.height) == (300, 200)


def test_small_images_are_not_enlarged_and_the_edge_follows_the_policy() -> None:
    assert (prepared(jpeg((100, 50))).width, prepared(jpeg((100, 50))).height) == (100, 50)
    small = prepared(jpeg((3000, 1000)), policy=ImagePolicy(max_edge=600))
    assert (small.width, small.height) == (600, 200)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", "empty"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "unsupported_format"),
        (b"<?php system($_GET['c']); ?>", "unsupported_format"),
        (b"GIF89a<html><script>alert(1)</script></html>", "malformed"),
        (b"%PDF-1.7\n", "unsupported_format"),
        (jpeg()[:400], "malformed"),
        (png_declaring(60000, 60000), "bad_dimensions"),
        (png_declaring(10000, 9000), "bad_dimensions"),
    ],
)
def test_what_is_not_a_genuine_image_is_refused_locally(data: bytes, expected: str) -> None:
    assert reason(data) == expected


def test_a_png_whose_header_checksum_lies_is_refused() -> None:
    data = bytearray(png(alpha=None))
    data[29] ^= 0xFF  # the CRC of IHDR
    assert reason(bytes(data)) == "malformed"


def test_a_shell_named_like_a_photo_is_refused_and_the_name_never_decides() -> None:
    with pytest.raises(InvalidImageError):
        prepared(b"#!/bin/sh\nrm -rf /\n", "photo.png")
    assert prepared(png(alpha=None), "shell.php.png").name == "shell-php.jpg"


def test_the_policy_narrows_formats_and_size() -> None:
    assert reason(other("GIF"), ImagePolicy(formats=frozenset({"jpeg", "png"}))) == "unsupported_format"
    assert reason(jpeg((2000, 2000), quality=100), ImagePolicy(max_bytes=2048)) == "too_large"
    with pytest.raises(ConfigurationError):
        ImagePolicy(max_bytes=20 * 1024 * 1024)
    with pytest.raises(ConfigurationError):
        ImagePolicy(formats=frozenset({"svg"}))


def test_pillow_images_are_rebuilt_from_their_pixels_and_left_untouched() -> None:
    image = Image.new("RGB", (3000, 1500), (1, 2, 3))
    image.info["comment"] = b"keep out"
    sent = prepare_image(ImageSource.from_pil(image, "frame"))
    assert (sent.width, sent.height, sent.name, sent.source_format) == (2048, 1024, "frame.jpg", "pil")
    assert image.size == (3000, 1500)


def test_sniffing_reads_the_content_not_the_name() -> None:
    assert sniff(jpeg()) == "jpeg" and sniff(png()) == "png" and sniff(other("GIF")) == "gif"
    assert sniff(other("WEBP")) == "webp"
    if features.check("avif"):
        assert sniff(other("AVIF")) == "avif"
    heic = b"\0\0\0\x18ftypheic\0\0\0\0mif1heic"
    assert sniff(heic) == "heic"
    assert sniff(b"<svg/>") is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd.jpg", "passwd.jpg"),
        ("C:\\Users\\me\\Zdjęcie 1.JPG", "Zdj-cie-1.jpg"),
        (".htaccess", "htaccess.jpg"),
        ("", "image.jpg"),
        ("a" * 300 + ".png", "a" * 100 + ".jpg"),
    ],
)
def test_upload_names_are_harmless(name: str, expected: str) -> None:
    assert upload_name(name, "jpeg") == expected


# -- reading from disk ------------------------------------------------------------------------------------------------


def test_a_file_is_read_only_inside_the_allowed_directory(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "cat.jpg").write_bytes(jpeg())
    (tmp_path / "settings.py").write_text("SECRET_KEY = 'x'")

    assert ImageSource.from_path(media / "cat.jpg", within=media).name == "cat.jpg"
    for path in (
        media / ".." / "settings.py",
        tmp_path / "settings.py",
        media / "missing.jpg",
        "http://example.com/a.jpg",
        "file:///etc/passwd",
    ):
        with pytest.raises(InvalidImageError) as caught:
            ImageSource.from_path(path, within=media)
        assert caught.value.reason == "not_allowed"


def test_a_symlink_out_of_the_allowed_directory_is_refused(tmp_path: Path) -> None:
    media, outside = tmp_path / "media", tmp_path / "media-old"
    media.mkdir()
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(jpeg())
    os.symlink(outside / "secret.jpg", media / "link.jpg")
    with pytest.raises(InvalidImageError):
        ImageSource.from_path(media / "link.jpg", within=media)


def test_directories_fifos_and_oversized_files_are_refused(tmp_path: Path) -> None:
    (tmp_path / "dir.jpg").mkdir()
    with pytest.raises(InvalidImageError):
        ImageSource.from_path(tmp_path / "dir.jpg", within=tmp_path)
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "pipe.jpg")
        with pytest.raises(InvalidImageError):
            ImageSource.from_path(tmp_path / "pipe.jpg", within=tmp_path)
    (tmp_path / "big.jpg").write_bytes(b"\xff\xd8\xff" + b"\0" * 5000)
    with pytest.raises(InvalidImageError) as caught:
        ImageSource.from_path(tmp_path / "big.jpg", within=tmp_path, max_bytes=4096)
    assert caught.value.reason == "too_large"


def test_an_allowed_directory_is_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        ImageSource.from_path(tmp_path / "a.jpg", within=[])
    with pytest.raises(ConfigurationError):
        ImageSource.from_path(tmp_path / "a.jpg", within=tmp_path / "missing")


# -- framework objects ----------------------------------------------------------------------------------------------


class StarletteLikeUpload:
    def __init__(self, data: bytes, filename: str) -> None:
        self.file = io.BytesIO(data)
        self.filename = filename

    async def read(self) -> bytes:  # pragma: no cover - never called
        raise AssertionError("the async read must not be used")


class WerkzeugLikeStorage:
    def __init__(self, data: bytes, filename: str) -> None:
        self.stream = io.BytesIO(data)
        self.filename = filename


def test_framework_uploads_are_read_without_moving_their_position() -> None:
    data = jpeg()
    stream = io.BytesIO(data)
    stream.seek(10)
    assert ImageSource.of(stream, "a.jpg").data == data
    assert stream.tell() == 10

    assert ImageSource.of(StarletteLikeUpload(data, "upload.jpg")).name == "upload.jpg"
    assert ImageSource.of(WerkzeugLikeStorage(data, "form.jpg")).name == "form.jpg"
    assert ImageSource.of(bytearray(data)).name == "image"


def test_a_path_string_is_never_read_implicitly(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="from_path"):
        ImageSource.of(str(tmp_path / "a.jpg"))
    with pytest.raises(ConfigurationError, match="from_path"):
        ImageSource.of(tmp_path / "a.jpg")
    with pytest.raises(ConfigurationError):
        ImageSource.of(io.StringIO("text"))


def test_the_repr_never_shows_bytes() -> None:
    assert repr(ImageSource.from_bytes(jpeg(), "a.jpg")).startswith("ImageSource(name='a.jpg', bytes=")
    assert "data" not in repr(prepared(jpeg()))


def test_png_chunk_helper_builds_valid_chunks() -> None:
    assert png_chunk(b"IEND", b"")[-4:] == bytes.fromhex("ae426082")
