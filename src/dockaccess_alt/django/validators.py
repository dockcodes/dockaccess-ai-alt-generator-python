"""A validator that checks an upload the way the generator will: by its content, not its name or MIME type.
Nothing is sent and no connection is needed.

::

    from dockaccess_alt.django.validators import validate_image

    class Product(models.Model):
        photo = models.ImageField(upload_to="products/", validators=[validate_image])
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

from ..errors import InvalidImageError
from ..image import ImagePolicy, ImageSource, prepare_image

MESSAGES = {
    InvalidImageError.NOT_ALLOWED: _("This file cannot be read."),
    InvalidImageError.EMPTY: _("The file is empty."),
    InvalidImageError.TOO_LARGE: _("The image is too large."),
    InvalidImageError.UNSUPPORTED_FORMAT: _("Upload a JPEG, PNG, GIF, WebP, AVIF or HEIC image."),
    InvalidImageError.MALFORMED: _("The image is damaged or cannot be read."),
    InvalidImageError.BAD_DIMENSIONS: _("The image dimensions are too large."),
    InvalidImageError.CANNOT_SANITIZE: _(
        "This image format cannot be processed here. Upload a JPEG, PNG or WebP image."
    ),
}


@deconstructible
class ImageValidator:
    """Raises ``ValidationError`` (code = the reason, e.g. ``unsupported_format``) for anything the generator
    would refuse. ``policy`` narrows what is accepted (formats, size, dimensions)."""

    def __init__(self, policy: ImagePolicy | None = None) -> None:
        self.policy = policy

    def __call__(self, value: Any) -> None:
        policy = self.policy or ImagePolicy()
        try:
            prepare_image(ImageSource.of(value, max_bytes=policy.max_bytes), policy)
        except InvalidImageError as error:
            raise ValidationError(
                MESSAGES.get(error.reason, MESSAGES[InvalidImageError.MALFORMED]), code=error.reason
            ) from None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ImageValidator) and self.policy == other.policy

    def __hash__(self) -> int:
        return hash((ImageValidator, self.policy))


validate_image = ImageValidator()
