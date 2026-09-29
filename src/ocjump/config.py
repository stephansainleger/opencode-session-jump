"""Optional user configuration (``~/.config/ocjump/config.ini``).

Zero-dependency and entirely optional: when the file is absent every setting
falls back to a sensible default.  CLI flags always override these values.
The format is INI so it needs no third-party parser and stays easy to edit::

    [ocjump]
    open = session        ; session | window | split
    command = opencode
    session_prefix = oc-
    theme = opencode      ; opencode | ansi
    # db = /path/to/opencode.db
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR = "ocjump"
CONFIG_NAME = "config.ini"
ENV_CONFIG = "OCJUMP_CONFIG"

OPEN_ACTIONS = ("session", "window", "split")
DEFAULT_OPEN_ACTION = "session"
DEFAULT_COMMAND = "opencode"
DEFAULT_SESSION_PREFIX = "oc-"
DEFAULT_THEME = "opencode"


class ConfigError(RuntimeError):
    """The configuration file exists but is malformed or invalid."""


@dataclass(frozen=True)
class Config:
    """Resolved configuration values (before CLI overrides)."""

    open_action: str = DEFAULT_OPEN_ACTION
    command: str = DEFAULT_COMMAND
    session_prefix: str = DEFAULT_SESSION_PREFIX
    theme: str = DEFAULT_THEME
    db: str | None = None


def config_path() -> Path:
    """Return the conventional configuration path under the XDG config dir."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / APP_DIR / CONFIG_NAME


def load(path: Path | None = None) -> Config:
    """Load configuration from ``path`` (or the default / ``$OCJUMP_CONFIG``).

    Raises:
        ConfigError: the file exists but cannot be parsed or holds an unknown
            ``open`` action.
    """
    resolved = Path(path or os.environ.get(ENV_CONFIG) or config_path()).expanduser()
    if not resolved.exists():
        return Config()
    parser = configparser.ConfigParser()
    try:
        with resolved.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (configparser.Error, OSError) as exc:
        raise ConfigError(f"invalid config {resolved}: {exc}") from exc

    def value(key: str, default: str | None) -> str | None:
        if parser.has_option("ocjump", key):
            text = parser.get("ocjump", key, raw=True).strip()
            return text or default
        return default

    open_action = value("open", DEFAULT_OPEN_ACTION)
    if open_action not in OPEN_ACTIONS:
        raise ConfigError(
            f"invalid 'open' = {open_action!r} in {resolved}; "
            f"expected one of {', '.join(OPEN_ACTIONS)}"
        )
    return Config(
        open_action=open_action,
        command=value("command", DEFAULT_COMMAND) or DEFAULT_COMMAND,
        session_prefix=value("session_prefix", DEFAULT_SESSION_PREFIX)
        or DEFAULT_SESSION_PREFIX,
        theme=value("theme", DEFAULT_THEME) or DEFAULT_THEME,
        db=value("db", None),
    )
