"""FastAPI, Django and Flask helpers, in real (test) applications.

No `from __future__ import annotations` here: FastAPI resolves the endpoints' annotations, imported locally.
"""

import io
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from dockaccess_alt import AltGeneratorError, PushedAlts
from dockaccess_alt.push import sign_push
from dockaccess_alt.testing import FakeAltApi

from .conftest import jpeg

SECRET = "push-secret-0123456789abcdef0123456789abcdef"
PUSH = {
    "token": "SiteTok3n",
    "image_uuid": "11111111-2222-4333-8444-555555555555",
    "alts": [{"alt": "Rudy kot", "locale": "pl"}],
}


def signed(payload: dict[str, Any] = PUSH) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, sign_push(body, SECRET)


# -- FastAPI ----------------------------------------------------------------------------------------------------------


def test_fastapi_lifespan_dependency_errors_and_push() -> None:
    from fastapi import FastAPI, UploadFile
    from fastapi.testclient import TestClient

    from dockaccess_alt.fastapi import (
        AltGeneratorDep,
        alt_generator_exception_handler,
        alt_generator_lifespan,
        alt_push_router,
        get_alt_generator,
    )

    fake = FakeAltApi("A ginger cat on a windowsill")
    saved: list[PushedAlts] = []

    async def save(pushed: PushedAlts) -> int | None:
        saved.append(pushed)
        return len(pushed.alts) if pushed.image_uuid == PUSH["image_uuid"] else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        async with alt_generator_lifespan(
            app, fake.config(site_token="SiteTok3n", connection_secret=SECRET), transport=fake.transport
        ):
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_exception_handler(AltGeneratorError, alt_generator_exception_handler)
    app.include_router(alt_push_router(save))

    @app.post("/images")
    async def upload(photo: UploadFile, generator: AltGeneratorDep) -> dict[str, str]:
        result = await generator.generate(photo, language="pl")
        return {"alt": result.text, "uuid": result.image_uuid}

    with TestClient(app) as client:
        answer = client.post("/images", files={"photo": ("cat.jpg", jpeg(), "image/jpeg")})
        assert answer.status_code == 200 and answer.json()["alt"] == "A ginger cat on a windowsill"

        refused = client.post("/images", files={"photo": ("cat.jpg", b"<svg onload=alert(1)>", "image/jpeg")})
        assert refused.status_code == 422 and refused.json()["reason"] == "unsupported_format"

        fake.limit_reached()
        spent = client.post("/images", files={"photo": ("cat.jpg", jpeg(), "image/jpeg")})
        assert spent.status_code == 429 and spent.json()["reason"] == "limit_reached"

        body, headers = signed()
        pushed = client.post("/dockaccess/alts", content=body, headers=headers)
        assert pushed.status_code == 200 and pushed.json() == {
            "success": True,
            "updated": 1,
            "image_uuid": PUSH["image_uuid"],
        }
        assert pushed.headers["cache-control"] == "no-store" and saved[0].alts[0].text == "Rudy kot"

        body, headers = signed({**PUSH, "image_uuid": "99999999-2222-4333-8444-555555555555"})
        assert client.post("/dockaccess/alts", content=body, headers=headers).status_code == 404
        assert (
            client.post(
                "/dockaccess/alts", content=body, headers={**headers, "X-Dockaccess-Signature": "sha256=00"}
            ).status_code
            == 401
        )
        assert client.post("/dockaccess/alts", content=b"x" * 70000, headers=headers).status_code == 401

    # Tests of an application swap the generator for the fake.
    plain = FastAPI()

    @plain.post("/images")
    async def plain_upload(photo: UploadFile, generator: AltGeneratorDep) -> dict[str, str]:
        return {"alt": (await generator.generate(photo)).text}

    other = FakeAltApi("Overridden")
    replacement = other.async_generator()
    plain.dependency_overrides[get_alt_generator] = lambda: replacement
    with TestClient(plain) as client:
        assert client.post("/images", files={"photo": ("a.jpg", jpeg(), "image/jpeg")}).json() == {"alt": "Overridden"}
    other.assert_generated(1)

    with TestClient(FastAPI()) as client:
        app_without = client.app
        app_without.post("/x")(plain_upload)  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="lifespan"):
            client.post("/x", files={"photo": ("a.jpg", jpeg(), "image/jpeg")})


# -- Flask ------------------------------------------------------------------------------------------------------------


