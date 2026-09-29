"""Shared helpers for the test suite: a throwaway OpenCode store builder.

The real store schema is wide; tests only need the subset of columns the
picker reads, so the builder creates a minimal, self-contained database that
keeps unit tests fast and independent from the user's data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'p',
    parent_id TEXT,
    slug TEXT NOT NULL DEFAULT 's',
    directory TEXT NOT NULL,
    title TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1',
    time_created INTEGER NOT NULL DEFAULT 0,
    time_updated INTEGER NOT NULL DEFAULT 0,
    time_archived INTEGER,
    agent TEXT,
    model TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL DEFAULT 0,
    time_updated INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL DEFAULT 0,
    time_updated INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
"""


def make_db(path: Path) -> sqlite3.Connection:
    """Create and return a connection to a minimal OpenCode store at ``path``."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def add_session(
    conn: sqlite3.Connection,
    session_id: str,
    title: str,
    directory: str,
    time_updated: int = 1000,
    parent_id: str | None = None,
    time_archived: int | None = None,
) -> None:
    """Insert one session row with sane defaults."""
    conn.execute(
        "INSERT INTO session (id, title, directory, time_updated, parent_id, time_archived, "
        "agent, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            title,
            directory,
            time_updated,
            parent_id,
            time_archived,
            "build",
            json.dumps({"id": "m", "providerID": "prov"}),
        ),
    )
    conn.commit()


def add_message(
    conn: sqlite3.Connection,
    message_id: str,
    session_id: str,
    role: str,
    text: str,
    time_created: int = 0,
) -> None:
    """Insert a message with a single text part."""
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            message_id,
            session_id,
            time_created,
            time_created,
            json.dumps({"role": role}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            f"{message_id}-p",
            message_id,
            session_id,
            time_created,
            time_created,
            json.dumps({"type": "text", "text": text}),
        ),
    )
    conn.commit()


def pane_line(
    pane_id: str,
    session_name: str,
    title: str,
    current_path: str,
    command: str = "opencode",
    bound_session_id: str = "",
    state: str = "",
    state_at: str = "",
) -> str:
    """Build one tab-separated ``tmux list-panes`` line matching PANE_FORMAT."""
    return "\t".join(
        (
            pane_id,
            session_name,
            "0",
            "0",
            command,
            current_path,
            title,
            bound_session_id,
            state,
            state_at,
        )
    )
