"""Turning a file into the image that may leave the server, or refusing it.

1. The format is identified from the content (magic bytes) - never from the name or a client's MIME type.
   SVG is never accepted: it is a document that can carry scripts.
2. The dimensions are read from the header and checked before anything is decoded (decompression bombs).
3. Pillow decodes the image with only the plugin of the identified format, the EXIF orientation is applied,
   the image is scaled down to the policy's longest side and a brand new image is built from the pixels.
4. That image is encoded as JPEG (opaque) or WebP (transparent): no EXIF, GPS, XMP, IPTC, ICC profile,
   comment, thumbnail or anything appended after the end of the file can travel with it.

Nothing is written to disk and nothing is cached.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import os
import re
import stat
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from PIL import Image, ImageOps

from .errors import ConfigurationError, InvalidImageError

JPEG = "jpeg"
PNG = "png"
GIF = "gif"
WEBP = "webp"
AVIF = "avif"
HEIC = "heic"

#: Formats an image may arrive in.
FORMATS: tuple[str, ...] = (JPEG, PNG, GIF, WEBP, AVIF, HEIC)

#: The API refuses larger uploads.
API_MAX_BYTES = 10 * 1024 * 1024

#: File extensions a directory walk picks up (a named file is always tried, whatever its name).
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".avif", ".heic", ".heif"})

_PILLOW = {JPEG: "JPEG", PNG: "PNG", GIF: "GIF", WEBP: "WEBP", AVIF: "AVIF", HEIC: "HEIF"}
_EXTENSION = {JPEG: "jpg", PNG: "png", GIF: "gif", WEBP: "webp", AVIF: "avif", HEIC: "heic"}
_MIME = {
    JPEG: "image/jpeg",
    PNG: "image/png",
    GIF: "image/gif",
    WEBP: "image/webp",
    AVIF: "image/avif",
    HEIC: "image/heic",
}
_AVIF_BRANDS = {b"avif", b"avis"}
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(//|[^\\/])")
_PLUGINS = threading.Lock()


@dataclass(frozen=True)
class ImagePolicy:
    """What an image must be before it is sent, and how it is rebuilt. Immutable.

    - ``max_bytes`` - files above it are refused before they are read whole (1 KiB to the API's 10 MiB),
    - ``max_dimension`` and ``max_pixels`` - larger images are refused before decoding,
    - ``max_edge`` - the longest side of the image sent (256 to 8192); the generator needs no more,
      and less leaves the server,
    - ``quality`` - JPEG and WebP quality (50 to 95),
    - ``formats`` - the accepted input formats, a subset of :data:`FORMATS`.
    """

    max_bytes: int = API_MAX_BYTES
    max_dimension: int = 20000
    max_pixels: int = 50_000_000
    max_edge: int = 2048
    quality: int = 85
    formats: frozenset[str] = field(default_factory=lambda: frozenset(FORMATS))

    def __post_init__(self) -> None:
        if not 1024 <= self.max_bytes <= API_MAX_BYTES:
            raise ConfigurationError("The size limit must be between 1 KiB and 10 MiB.")
        if not 64 <= self.max_dimension <= 65535 or not 4096 <= self.max_pixels <= 268_435_456:
            raise ConfigurationError("Pixel limits must satisfy 64 <= side <= 65535 and 4096 <= total <= 268435456.")
        if not 256 <= self.max_edge <= 8192:
            raise ConfigurationError("The longest side must be between 256 and 8192 pixels.")
        if not 50 <= self.quality <= 95:
            raise ConfigurationError("The quality must be between 50 and 95.")
        formats = frozenset(self.formats)
        if not formats or not formats <= set(FORMATS):
            raise ConfigurationError(f"Formats must be a non-empty subset of {', '.join(FORMATS)}.")
        object.__setattr__(self, "formats", formats)


@dataclass(frozen=True)
class PreparedImage:
    """The image exactly as it is (or would be) uploaded."""

    data: bytes = field(repr=False)
    format: str
    width: int
    height: int
    name: str
    source_format: str
    source_size: int

    @property
    def mime_type(self) -> str:
        return _MIME[self.format]

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "size": self.size,
            "sha256": self.sha256,
            "source_format": self.source_format,
            "source_size": self.source_size,
        }


class ImageSource:
    """The bytes of an image (or a decoded Pillow image) and the name it is shown under.

    Reading is where most leaks start - a path from a request that points at ``settings.py``, a symlink out
    of the media folder - so a path is only read with :meth:`from_path`, inside directories you name.
    Nothing here decides whether the bytes are an image; :func:`prepare_image` does, from the content.
    """

    __slots__ = ("_data", "_image", "_name")

    def __init__(self, data: bytes | None, name: str, image: Image.Image | None = None) -> None:
        self._data = data
        self._name = name
        self._image = image

    @classmethod
    def from_bytes(
        cls, data: bytes | bytearray | memoryview, name: str = "image", *, max_bytes: int = API_MAX_BYTES
    ) -> ImageSource:
        """Bytes you already hold: a download, an object from S3, a framework's upload."""
        data = bytes(data)
        if not data:
            raise InvalidImageError(InvalidImageError.EMPTY, "The image is empty.")
        if len(data) > max_bytes:
            raise InvalidImageError(InvalidImageError.TOO_LARGE, "The image is larger than allowed.")
        return cls(data, _basename(name))

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        *,
        within: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        max_bytes: int = API_MAX_BYTES,
    ) -> ImageSource:
        """A file that must lie inside ``within`` (a directory or several) once symlinks are resolved.

        Name the narrowest directory that holds images - ``settings.MEDIA_ROOT``, the uploads folder - never
        the project root. URLs, ``..`` escapes, symlinks pointing outside and anything but a regular file are
        refused, and the file is read at most up to ``max_bytes``.
        """
        roots = _roots(within)
        raw = os.fspath(path) if isinstance(path, (str, os.PathLike)) else None
        if not isinstance(raw, str):
            raise ConfigurationError("The path must be a string or a path-like object.")
        if raw == "" or "\0" in raw or _URL.match(raw):
            raise InvalidImageError(InvalidImageError.NOT_ALLOWED, "Only local files are read; URLs are refused.")

        real = os.path.realpath(raw)
        if not any(_inside(real, root) for root in roots):
            raise InvalidImageError(
                InvalidImageError.NOT_ALLOWED, "The file does not exist or lies outside the allowed directories."
            )

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(real, flags)
        except OSError:
            raise InvalidImageError(InvalidImageError.NOT_ALLOWED, "The file cannot be opened.") from None
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            os.close(fd)
            raise InvalidImageError(InvalidImageError.NOT_ALLOWED, "Only regular files are read.")
        with os.fdopen(fd, "rb") as handle:
            try:
                checked = os.stat(real)
            except OSError:
                checked = None
            if checked is None or (checked.st_dev, checked.st_ino) != (opened.st_dev, opened.st_ino):
                raise InvalidImageError(InvalidImageError.NOT_ALLOWED, "The file changed while it was opened.")
            if opened.st_size > max_bytes:
                raise InvalidImageError(InvalidImageError.TOO_LARGE, "The image is larger than allowed.")
            data = handle.read(max_bytes + 1)

        if not data:
            raise InvalidImageError(InvalidImageError.EMPTY, "The image is empty.")
        if len(data) > max_bytes:
            raise InvalidImageError(InvalidImageError.TOO_LARGE, "The image is larger than allowed.")
        return cls(data, _basename(raw))

    @classmethod
    def from_file(cls, file: IO[bytes], name: str | None = None, *, max_bytes: int = API_MAX_BYTES) -> ImageSource:
        """An open binary file: ``open(..., "rb")``, ``io.BytesIO``, a framework's upload stream.

        A seekable file is read from its start and left where it was, so it can still be saved afterwards.
        """
        read = getattr(file, "read", None)
        if not callable(read) or inspect.iscoroutinefunction(read):
            raise ConfigurationError("The file must have a synchronous read(); await an upload's read() first.")
        position = None
        try:
            if file.seekable():
                position = file.tell()
                file.seek(0)
        except (AttributeError, OSError, ValueError):
            position = None
        try:
            data = read(max_bytes + 1)
        finally:
            if position is not None:
                file.seek(position)
        if isinstance(data, str):
            raise ConfigurationError("The file is open in text mode; open it with 'rb'.")
        if name is None:
            given = getattr(file, "name", None)
            name = given if isinstance(given, str) else "image"
        return cls.from_bytes(bytes(data or b""), name, max_bytes=max_bytes)

    @classmethod
    def from_pil(cls, image: Image.Image, name: str = "image") -> ImageSource:
        """An image already decoded by Pillow (a pipeline step, a thumbnail you made). It is rebuilt from its
        pixels like any other."""
        if not isinstance(image, Image.Image):
            raise ConfigurationError("from_pil() takes a PIL.Image.Image.")
        return cls(None, _basename(name), image)

    @classmethod
    def of(cls, value: ImageLike, name: str | None = None, *, max_bytes: int = API_MAX_BYTES) -> ImageSource:
        """Whatever a framework hands over: bytes, a binary file, a Pillow image, FastAPI's/Starlette's
        ``UploadFile``, Django's ``UploadedFile``/``File``, Werkzeug's ``FileStorage``.

        A path (``str`` or ``Path``) is refused on purpose - read it with :meth:`from_path` and a directory.
        """
        if isinstance(value, ImageSource):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return cls.from_bytes(value, name or "image", max_bytes=max_bytes)
        if isinstance(value, Image.Image):
            return cls.from_pil(value, name or "image")
        if isinstance(value, (str, os.PathLike)):
            raise ConfigurationError(
                "A path is read only with ImageSource.from_path(path, within=<directory>), "
                "so a path from a request cannot point anywhere."
            )

        filename = getattr(value, "filename", None)
        label = name or (filename if isinstance(filename, str) else None)
        read = getattr(value, "read", None)
        if callable(read) and not inspect.iscoroutinefunction(read):
            return cls.from_file(value, label or _attribute_name(value), max_bytes=max_bytes)
        for attribute in ("file", "stream"):
            inner = getattr(value, attribute, None)
            if inner is not None and callable(getattr(inner, "read", None)):
                return cls.from_file(inner, label or _attribute_name(value), max_bytes=max_bytes)
        raise ConfigurationError(f"Cannot read an image from {type(value).__name__}.")

    @property
    def name(self) -> str:
        """The original base name, untrusted; the upload name is derived from it safely."""
        return self._name

    @property
    def data(self) -> bytes | None:
        """The original bytes (None for a Pillow image)."""
        return self._data

    def sha256(self) -> str | None:
        """SHA-256 of the original bytes - to recognise an unchanged file in a pipeline."""
        return hashlib.sha256(self._data).hexdigest() if self._data is not None else None

    def __repr__(self) -> str:
        size = len(self._data) if self._data is not None else "pil"
        return f"ImageSource(name={self._name!r}, bytes={size})"


