"""Read-only access to the OpenCode SQLite store.

The ``opencode`` binary owns the store (``~/.local/share/opencode/opencode.db``
by default) and keeps it in WAL mode.  We open it read-only (``mode=ro`` +
``PRAGMA query_only``) so the picker can never mutate or lock a running
instance, and we validate the schema up-front so an unsupported OpenCode
version fails with a clear message instead of an opaque SQL error.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .runner import run

ENV_DB = "OPENCODE_DB"
ENV_CHANNEL = "OPENCODE_CHANNEL"
ENV_DISABLE_CHANNEL_DB = "OPENCODE_DISABLE_CHANNEL_DB"
DATA_SUBDIR = Path("opencode")

# Channels whose store keeps the plain ``opencode.db`` name.
STABLE_CHANNELS = frozenset({"", "latest", "stable", "beta", "prod"})

# Columns the picker reads from the ``session`` table.  A store missing any of
# them is a different (older/newer) schema and must not be queried blindly.
REQUIRED_SESSION_COLUMNS = frozenset(
    {"id", "title", "directory", "time_updated", "parent_id", "time_archived", "agent", "model"}
)

PREVIEW_TEXT_LIMIT = 400
PREVIEW_MAX_PARTS = 40


class SchemaError(RuntimeError):
    """The OpenCode store does not expose the schema this picker supports."""


@dataclass(frozen=True)
class Session:
    """A persisted OpenCode conversation as shown in the picker."""

    session_id: str
    title: str
    directory: str
    time_updated: int
    agent: str | None = None
    model: str | None = None


def _env_flag(name: str) -> bool:
    """Return true when environment variable ``name`` is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def default_db_path() -> Path:
    """Return the conventional store path, honoring the OpenCode channel.

    Non-stable channels (``$OPENCODE_CHANNEL``) use ``opencode-<channel>.db``
    unless ``$OPENCODE_DISABLE_CHANNEL_DB`` is set, mirroring upstream.
    """
    data_home = os.environ.get("XDG_DATA_HOME")
    directory = (
        Path(data_home).expanduser() / DATA_SUBDIR
        if data_home
        else Path("~/.local/share").expanduser() / DATA_SUBDIR
    )
    channel = os.environ.get(ENV_CHANNEL, "").strip()
    if channel and channel not in STABLE_CHANNELS and not _env_flag(ENV_DISABLE_CHANNEL_DB):
        return directory / f"opencode-{channel}.db"
    return directory / "opencode.db"


def probe_db_path(runner: Callable[..., object] = run) -> Path | None:
    """Ask the ``opencode`` binary for its active store path, if available.

    This is authoritative: it handles the channel suffix and custom locations
    exactly as OpenCode does.  Returns ``None`` when ``opencode`` is absent,
    errors, or reports a path that does not exist.
    """
    executable = shutil.which("opencode")
    if not executable:
        return None
    result = runner([executable, "db", "path"])
    if result.returncode != 0:
        return None
    candidate = result.stdout.strip().splitlines()
    if not candidate:
        return None
    path = Path(candidate[-1]).expanduser()
    return path if path.exists() else None


