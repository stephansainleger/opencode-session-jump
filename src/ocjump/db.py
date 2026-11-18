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

SESSION_COLUMNS = "id, title, directory, time_updated, time_archived, parent_id, agent, model"

PREVIEW_DEFAULT_DIRECT = 4
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
    archived: bool = False


@dataclass(frozen=True)
class PreviewPart:
    """One visible unit inside a previewed message.

    ``kind`` is ``text``, ``reasoning`` or ``tool``; tool parts carry the tool
    name and its last state status so the renderer can label them without
    re-parsing JSON.
    """

    kind: str
    text: str = ""
    tool: str = ""
    status: str = ""


@dataclass(frozen=True)
class PreviewMessage:
    """A message selected for the preview, with its parts kept separate."""

    role: str
    time_created: int
    parts: tuple[PreviewPart, ...]


@dataclass(frozen=True)
class Preview:
    """The tail of a session shown in fzf's preview pane.

    ``messages`` covers the newest messages back to (and including) the
    ``direct_limit``-th most recent direct message, so a long run of
    intermediate assistant steps cannot push user-facing text out of view.
    ``total`` is the whole session's message count and ``omitted`` is how many
    older messages precede the selected window.
    """

    messages: tuple[PreviewMessage, ...]
    total: int
    omitted: int


def is_direct_message(message: PreviewMessage) -> bool:
    """Return whether a message is a user prompt or contains text for the user."""
    if message.role == "user":
        return True
    if message.role != "assistant":
        return False
    return any(part.kind == "text" and part.text.strip() for part in message.parts)


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


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """Return the SQLite table names present in the store."""
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _session_source(conn: sqlite3.Connection) -> str:
    """Return the SQL source for the canonical session table.

    OpenCode 2 keeps ``session_v2`` as the canonical table. Reading it alone
    avoids legacy rows that OpenCode 2 can no longer delete (for example the
    ghosts left when ``session delete`` removes only ``session_v2``). OpenCode 1,
    which has no ``session_v2``, keeps reading ``session``.
    """
    tables = _table_names(conn)
    if "session_v2" in tables:
        return f"(SELECT {SESSION_COLUMNS} FROM session_v2)"
    return f"(SELECT {SESSION_COLUMNS} FROM session)"