def test_flask_extension_error_handler_and_push(monkeypatch: pytest.MonkeyPatch) -> None:
    from flask import Flask, request

    from dockaccess_alt.flask import (
        DockAccessAlt,
        alt_push_blueprint,
        config_from_flask,
        get_alt_generator,
        register_error_handler,
    )

    fake = FakeAltApi("A Flask photo")
    monkeypatch.delenv("DOCKACCESS_ALT_KEY", raising=False)
    app = Flask(__name__)
    app.config.update(
        DOCKACCESS_ALT_KEY="fake-account-key",
        DOCKACCESS_ALT_LANGUAGE="de",
        DOCKACCESS_ALT_SITE_TOKEN="SiteTok3n",
        DOCKACCESS_ALT_SECRET=SECRET,
        DOCKACCESS_ALT_VERIFY=False,
    )
    DockAccessAlt(app, transport=fake.transport)
    register_error_handler(app)
    app.register_blueprint(alt_push_blueprint(lambda pushed: 1))

    @app.post("/images")
    def upload() -> dict[str, str]:
        return {"alt": get_alt_generator().generate(request.files["photo"]).text}

    client = app.test_client()
    answer = client.post("/images", data={"photo": (io.BytesIO(jpeg()), "photo.jpg")})
    assert answer.status_code == 200 and answer.json == {"alt": "A Flask photo"}
    assert fake.calls("/api/generate")[0].url.params["lang"] == "de"
    refused = client.post("/images", data={"photo": (io.BytesIO(b"GIF89a<html>"), "photo.gif")})
    assert refused.status_code == 422 and refused.json["reason"] == "malformed"

    body, headers = signed()
    assert client.post("/dockaccess/alts", data=body, headers=headers).json == {
        "success": True,
        "updated": 1,
        "image_uuid": PUSH["image_uuid"],
    }
    assert (
        client.post("/dockaccess/alts", data=body, headers={**headers, "X-Dockaccess-Timestamp": "1"}).status_code
        == 401
    )

    with app.app_context():
        assert config_from_flask(app).defaults.language == "de"


# -- Django -----------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def django_settings() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            SECRET_KEY="test",
            ROOT_URLCONF=__name__,
            ALLOWED_HOSTS=["testserver"],
            INSTALLED_APPS=["django.contrib.contenttypes", "dockaccess_alt.django"],
            MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"],
            USE_I18N=True,
            LANGUAGE_CODE="pl",
            DOCKACCESS_ALT={"KEY": "fake-account-key", "SITE_TOKEN": "SiteTok3n", "SECRET": SECRET, "VERIFY": False},
        )
        django.setup()


urlpatterns: list[Any] = []


def test_django_settings_validator_and_push_view(django_settings: None) -> None:
    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client, override_settings
    from django.urls import path
    from django.utils import translation

    import dockaccess_alt.django as integration
    from dockaccess_alt.django.validators import validate_image
    from dockaccess_alt.django.views import alt_push_view

    generator = integration.get_alt_generator()
    assert generator is integration.get_alt_generator()
    assert generator.config.defaults.language == "pl", "the language follows LANGUAGE_CODE"
    assert generator.config.site_token == "SiteTok3n"

    validate_image(SimpleUploadedFile("cat.jpg", jpeg(), "image/jpeg"))
    with translation.override("pl"), pytest.raises(ValidationError) as caught:
        validate_image(SimpleUploadedFile("cat.jpg", b"<?php ?>", "image/jpeg"))
    assert caught.value.code == "unsupported_format"
    assert caught.value.messages == ["Prześlij obraz JPEG, PNG, GIF, WebP, AVIF lub HEIC."]
    with translation.override("en"):
        with pytest.raises(ValidationError) as caught:
            validate_image(SimpleUploadedFile("cat.jpg", b"", "image/jpeg"))
        assert caught.value.messages == ["The file is empty."]

    written: list[str] = []

    def save(pushed: PushedAlts) -> int:
        written.append(pushed.for_language("pl").text)
        return 1

    urlpatterns[:] = [path("dockaccess/alts", alt_push_view(save))]
    client = Client(enforce_csrf_checks=True)
    body, headers = signed()
    answer = client.post("/dockaccess/alts", data=body, content_type="application/json", headers=headers)
    assert answer.status_code == 200 and answer.json()["updated"] == 1 and written == ["Rudy kot"]
    assert answer["Cache-Control"] == "no-store"
    assert client.post("/dockaccess/alts", data=body, content_type="application/json").status_code == 401
    assert client.get("/dockaccess/alts").status_code == 405

    with override_settings(DOCKACCESS_ALT={"KEY": "other-key", "VERIFY": False, "LANGUAGE": "en"}):
        assert integration.get_alt_generator().config.defaults.language == "en"
    assert integration.get_alt_generator().config.defaults.language == "pl"
