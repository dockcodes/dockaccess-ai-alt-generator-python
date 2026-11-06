"""``dockaccess-alt``: connect an application, check it and generate alt texts for files - in a terminal,
a CI job or a data pipeline.

Exit codes: 0 done, 1 some images failed, 2 a usage or configuration error, 3 not connected or the
generator is off, 4 the plan's limit is spent, 5 dockaccess.org could not be reached or failed.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from ._version import __version__
from .client import AltGenerator, BatchItem
from .config import Config
from .connection import ConnectionClient, ConnectRequest
from .envfile import update_env_file
from .errors import (
    AltGeneratorError,
    AuthenticationError,
    ConfigurationError,
    GeneratorUnavailableError,
    InvalidImageError,
    LimitReachedError,
    ServerError,
    SiteConnectionError,
    TransportError,
    UnexpectedResponseError,
)
from .image import IMAGE_EXTENSIONS, ImageSource
from .tone import TONES

OK, FAILED, USAGE, UNAVAILABLE, LIMIT, NETWORK = 0, 1, 2, 3, 4, 5

ENV_NAMES = ("DOCKACCESS_ALT_KEY", "DOCKACCESS_ALT_SITE_TOKEN", "DOCKACCESS_ALT_SECRET", "DOCKACCESS_ALT_HOME_URL")
MANIFEST_VERSION = 1

_EXPLAIN = {
    "invalid_credentials": "The e-mail address or the password of the dockaccess.org account is wrong. "
    "Reset the password at https://dockaccess.org/my-account/lost-password/ if needed.",
    "email_not_verified": "Confirm the e-mail address of the dockaccess.org account first (see the message we sent).",
    "token_not_owned": "This site token belongs to another dockaccess.org account. "
    "Sign in with the account that owns the site.",
    "domain_not_allowed": "The application address is not one of the site's domains. Add the domain to the site in "
    "the account, or connect with --url set to the site's public address.",
    "alt_disabled": "The alt generator is switched off for this site. "
    "Switch it on in the account: My sites, the site, ALT generator.",
    "alt_paused": "The plan paused the alt generator for this site. Upgrade the plan or free up a site in the account.",
    "already_connected": "Another installation is connected at this address. Run again with --overwrite to replace it "
    "(the other installation stops working).",
    "disconnected": "The application was disconnected in the account. Connect it again: dockaccess-alt connect",
    "not_connected": "dockaccess.org has no connection for this address.",
    "too_many_attempts": "Too many failed attempts. Wait 15 minutes and try again.",
}


# -- entry point --------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return USAGE
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        _err("Interrupted.")
        return 130
    except ConfigurationError as error:
        _err(f"error: {error}")
        return USAGE
    except LimitReachedError as error:
        _err(f"error: {error}")
        return LIMIT
    except (AuthenticationError, GeneratorUnavailableError, SiteConnectionError) as error:
        _err("error: " + _explain(error))
        return UNAVAILABLE
    except (TransportError, ServerError, UnexpectedResponseError) as error:
        _err(f"error: {error}")
        return NETWORK
    except AltGeneratorError as error:
        _err(f"error: {error}")
        return FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dockaccess-alt",
        description="Alt texts for images from the dockaccess.org AI alt generator.",
        epilog="Settings come from DOCKACCESS_ALT_* environment variables and the --env-file (default: .env).",
    )
    parser.add_argument("--version", action="version", version=f"dockaccess-alt {__version__}")
    commands = parser.add_subparsers(title="commands", metavar="<command>")

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.add_argument("--env-file", default=".env", help="the dotenv file to read and write (default: .env)")
        return sub

    connect = command("connect", "connect this application to a dockaccess.org account and store the keys")
    connect.add_argument("--token", help="the site token (account: My sites, the site, ALT generator)")
    connect.add_argument("--login", help="the e-mail address of the dockaccess.org account")
    connect.add_argument("--url", help="the application's public address, one of the site's domains")
    connect.add_argument("--system", default="python", help="what the account shows (default: python)")
    connect.add_argument("--callback-url", help="where dockaccess.org may push alts edited in the account")
    connect.add_argument("--overwrite", action="store_true", help="replace another installation's connection")
    connect.add_argument("--show", action="store_true", help="print the values instead of writing the env file")
    connect.add_argument(
        "--no-input", action="store_true", help="never prompt (the password comes from DOCKACCESS_PASSWORD)"
    )
    connect.set_defaults(handler=_connect)

    status = command("status", "show the connection, the generator's state and the usage")
    status.add_argument("--json", action="store_true", help="print JSON")
    status.set_defaults(handler=_status)

    generate = command("generate", "generate alt texts for image files and directories")
    _paths(generate)
    generate.add_argument("--lang", "--language", dest="language", help="the language, a code or a locale (pl, de_AT)")
    generate.add_argument("--tone", choices=TONES, metavar="TONE", help="the tone (neutral, descriptive, ...)")
    generate.add_argument("--context", help="a hint about the images, up to 500 characters")
    generate.add_argument("--variants", type=int, choices=(1, 2, 3), help="alternatives per image (1 to 3)")
    generate.add_argument("--set-ref", help="the image set in the account (default: the site token)")
    generate.add_argument("--manifest", help="a JSON file of generated alts; unchanged images are not generated again")
    generate.add_argument("--force", action="store_true", help="generate even images the manifest already has")
    generate.add_argument("--max", type=int, metavar="N", help="generate at most N images in this run")
    generate.add_argument("--dry-run", action="store_true", help="verify and rebuild the images, send nothing")
    generate.set_defaults(handler=_generate)

    check = command("check", "verify image files locally, the way they would be sent (no key needed)")
    _paths(check)
    check.set_defaults(handler=_check)

    disconnect = command("disconnect", "disconnect this application and remove the keys from the env file")
    disconnect.add_argument("--login", help="the e-mail address of the dockaccess.org account")
    disconnect.add_argument("--url", help="the address the application was connected with")
    disconnect.add_argument("--keep-env", action="store_true", help="keep the keys in the env file")
    disconnect.add_argument(
        "--no-input", action="store_true", help="never prompt (the password comes from DOCKACCESS_PASSWORD)"
    )
    disconnect.set_defaults(handler=_disconnect)
    return parser


def _paths(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("paths", nargs="+", metavar="PATH", help="image files or directories")
    sub.add_argument("-r", "--recursive", action="store_true", help="include subdirectories")
    sub.add_argument("--format", choices=("text", "jsonl"), default="text", help="output: text (default) or JSON Lines")


# -- connect, status, disconnect ------------------------------------------------------------------------------------


def _connect(args: argparse.Namespace) -> int:
    env = _environment(args.env_file)
    interactive = not args.no_input and sys.stdin.isatty()
    token = args.token or env.get("DOCKACCESS_ALT_SITE_TOKEN") or _ask("Site token", interactive)
    login = args.login or env.get("DOCKACCESS_LOGIN") or _ask("E-mail of the dockaccess.org account", interactive)
    url = (
        args.url
        or env.get("DOCKACCESS_ALT_HOME_URL")
        or _ask("Public address of the application (https://...)", interactive)
    )
    password = env.get("DOCKACCESS_PASSWORD") or _ask_password(interactive)
    config = Config.from_env(env)
    request = ConnectRequest(
        site_token=token, login=login, password=password, home_url=url, system=args.system,
        callback_url=args.callback_url, overwrite=args.overwrite,
    )  # fmt: skip

    with ConnectionClient(config) as client:
        try:
            connection = client.connect(request)
        except SiteConnectionError as error:
            if error.reason != SiteConnectionError.ALREADY_CONNECTED or not interactive:
                raise
            if input("Another installation is connected at this address. Replace it? [y/N] ").strip().lower() not in (
                "y",
                "yes",
            ):
                return UNAVAILABLE
            connection = client.connect(dataclasses.replace(request, overwrite=True))

    values = {**connection.environment(), "DOCKACCESS_ALT_HOME_URL": connection.home_url}
    if args.show:
        for name, value in values.items():
            print(f"{name}={value}")
    else:
        update_env_file(args.env_file, values)
        _err(f"Saved DOCKACCESS_ALT_KEY, DOCKACCESS_ALT_SITE_TOKEN and DOCKACCESS_ALT_SECRET to {args.env_file}.")
        _err(
            "Load that file in your application (python-dotenv, pydantic-settings, docker --env-file) "
            "or move the values to your secrets manager."
        )
    who = f" ({connection.account_name})" if connection.account_name else ""
    _err(f"Connected {connection.system} at {connection.home_url} to the dockaccess.org account{who}.")
    if not connection.domain_verified:
        _err(
            "Note: a local address is not one of the site's domains. "
            "Connect again with the public address before going live."
        )
    if not connection.alt_enabled:
        _err(
            "Note: the alt generator is off for this site. "
            "Switch it on in the account: My sites, the site, ALT generator."
        )
    return OK


def _status(args: argparse.Namespace) -> int:
    config = Config.from_env(_environment(args.env_file))
    report: dict[str, Any] = {
        "connected": config.account_key is not None,
        "site_token": _mask(config.site_token),
        "connection_check": bool(config.site_token and config.connection_secret),
    }
    code = OK
    if not report["connected"]:
        report["state"] = "not_connected"
        code = UNAVAILABLE
    else:
        with AltGenerator(config) as generator:
            if report["connection_check"]:
                try:
                    verification = generator.verify(with_usage=True)
                    report["state"] = verification.state
                    report["system"] = verification.system
                    if verification.websites_limit is not None:
                        report["sites"] = {"used": verification.websites_used, "limit": verification.websites_limit}
                    code = OK if verification.is_active else UNAVAILABLE
                except SiteConnectionError as error:
                    report["state"] = "disconnected" if error.is_connection_gone else error.reason
                    report["message"] = _explain(error)
                    code = UNAVAILABLE
            try:
                report["usage"] = generator.usage().to_dict()
            except AuthenticationError as error:
                report["state"], report["message"], code = "invalid_key", _explain(error), UNAVAILABLE
    if args.json:
        print(json.dumps(report, indent=2))
        return code

    print(f"Connected:   {'yes' if report['connected'] else 'no - run: dockaccess-alt connect'}")
    if report["site_token"]:
        print(f"Site token:  {report['site_token']}")
    if "state" in report:
        print(f"Generator:   {report['state']}")
    if "message" in report:
        print(f"             {report['message']}")
    if "sites" in report:
        print(f"Sites:       {report['sites']['used']} of {report['sites']['limit']}")
    usage = report.get("usage")
    if usage:
        window = "this month" if usage["period"] == "month" else "today (UTC)"
        resets = f", renews {usage['resets_at']}" if usage["resets_at"] else ""
        print(f"Usage:       {usage['used']} of {usage['limit']} {window}, {usage['remaining']} left{resets}")
    return code


def _disconnect(args: argparse.Namespace) -> int:
    env = _environment(args.env_file)
    config = Config.from_env(env)
    if not config.site_token:
        raise ConfigurationError("No site token is configured; nothing to disconnect.")
    interactive = not args.no_input and sys.stdin.isatty()
    login = args.login or env.get("DOCKACCESS_LOGIN") or _ask("E-mail of the dockaccess.org account", interactive)
    url = (
        args.url
        or env.get("DOCKACCESS_ALT_HOME_URL")
        or _ask("The address the application was connected with", interactive)
    )
    password = env.get("DOCKACCESS_PASSWORD") or _ask_password(interactive)
    with ConnectionClient(config) as client:
        try:
            client.disconnect(config.site_token, login, password, url)
        except SiteConnectionError as error:
            if error.reason != SiteConnectionError.NOT_CONNECTED:
                raise
    if not args.keep_env and os.path.exists(args.env_file):
        update_env_file(args.env_file, dict.fromkeys(ENV_NAMES))
    _err("Disconnected." + ("" if args.keep_env else f" The keys were removed from {args.env_file}."))
    return OK


# -- generate and check ---------------------------------------------------------------------------------------------


def _generate(args: argparse.Namespace) -> int:
    if args.max is not None and args.max < 1:
        raise ConfigurationError("--max must be at least 1.")
    config = Config.from_env(_environment(args.env_file))
    options = config.default_options().replace(
        language=args.language, tone=args.tone, context=args.context, variants=args.variants, set_ref=args.set_ref
    )
    if not args.dry_run:
        config.require_account_key()
    manifest_path = Path(args.manifest) if args.manifest else None
    manifest = _load_manifest(manifest_path)
    base = manifest_path.resolve().parent if manifest_path else Path.cwd()
    out = _Output(args.format)
    counts = {"generated": 0, "cached": 0, "failed": 0, "ready": 0}
    files = list(_files(args.paths, args.recursive))
    if not files:
        raise ConfigurationError("No image files found.")

    with AltGenerator(config) as generator:

        def queue() -> Iterator[tuple[tuple[str, str], ImageSource]]:
            sent = 0
            for path, root in files:
                key = _key(path, base)
                try:
                    source = ImageSource.from_path(path, within=root, max_bytes=config.image_policy.max_bytes)
                except InvalidImageError as error:
                    counts["failed"] += 1
                    out.record({"path": key, "status": "failed", "error": _error(error)})
                    continue
                digest = source.sha256() or ""
                known = manifest["images"].get(key, {})
                entry = known.get("languages", {}).get(options.language) if known.get("sha256") == digest else None
                if entry and not args.force:
                    counts["cached"] += 1
                    out.record(
                        {"path": key, "status": "cached", "sha256": digest, "language": options.language, **entry}
                    )
                    continue
                if args.max is not None and sent >= args.max:
                    continue
                sent += 1
                yield (key, digest), source

        if args.dry_run:
            for (key, digest), source in queue():
                try:
                    prepared = generator.prepare(source)
                except InvalidImageError as error:
                    counts["failed"] += 1
                    out.record({"path": key, "status": "failed", "sha256": digest, "error": _error(error)})
                    continue
                counts["ready"] += 1
                out.record({"path": key, "status": "ready", "sha256": digest, "image": prepared.to_dict()})
            out.summary(counts, pending=len(files) - sum(counts.values()))
            return FAILED if counts["failed"] else OK

        stopped: BatchItem | None = None
        for item in generator.generate_many(queue(), options):
            key, digest = item.key
            if item.result is None:
                counts["failed"] += 1
                out.record({"path": key, "status": "failed", "sha256": digest, "error": _error(item.error)})
                if item.stopped:
                    stopped = item
                continue
            counts["generated"] += 1
            entry = _entry(item)
            if manifest_path is not None:
                images = manifest["images"]
                known = images.get(key, {})
                languages = known.get("languages", {}) if known.get("sha256") == digest else {}
                images[key] = {"sha256": digest, "languages": {**languages, item.result.language: entry}}
                _save_manifest(manifest_path, manifest)
            out.record(
                {"path": key, "status": "generated", "sha256": digest, "language": item.result.language, **entry}
            )

    out.summary(counts, pending=len(files) - sum(counts.values()))
    if stopped is not None:
        _err("Stopped: " + _explain(stopped.error))  # type: ignore[arg-type]
        return LIMIT if isinstance(stopped.error, LimitReachedError) else UNAVAILABLE
    return FAILED if counts["failed"] else OK


def _check(args: argparse.Namespace) -> int:
    config = Config.from_env(_environment(args.env_file))
    out = _Output(args.format)
    counts = {"ready": 0, "failed": 0}
    with AltGenerator(config) as generator:
        for path, root in _files(args.paths, args.recursive):
            key = _key(path, Path.cwd())
            try:
                prepared = generator.prepare(
                    ImageSource.from_path(path, within=root, max_bytes=config.image_policy.max_bytes)
                )
            except InvalidImageError as error:
                counts["failed"] += 1
                out.record({"path": key, "status": "failed", "error": _error(error)})
                continue
            counts["ready"] += 1
            out.record({"path": key, "status": "ready", "image": prepared.to_dict()})
    out.summary(counts)
    return FAILED if counts["failed"] else OK


# -- helpers ----------------------------------------------------------------------------------------------------------


class _Output:
    def __init__(self, format: str, stream: TextIO | None = None) -> None:
        self.format = format
        self.stream = stream or sys.stdout

    def record(self, record: dict[str, Any]) -> None:
        if self.format == "jsonl":
            print(json.dumps(record, ensure_ascii=False), file=self.stream, flush=True)
            return
        status = record["status"]
        if status in ("generated", "cached"):
            line = f"{record['path']}: {record['alt']}" + (" (cached)" if status == "cached" else "")
        elif status == "ready":
            image = record["image"]
            size = f"{image['width']}x{image['height']} {image['format'].upper()}, {image['size']} bytes"
            line = f"{record['path']}: ok, would send {size}"
        else:
            line = f"{record['path']}: FAILED - {record['error']['message']}"
        print(line, file=self.stream, flush=True)

    def summary(self, counts: dict[str, int], pending: int = 0) -> None:
        parts = [f"{value} {name}" for name, value in counts.items() if value]
        if pending:
            parts.append(f"{pending} not attempted")
        _err("Done: " + (", ".join(parts) or "nothing to do") + ".")


def _files(paths: list[str], recursive: bool) -> Iterator[tuple[str, str]]:
    """(file, the directory it may be read from) for every file argument and every image in a directory."""
    seen: set[str] = set()
    for given in paths:
        if os.path.isdir(given):
            root = os.path.realpath(given)
            found = []
            for directory, subdirectories, names in os.walk(given):
                subdirectories[:] = sorted(d for d in subdirectories if not d.startswith(".")) if recursive else []
                found += [
                    os.path.join(directory, name)
                    for name in names
                    if not name.startswith(".") and os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS
                ]
            for path in sorted(found):
                if path not in seen:
                    seen.add(path)
                    yield path, root
        elif os.path.exists(given) or os.path.islink(given):
            if given not in seen:
                seen.add(given)
                yield given, os.path.realpath(os.path.dirname(os.path.abspath(given)))
        else:
            raise ConfigurationError(f"No such file or directory: {given}")


def _key(path: str, base: Path) -> str:
    absolute = Path(os.path.abspath(path))
    try:
        return absolute.relative_to(base).as_posix()
    except ValueError:
        return Path(os.path.relpath(absolute, base)).as_posix()


def _entry(item: BatchItem) -> dict[str, Any]:
    result = item.result
    assert result is not None
    return {
        "alt": result.text,
        "variants": [alt.text for alt in result.alts],
        "image_uuid": result.image_uuid,
        "set_uuid": result.set_uuid,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _error(error: AltGeneratorError | None) -> dict[str, Any]:
    if error is None:
        return {"type": "unknown", "message": "Unknown error."}
    found: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
    reason = getattr(error, "reason", None)
    if isinstance(reason, str):
        found["reason"] = reason
    return found


def _load_manifest(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"version": MANIFEST_VERSION, "images": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigurationError(f"The manifest {path} is not valid JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("images"), dict):
        raise ConfigurationError(f"The manifest {path} is not a dockaccess-alt manifest.")
    return data


def _save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["version"] = MANIFEST_VERSION
    manifest["images"] = dict(sorted(manifest["images"].items()))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _environment(env_file: str | None) -> dict[str, str]:
    from .envfile import read_env_file

    values = read_env_file(env_file) if env_file else {}
    values.update(os.environ)
    return values


def _ask(label: str, interactive: bool) -> str:
    if not interactive:
        raise ConfigurationError(f"{label} is required (pass it as an option; prompts are off).")
    value = input(f"{label}: ").strip()
    if not value:
        raise ConfigurationError(f"{label} is required.")
    return value


def _ask_password(interactive: bool) -> str:
    if not interactive:
        raise ConfigurationError("The password is required: set DOCKACCESS_PASSWORD (prompts are off).")
    value = getpass.getpass("Password of the dockaccess.org account (used once, not stored): ")
    if not value:
        raise ConfigurationError("The password is required.")
    return value


def _mask(token: str | None) -> str | None:
    if not token:
        return None
    return token[:4] + "*" * max(4, len(token) - 4)


def _explain(error: AltGeneratorError) -> str:
    if isinstance(error, AuthenticationError):
        return "The account key was not accepted. Connect the application again: dockaccess-alt connect"
    if isinstance(error, LimitReachedError):
        window = "this month's" if error.period == "month" else "today's"
        return (
            f"{window.capitalize()} generation limit of the plan is spent. Check the usage with: dockaccess-alt status"
        )
    if isinstance(error, (SiteConnectionError, GeneratorUnavailableError)):
        return _EXPLAIN.get(error.reason, str(error))
    return str(error)


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