def validate_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Verify the store exposes the session schema the picker needs.

    OpenCode 1 stores sessions in ``session``; OpenCode 2 adds ``session_v2``
    and keeps ``session`` as a compatibility table. Both are accepted, and
    every present table must carry the columns the picker reads.

    Raises:
        SchemaError: when the store is a pre-SQLite install or an unsupported
            schema, so the user gets an actionable message.
    """
    tables = _table_names(conn)
    table = "session_v2" if "session_v2" in tables else "session"
    if table not in tables:
        raise SchemaError(
            f"{path} has no 'session' or 'session_v2' table — it is not a supported "
            "OpenCode store (pre-SQLite/legacy layout)."
        )
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = REQUIRED_SESSION_COLUMNS - columns
    if missing:
        raise SchemaError(
            f"{path} uses an unsupported OpenCode schema: {table} is missing "
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


def list_sessions(
    conn: sqlite3.Connection,
    include_children: bool = False,
    include_archived: bool = False,
    limit: int = 0,
) -> list[Session]:
    """Return persisted sessions, most recently updated first.

    Sub-agent sessions (non-null ``parent_id``) are hidden unless
    ``include_children`` is true; archived sessions are hidden unless
    ``include_archived`` is true.  ``limit`` caps the rows when positive, so a
    frequent caller (for example ``--status``) can bound its cost.
    """
    where = []
    if not include_archived:
        where.append("time_archived IS NULL")
    if not include_children:
        where.append("parent_id IS NULL")
    query = (
        f"SELECT {SESSION_COLUMNS} "
        f"FROM {_session_source(conn)} AS sessions"
    )
    if where:
        query += f" WHERE {' AND '.join(where)}"
    query += " ORDER BY time_updated DESC"
    if limit > 0:
        query += f" LIMIT {int(limit)}"
    return [
        Session(
            session_id=row["id"],
            title=row["title"],
            directory=row["directory"],
            time_updated=row["time_updated"],
            agent=row["agent"],
            model=row["model"],
            archived=bool(row["time_archived"]),
        )
        for row in conn.execute(query)
    ]


def get_session(conn: sqlite3.Connection, session_id: str) -> Session:
    """Return the session with ``session_id``.

    Raises:
        KeyError: no such session exists.
    """
    row = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM {_session_source(conn)} AS sessions WHERE id=?",
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
        archived=bool(row["time_archived"]),
    )


def _preview_part_obj(part) -> PreviewPart | None:
    """Turn one parsed part/content object into a :class:`PreviewPart`.

    Text and reasoning carry a ``text`` field; tool entries keep their name
    (``tool`` in v1 parts, ``name`` in v2 content) and last ``state.status``.
    """
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind == "text":
        text = (part.get("text") or "").strip()
        return PreviewPart("text", text) if text else None
    if kind == "reasoning":
        text = (part.get("text") or "").strip()
        return PreviewPart("reasoning", text) if text else None
    if kind == "tool":
        tool = part.get("tool") or part.get("name") or "tool"
        status = (part.get("state") or {}).get("status") or "?"
        return PreviewPart("tool", tool=tool, status=status)
    return None


def _preview_part(data: str) -> PreviewPart | None:
    """Turn one ``part`` row payload into a typed, render-ready part."""
    try:
        part = json.loads(data)
    except (TypeError, ValueError):
        return None
    return _preview_part_obj(part)


def _build_preview_message(row: sqlite3.Row, part_data: list[str]) -> PreviewMessage:
    """Build one :class:`PreviewMessage` from a message row and its raw parts."""
    try:
        role = json.loads(row["data"]).get("role", "?")
    except (TypeError, ValueError):
        role = "?"
    parts = tuple(
        part
        for part in (
            _preview_part(data) for data in part_data[:PREVIEW_MAX_PARTS]
        )
        if part is not None
    )
    return PreviewMessage(
        role=role,
        time_created=int(row["time_created"]),
        parts=parts,
    )


def _build_preview_message_v2(row: sqlite3.Row) -> PreviewMessage:
    """Build one preview message from a v2 ``session_message`` row.

    V2 stores parts inside ``data.content`` for assistant messages and the
    prompt text directly in ``data.text`` for user messages.
    """
    try:
        payload = json.loads(row["data"])
    except (TypeError, ValueError):
        payload = {}
    role = row["type"] or payload.get("role") or "?"
    content = payload.get("content")
    if isinstance(content, list):
        parts = tuple(
            part
            for part in (
                _preview_part_obj(item) for item in content[:PREVIEW_MAX_PARTS]
            )
            if part is not None
        )
    else:
        text = (payload.get("text") or "").strip()
        parts = (PreviewPart("text", text),) if text else ()
    return PreviewMessage(
        role=role,
        time_created=int(row["time_created"] or 0),
        parts=parts,
    )


def load_preview(
    conn: sqlite3.Connection,
    session_id: str,
    direct_limit: int = PREVIEW_DEFAULT_DIRECT,
) -> Preview:
    """Load the newest window containing ``direct_limit`` direct messages.

    Walking backwards from the newest message keeps every intermediate
    assistant step between the direct entries, so the window size adapts to the
    conversation instead of being a fixed message count. Parts stay typed for
    the renderer, and ``total``/``omitted`` let it report the window honestly.

    OpenCode 2 stores messages in ``session_message`` with parts embedded in
    ``data.content``. Legacy sessions keep using ``message`` + ``part``.

    Raises:
        ValueError: ``direct_limit`` is lower than one.
    """
    if direct_limit < 1:
        raise ValueError(f"preview direct limit must be positive, got {direct_limit}")

    if "session_message" in _table_names(conn):
        v2_rows = list(
            conn.execute(
                "SELECT id, type, time_created, data FROM session_message "
                "WHERE session_id=? ORDER BY time_created DESC, seq DESC",
                (session_id,),
            )
        )
        if v2_rows:
            selected: list[PreviewMessage] = []
            direct_count = 0
            for row in v2_rows:
                message = _build_preview_message_v2(row)
                selected.append(message)
                if is_direct_message(message):
                    direct_count += 1
                    if direct_count >= direct_limit:
                        break
            selected.reverse()
            total = len(v2_rows)
            return Preview(
                messages=tuple(selected),
                total=total,
                omitted=max(0, total - len(selected)),
            )

    message_rows = list(
        conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id=? "
            "ORDER BY time_created DESC, id DESC",
            (session_id,),
        )
    )
    parts_by_message: dict[str, list[str]] = {}
    for part_row in conn.execute(
        "SELECT message_id, data FROM part WHERE session_id=? "
        "ORDER BY message_id, time_created, id",
        (session_id,),
    ):
        parts_by_message.setdefault(part_row["message_id"], []).append(part_row["data"])
    selected = []
    direct_count = 0
    for row in message_rows:
        message = _build_preview_message(row, parts_by_message.get(row["id"], []))
        selected.append(message)
        if is_direct_message(message):
            direct_count += 1
            if direct_count >= direct_limit:
                break
    selected.reverse()
    total = len(message_rows)
    return Preview(
        messages=tuple(selected),
        total=total,
        omitted=max(0, total - len(selected)),
    )


def model_label(raw: str | None) -> str:
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
