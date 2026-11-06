"""Reading and updating ``.env`` files - what ``dockaccess-alt connect`` writes the keys into."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_PLAIN = re.compile(r"^[A-Za-z0-9_./:@+=,-]*$")


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """``KEY=value`` pairs of a dotenv file (quotes, ``export`` and comments understood). A missing file is
    empty."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def update_env_file(path: str | os.PathLike[str], values: Mapping[str, str | None]) -> None:
    """Sets (or, for ``None``, removes) variables, keeping every other line as it was.

    The file is replaced atomically; a new one is created readable by its owner only (0600), an existing one
    keeps its permissions.
    """
    target = os.fspath(path)
    try:
        with open(target, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        lines, mode = [], 0o600

    remaining = dict(values)
    out = []
    for line in lines:
        match = _LINE.match(line)
        if match and match.group(1) in values:
            value = remaining.pop(match.group(1), None)
            if value is not None:
                out.append(f"{match.group(1)}={_quote(value)}")
            continue
        out.append(line)
    additions = [f"{name}={_quote(value)}" for name, value in remaining.items() if value is not None]
    if additions:
        if out and out[-1].strip() != "":
            out.append("")
        out.extend(additions)

    directory = os.path.dirname(os.path.abspath(target))
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            while out and out[-1].strip() == "":
                out.pop()
            handle.write("\n".join(out) + "\n" if out else "")
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _quote(value: str) -> str:
    if _PLAIN.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r"\\(.)", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), raw[1:-1])
    return re.sub(r"\s+#.*$", "", raw)