def resolve_db_path(
    explicit: str | None = None, probe: Callable[[], Path | None] | None = None
) -> Path:
    """Resolve the store path: flag, env, conventional path, then probe.

    Order: ``--db`` → ``$OPENCODE_DB`` → the conventional path when it exists →
    ``probe`` (usually :func:`probe_db_path`).  The probe runs only when the
    conventional file is missing, because asking the ``opencode`` binary is
    authoritative but slow (it starts the whole runtime).
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_DB)
    if env:
        return Path(env).expanduser()
    default = default_db_path()
    if default.exists():
        return default
    if probe is not None:
        found = probe()
        if found is not None:
            return found
    return default


def validate_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Verify the store has the ``session`` schema the picker needs.

    Raises:
        SchemaError: when the store is a pre-SQLite install or an unsupported
            schema, so the user gets an actionable message.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "session" not in tables:
        raise SchemaError(
            f"{path} has no 'session' table — it is not a supported OpenCode store "
            "(pre-SQLite/legacy layout)."
        )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
    missing = REQUIRED_SESSION_COLUMNS - columns
    if missing:
        raise SchemaError(
            f"{path} uses an unsupported OpenCode schema: session is missing "
            f"{', '.join(sorted(missing))}."
        )


def connect(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only, failing fast with an actionable message.

    Raises:
        FileNotFoundError: the store does not exist.
        SchemaError: the store exists but is not a supported OpenCode store.
        sqlite3.Error: the file exists but cannot be opened read-only.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"OpenCode store not found: {path}\n"
            "Pass --db PATH or set $OPENCODE_DB to point at opencode.db."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only=1")
    conn.row_factory = sqlite3.Row
    validate_schema(conn, path)
    return conn


def list_sessions(conn: sqlite3.Connection, include_children: bool = False) -> list[Session]:
    """Return persisted sessions, most recently updated first.

    Sub-agent sessions (non-null ``parent_id``) and archived sessions are
    hidden unless ``include_children`` is true.
    """
    where = ["time_archived IS NULL"]
    if not include_children:
        where.append("parent_id IS NULL")
    query = (
        "SELECT id, title, directory, time_updated, agent, model "
        f"FROM session WHERE {' AND '.join(where)} "
        "ORDER BY time_updated DESC"
    )
    return [
        Session(
            session_id=row["id"],
            title=row["title"],
            directory=row["directory"],
            time_updated=row["time_updated"],
            agent=row["agent"],
            model=row["model"],
        )
        for row in conn.execute(query)
    ]


def get_session(conn: sqlite3.Connection, session_id: str) -> Session:
    """Return the session with ``session_id``.

    Raises:
        KeyError: no such session exists.
    """
    row = conn.execute(
        "SELECT id, title, directory, time_updated, agent, model FROM session WHERE id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(session_id)
    return Session(
        session_id=row["id"],
        title=row["title"],
        directory=row["directory"],
        time_updated=row["time_updated"],
        agent=row["agent"],
        model=row["model"],
    )


def _text_of_part(data: str) -> str | None:
    """Extract the visible text of a single ``part`` row, or ``None``.

    Text and reasoning parts carry a ``text`` field; tool parts carry a tool
    name and a ``state.status`` used to summarize the call.
    """
    try:
        part = json.loads(data)
    except (TypeError, ValueError):
        return None
    kind = part.get("type")
    if kind == "text":
        text = (part.get("text") or "").strip()
        return text or None
    if kind == "reasoning":
        text = (part.get("text") or "").strip()
        return f"[thinking] {text}" if text else None
    if kind == "tool":
        tool = part.get("tool") or "tool"
        status = (part.get("state") or {}).get("status") or "?"
        return f"[tool:{tool} {status}]"
    return None


def _message_lines(conn: sqlite3.Connection, session_id: str, limit: int) -> list[str]:
    """Return the last ``limit`` messages rendered as ``role: text`` lines."""
    rows = list(
        conn.execute(
            "SELECT id, data FROM message WHERE session_id=? "
            "ORDER BY time_created DESC LIMIT ?",
            (session_id, limit),
        )
    )
    lines: list[str] = []
    for row in reversed(rows):
        try:
            role = json.loads(row["data"]).get("role", "?")
        except (TypeError, ValueError):
            role = "?"
        chunks: list[str] = []
        for part in conn.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created LIMIT ?",
            (row["id"], PREVIEW_MAX_PARTS),
        ):
            text = _text_of_part(part["data"])
            if text:
                chunks.append(text)
        body = " ".join(chunks)
        if len(body) > PREVIEW_TEXT_LIMIT:
            body = body[:PREVIEW_TEXT_LIMIT] + "\u2026"
        lines.append(f"{role}: {body}")
    return lines


def _model_label(raw: str | None) -> str:
    """Render the session ``model`` JSON column as ``provider/model``."""
    if not raw:
        return "?"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    provider = data.get("providerID", "?")
    name = data.get("id", "?")
    return f"{provider}/{name}"


def describe(conn: sqlite3.Connection, session: Session) -> str:
    """Return a multi-line, human-readable summary used by the preview pane."""
    header = [
        f"title:     {session.title}",
        f"id:        {session.session_id}",
        f"directory: {session.directory}",
        f"agent:     {session.agent or '?'}",
        f"model:     {_model_label(session.model)}",
        "",
    ]
    return "\n".join(header + _message_lines(conn, session.session_id, limit=8))
