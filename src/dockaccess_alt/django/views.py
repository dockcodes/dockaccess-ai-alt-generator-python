"""The view dockaccess.org pushes alts edited in the account to.

::

    # urls.py
    from dockaccess_alt.django.views import alt_push_view

    def save_alts(pushed):
        updated = Photo.objects.filter(alt_image_uuid=pushed.image_uuid).update(alt=pushed.for_language("pl").text)
        return updated or None          # None: no local image matches (404)

    urlpatterns = [path("dockaccess/alts", alt_push_view(save_alts))]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..push import MAX_BODY, PushedAlts, PushError, handler_answer, parse_push
from . import get_alt_generator


def alt_push_view(handler: Callable[[PushedAlts], Any]) -> Callable[[HttpRequest], HttpResponse]:
    """A CSRF-exempt POST view (the request is authenticated by its signature instead). ``handler(pushed)``
    writes the alts and returns how many it wrote, or ``None`` when no local image matches."""

    @csrf_exempt
    @require_POST
    def receive(request: HttpRequest) -> HttpResponse:
        settings = get_alt_generator().config
        try:
            declared = request.META.get("CONTENT_LENGTH") or "0"
            if not declared.isdigit() or int(declared) > MAX_BODY:
                raise PushError(401, "The request could not be verified.")
            body = request.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise PushError(401, "The request could not be verified.")
            pushed = parse_push(
                body, dict(request.headers), secret=settings.connection_secret, site_token=settings.site_token
            )
            status, answer = handler_answer(handler(pushed), pushed)
        except PushError as error:
            status, answer = error.status, error.to_response()
        response = JsonResponse(answer, status=status)
        response["Cache-Control"] = "no-store"
        return response

    return receive