#: What generate() and prepare() take: an ImageSource, bytes, a binary file, a Pillow image, or a framework's
#: upload object (FastAPI/Starlette UploadFile, Django UploadedFile/File, Werkzeug FileStorage).
ImageLike = Any


def sniff(data: bytes) -> str | None:
    """The format of the bytes, from their signature alone."""
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return GIF
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    if data[4:8] == b"ftyp" and len(data) >= 16:
        size = int.from_bytes(data[0:4], "big")
        end = min(size if 16 <= size <= 4096 else 16, len(data))
        brands = {data[8:12]} | {data[i : i + 4] for i in range(16, end - 3, 4)}
        if brands & _AVIF_BRANDS:
            return AVIF
        if brands & _HEIC_BRANDS:
            return HEIC
    return None


def upload_name(name: str, format: str) -> str:
    """The original base name made harmless: ASCII letters, digits, dashes and underscores, no path, no
    second extension, and the extension of the format actually sent (``shell.php.png`` -> ``shell-php.jpg``).
    The API shows it in the account and uses it with the bytes to recognise a re-upload."""
    base = re.split(r"[/\\]", name)[-1]
    dot = base.rfind(".")
    stem = base if dot <= 0 else base[:dot]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem)
    stem = re.sub(r"-{2,}", "-", stem).strip("-_")[:100]
    return f"{stem or 'image'}.{_EXTENSION[format]}"


