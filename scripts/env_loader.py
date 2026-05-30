"""Small .env loader for local script runs.

The scripts use it before reading credentials so local runs can share the same
environment variable names as GitHub Actions secrets.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


ENV_LINE_PATTERN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_value(raw_value: str) -> str:
    """Parse one dotenv value, including simple quoted strings."""

    value = raw_value.strip()
    if not value:
        return ""

    quote = value[0]
    if quote in {"'", '"'}:
        end_index = value.find(quote, 1)
        if end_index >= 0:
            value = value[1:end_index]
        else:
            value = value[1:]
        if quote == '"':
            value = bytes(value, "utf-8").decode("unicode_escape")
        return value

    value = value.split(" #", 1)[0].strip()
    return value


def load_env_file(path: Path, override: bool = False) -> None:
    """Load KEY=value lines into os.environ without overriding by default."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        match = ENV_LINE_PATTERN.match(stripped)
        if not match:
            continue

        key, raw_value = match.groups()
        if override or key not in os.environ:
            os.environ[key] = parse_env_value(raw_value)
