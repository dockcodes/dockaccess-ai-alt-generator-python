"""Django helpers: the generator configured from ``settings.DOCKACCESS_ALT`` and the environment, a form and
model validator for uploads, and the view dockaccess.org pushes edited alts to.

::

    # settings.py
    INSTALLED_APPS = [..., "dockaccess_alt.django"]      # optional: Polish messages of the validator
    DOCKACCESS_ALT = {"LANGUAGE": "pl", "CONTEXT": "Handmade furniture shop"}   # optional

    # anywhere
    from dockaccess_alt.django import get_alt_generator

    result = get_alt_generator().generate(product.photo, context=product.name)
    product.photo_alt = result.text
"""

from __future__ import annotations

import os
import threading
from typing import Any

from ..client import AltGenerator
from ..config import Config

_lock = threading.Lock()
_generator: AltGenerator | None = None


def config_from_settings() -> Config:
    """``settings.DOCKACCESS_ALT`` - keys named like the environment variables without the prefix
    (``KEY``, ``LANGUAGE``, ``TONE``, ``VERIFY_TTL``...) - over the ``DOCKACCESS_ALT_*`` environment.
    The language defaults to ``settings.LANGUAGE_CODE``."""
    from django.conf import settings

    from ..language import try_normalize_language

    environ = dict(os.environ)
    if "DOCKACCESS_ALT_LANGUAGE" not in environ and try_normalize_language(
        getattr(settings, "LANGUAGE_CODE", "") or ""
    ):
        environ["DOCKACCESS_ALT_LANGUAGE"] = settings.LANGUAGE_CODE
    overrides: dict[str, Any] = getattr(settings, "DOCKACCESS_ALT", None) or {}
    environ.update(
        {f"DOCKACCESS_ALT_{key.upper()}": str(value) for key, value in overrides.items() if value is not None}
    )
    return Config.from_env(environ)


def get_alt_generator() -> AltGenerator:
    """The process-wide generator (thread-safe, reused across requests and Celery tasks)."""
    global _generator
    if _generator is None:
        with _lock:
            if _generator is None:
                _generator = AltGenerator(config_from_settings())
    return _generator


def reset_alt_generator() -> None:
    """Forget the generator, e.g. after changing settings in a test (done for you on ``setting_changed``)."""
    global _generator
    with _lock:
        if _generator is not None:
            _generator.close()
        _generator = None


def _on_setting_changed(*, setting: str, **_: Any) -> None:
    if setting in ("DOCKACCESS_ALT", "LANGUAGE_CODE"):
        reset_alt_generator()


try:  # connect only when Django is importable; the module is imported from Django projects
    from django.core.signals import setting_changed

    setting_changed.connect(_on_setting_changed, dispatch_uid="dockaccess_alt_setting_changed")
except ImportError:  # pragma: no cover
    pass

__all__ = ["config_from_settings", "get_alt_generator", "reset_alt_generator"]