def prepare_image(source: ImageSource, policy: ImagePolicy | None = None) -> PreparedImage:
    """Verifies the image and rebuilds it from its pixels, or raises :class:`InvalidImageError`."""
    policy = policy or ImagePolicy()

    if source.data is None:
        image = source._image
        assert image is not None
        _check_dimensions(image.size, policy)
        return _encode(image, policy, source.name, (image.format or "pil").lower(), 0)

    data = source.data
    if not data:
        raise InvalidImageError(InvalidImageError.EMPTY, "The image is empty.")
    if len(data) > policy.max_bytes:
        raise InvalidImageError(InvalidImageError.TOO_LARGE, "The image is larger than allowed.")

    format = sniff(data)
    if format is None:
        raise InvalidImageError(
            InvalidImageError.UNSUPPORTED_FORMAT, "This is not a JPEG, PNG, GIF, WebP, AVIF or HEIC image."
        )
    if format not in policy.formats:
        raise InvalidImageError(InvalidImageError.UNSUPPORTED_FORMAT, f"{format.upper()} images are not accepted here.")
    if not _can_open(format):
        raise InvalidImageError(
            InvalidImageError.CANNOT_SANITIZE,
            f"Pillow cannot decode {format.upper()} here. Install dockaccess-alt-generator[heif] "
            "or convert the image to JPEG, PNG or WebP.",
        )

    try:
        with Image.open(io.BytesIO(data), formats=[_PILLOW[format]]) as opened:
            _check_dimensions(opened.size, policy)
            return _encode(opened, policy, source.name, format, len(data))
    except InvalidImageError:
        raise
    except Image.DecompressionBombError:
        raise InvalidImageError(InvalidImageError.BAD_DIMENSIONS, "The image declares too many pixels.") from None
    except Exception:
        # Whatever Pillow cannot decode is not sent; its message may quote the file, so it is not passed on.
        raise InvalidImageError(InvalidImageError.MALFORMED, "The image is damaged or cannot be decoded.") from None


