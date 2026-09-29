"""tmux pane discovery and session↔pane mapping.

A running OpenCode TUI sets its pane title to ``OC | <session title>`` and, when
the companion plugin is loaded, publishes ``@oc_session_id`` / ``@oc_state`` /
``@oc_state_at``.  This module turns ``tmux list-panes`` output into :class:`Pane`
objects and resolves which persisted session each pane currently displays.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .db import Session
from .runner import Runner, run

OPENCODE_COMMAND = "opencode"
TITLE_PREFIX = "OC | "
ELLIPSISES = ("\u2026", "...")

PANE_FORMAT = "\t".join(
    (
        "#{pane_id}",
        "#{session_name}",
        "#{window_index}",
        "#{pane_index}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_title}",
        "#{@oc_session_id}",
        "#{@oc_state}",
        "#{@oc_state_at}",
    )
)

_FIELD_COUNT = len(PANE_FORMAT.split("\t"))


@dataclass(frozen=True)
class Pane:
    """A tmux pane currently running an OpenCode TUI."""

    pane_id: str
    session_name: str
    window_index: int
    pane_index: int
    current_path: str
    title: str
    bound_session_id: str
    state: str
    state_at: int | None


def parse_panes(output: str) -> list[Pane]:
    """Parse ``tmux list-panes`` output, keeping only OpenCode panes.

    Malformed lines are skipped rather than raising: tmux format output is not
    a stable contract and a single odd line must not take down the picker.
    """
    panes: list[Pane] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != _FIELD_COUNT or fields[4] != OPENCODE_COMMAND:
            continue
        (
            pane_id,
            session_name,
            window_index,
            pane_index,
            _cmd,
            path,
            title,
            bound,
            state,
            state_at,
        ) = fields
        panes.append(
            Pane(
                pane_id=pane_id,
                session_name=session_name,
                window_index=int(window_index),
                pane_index=int(pane_index),
                current_path=path,
                title=title,
                bound_session_id=bound,
                state=state,
                state_at=int(state_at) if state_at.isdigit() else None,
            )
        )
    return panes


def list_panes(runner: Runner = run) -> list[Pane]:
    """Return all tmux panes running OpenCode, or ``[]`` when tmux is absent."""
    result = runner(["tmux", "list-panes", "-a", "-F", PANE_FORMAT])
    if result.returncode != 0:
        return []
    return parse_panes(result.stdout)


def split_pane_title(title: str) -> tuple[str, bool]:
    """Split an ``OC | ...`` pane title into its body and a truncation flag.

    Returns ``(body, truncated)`` where ``body`` has any trailing ellipsis
    removed.  A non-OpenCode title yields ``("", False)``.
    """
    if not title.startswith(TITLE_PREFIX):
        return "", False
    body = title[len(TITLE_PREFIX) :]
    for mark in ELLIPSISES:
        if body.endswith(mark):
            return body[: -len(mark)], True
    return body, False


def match_session(pane: Pane, sessions: Iterable[Session], by_id: dict[str, Session]) -> str | None:
    """Resolve the session id displayed by ``pane``, or ``None`` if ambiguous.

    Tier 1 uses the plugin-published ``@oc_session_id``.  Tier 2 matches the
    (possibly truncated) pane title against titles of sessions in the same
    working directory; the match is accepted only when exactly one candidate
    exists, so an ambiguous title degrades to ``None`` instead of guessing.
    """
    if pane.bound_session_id and pane.bound_session_id in by_id:
        return pane.bound_session_id
    body, truncated = split_pane_title(pane.title)
    if not body:
        return None
    candidates = [
        session
        for session in sessions
        if session.directory == pane.current_path
        and (
            session.title.startswith(body)
            if truncated
            else session.title == body
        )
    ]
    if len(candidates) == 1:
        return candidates[0].session_id
    return None


def resolve(sessions: Iterable[Session], panes: Iterable[Pane]) -> dict[str, Pane]:
    """Map session ids to the pane displaying them (first pane wins)."""
    sessions = list(sessions)
    by_id = {session.session_id: session for session in sessions}
    mapping: dict[str, Pane] = {}
    for pane in panes:
        session_id = match_session(pane, sessions, by_id)
        if session_id and session_id not in mapping:
            mapping[session_id] = pane
    return mapping
