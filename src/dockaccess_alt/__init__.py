"""Alt text for your images from the dockaccess.org AI alt generator.

::

    from dockaccess_alt import AltGenerator

    generator = AltGenerator.from_env()          # DOCKACCESS_ALT_KEY, written by `dockaccess-alt connect`
    with open("photo.jpg", "rb") as photo:
        result = generator.generate(photo, language="pl")
    print(result.text)

:class:`AsyncAltGenerator` is the same for asyncio and trio (FastAPI, Starlette, Litestar, Quart).
"""

from ._version import __version__
from .client import AltGenerator, AsyncAltGenerator, BatchItem
from .config import DEFAULT_BASE_URL, DEFAULT_CONNECTION_URL, Config
from .connection import AsyncConnectionClient, ConnectionClient, ConnectRequest
from .errors import (
    AltGeneratorError,
    ApiError,
    AuthenticationError,
    ConfigurationError,
    GeneratorUnavailableError,
    ImageRejectedError,
    InvalidImageError,
    LimitReachedError,
    RateLimitedError,
    ServerError,
    SiteConnectionError,
    TransportError,
    UnexpectedResponseError,
)
from .image import FORMATS, ImagePolicy, ImageSource, PreparedImage, prepare_image
from .language import LANGUAGES, normalize_language, try_normalize_language
from .options import GenerateOptions
from .push import PushedAlts, PushError, parse_push, push_response
from .results import Alt, Connection, GenerationResult, Usage, Verification
from .secret import Secret
from .text import clean_text
from .tone import TONES, Tone

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CONNECTION_URL",
    "FORMATS",
    "LANGUAGES",
    "TONES",
    "Alt",
    "AltGenerator",
    "AltGeneratorError",
    "ApiError",
    "AsyncAltGenerator",
    "AsyncConnectionClient",
    "AuthenticationError",
    "BatchItem",
    "Config",
    "ConfigurationError",
    "ConnectRequest",
    "Connection",
    "ConnectionClient",
    "GenerateOptions",
    "GenerationResult",
    "GeneratorUnavailableError",
    "ImagePolicy",
    "ImageRejectedError",
    "ImageSource",
    "InvalidImageError",
    "LimitReachedError",
    "PreparedImage",
    "PushError",
    "PushedAlts",
    "RateLimitedError",
    "Secret",
    "ServerError",
    "SiteConnectionError",
    "Tone",
    "TransportError",
    "UnexpectedResponseError",
    "Usage",
    "Verification",
    "__version__",
    "clean_text",
    "normalize_language",
    "parse_push",
    "prepare_image",
    "push_response",
    "try_normalize_language",
]