def _encode(image: Image.Image, policy: ImagePolicy, name: str, source_format: str, source_size: int) -> PreparedImage:
    given = image
    image.load()
    try:
        upright = ImageOps.exif_transpose(image)
        image = upright if upright is not None else image
    except Exception:  # noqa: S110 - a broken orientation tag does not make the pixels unusable
        pass

    alpha = image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info
    if alpha:
        image = image.convert("RGBA")
        low, _ = image.getchannel("A").getextrema()
        if low == 255:
            image, alpha = image.convert("RGB"), False
    elif image.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N"):
        image = image.convert("I").point(lambda value: value * (1 / 256)).convert("L")
    elif image.mode == "1":
        image = image.convert("L")
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if image is given:
        image = image.copy()  # thumbnail() works in place; the caller's image stays as it was
    image.thumbnail((policy.max_edge, policy.max_edge), Image.Resampling.LANCZOS)

    # A brand new image from the pixels: no info, EXIF, ICC profile, palette comments or text chunks.
    clean = Image.frombytes(image.mode, image.size, image.tobytes())
    out = io.BytesIO()
    if alpha:
        clean.save(out, "WEBP", quality=policy.quality, method=4)
        format = WEBP
    else:
        clean.save(out, "JPEG", quality=policy.quality, optimize=True)
        format = JPEG
    data = out.getvalue()

    if len(data) > policy.max_bytes:
        raise InvalidImageError(InvalidImageError.TOO_LARGE, "The rebuilt image is larger than allowed.")
    with Image.open(io.BytesIO(data), formats=[_PILLOW[format]]) as check:
        check.load()
        width, height = check.size

    return PreparedImage(
        data=data,
        format=format,
        width=width,
        height=height,
        name=upload_name(name, format),
        source_format=source_format,
        source_size=source_size,
    )


def _check_dimensions(size: tuple[int, int], policy: ImagePolicy) -> None:
    width, height = size
    if width < 1 or height < 1 or width > policy.max_dimension or height > policy.max_dimension:
        raise InvalidImageError(InvalidImageError.BAD_DIMENSIONS, "The image dimensions are not acceptable.")
    if width * height > policy.max_pixels:
        raise InvalidImageError(InvalidImageError.BAD_DIMENSIONS, "The image has more pixels than allowed.")


def _can_open(format: str) -> bool:
    Image.init()
    if _PILLOW[format] in Image.OPEN:
        return True
    with _PLUGINS:
        try:
            import pillow_heif
        except ImportError:
            return False
        if format == HEIC:
            pillow_heif.register_heif_opener()
        elif hasattr(pillow_heif, "register_avif_opener"):
            pillow_heif.register_avif_opener()
    return _PILLOW[format] in Image.OPEN


def _roots(within: str | os.PathLike[str] | Iterable[str | os.PathLike[str]]) -> list[str]:
    items = [within] if isinstance(within, (str, os.PathLike)) else list(within)
    roots = []
    for item in items:
        if not isinstance(item, (str, os.PathLike)) or os.fspath(item) == "":
            raise ConfigurationError("Allowed directories must be non-empty paths.")
        real = os.path.realpath(os.fspath(item))
        if not os.path.isdir(real):
            raise ConfigurationError("An allowed directory does not exist.")
        roots.append(real)
    if not roots:
        raise ConfigurationError("Name at least one directory images may be read from.")
    return roots


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root)
    except ValueError:
        return False


def _basename(name: str) -> str:
    base = re.split(r"[/\\]", str(name))[-1]
    return base or "image"


def _attribute_name(value: object) -> str | None:
    given = getattr(value, "name", None)
    return Path(given).name if isinstance(given, str) and given else None
