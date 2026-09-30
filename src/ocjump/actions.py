"""tmux side effects: jump to an existing pane or open a new tmux session.

Command construction is split from execution (pure ``*_argv`` helpers versus
the ``jump`` / ``open_session`` wrappers) so the exact argv can be asserted in
tests without running tmux.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from .db import Session
from .panes import Pane
from .runner import Result, Runner, run

TMUX_SESSION_PREFIX = "oc-"
SESSION_SLUG_MAX = 32

# A tmux popup inherits the server's PATH, which often lacks the directory
# holding the ``opencode`` binary (e.g. ``~/.opencode/bin``).  Resolving to an
# absolute path before spawning the session is therefore mandatory: otherwise
# the new session dies instantly with "command not found".
FALLBACK_DIRS = (
    "~/.opencode/bin",
    "~/.local/bin",
    "~/.cache/opencode/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def jump_commands(pane: Pane) -> list[list[str]]:
    """Return the tmux commands that focus ``pane`` on the current client.

    Three steps are required: move the attached client to the pane's session,
    select its window, then select the pane itself.
    """
    return [
        ["tmux", "switch-client", "-t", pane.session_name],
        ["tmux", "select-window", "-t", f"{pane.session_name}:{pane.window_index}"],
        ["tmux", "select-pane", "-t", pane.pane_id],
    ]


def jump(pane: Pane, runner: Runner = run) -> list[Result]:
    """Focus ``pane``; individual step failures are tolerated and returned.

    ``switch-client`` fails when there is no attached client (e.g. a detached
    inspection), which is harmless, so errors are surfaced to the caller rather
    than raised.
    """
    return [runner(argv) for argv in jump_commands(pane)]


def resolve_command(command: str, search_dirs: Iterable[str] | None = None) -> str | None:
    """Resolve ``command`` to an executable path, or ``None`` when not found.

    A ``command`` containing a path separator is used as-is (if executable).
    Otherwise the process PATH is consulted first, then a list of well-known
    install directories — because a tmux popup inherits the *server* PATH and
    that frequently omits ``~/.opencode/bin``, where the OpenCode binary lives.
    """
    if os.sep in command:
        return command if os.access(command, os.X_OK) else None
    found = shutil.which(command)
    if found:
        return found
    if search_dirs is None:
        search_dirs = (os.path.expanduser(directory) for directory in FALLBACK_DIRS)
    for directory in search_dirs:
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def build_open_command(command: str, session_id: str, fork: bool = False) -> str:
    """Build the shell command that resumes (or forks) ``session_id``.

    ``command`` is the OpenCode executable (possibly with fixed flags); the
    session flag is appended so a resumed session opens exactly where it left.
    With ``fork`` the session is forked into a new one instead.
    """
    suffix = " --fork" if fork else ""
    return f"{command} --session {session_id}{suffix}"


def session_name(
    title: str, existing: Iterable[str] = (), prefix: str = TMUX_SESSION_PREFIX
) -> str:
    """Derive a unique tmux session name from a session title.

    The title is slugified to ``[a-z0-9-]`` (tmux forbids ``.`` and ``:`` in
    session names) and suffixed with ``-2``, ``-3``… when the slug is taken.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:SESSION_SLUG_MAX].strip("-")
    base = f"{prefix}{slug or 'session'}"
    taken = set(existing)
    name = base
    index = 2
    while name in taken:
        name = f"{base}-{index}"
        index += 1
    return name


def window_name(title: str) -> str:
    """Derive a compact single-line tmux window name from a session title."""
    flat = " ".join(title.split())
    return f"OC {flat}"[:40] if flat else "OC"


def existing_session_names(runner: Runner = run) -> set[str]:
    """Return the names of the tmux sessions on the current server."""
    result = runner(["tmux", "list-sessions", "-F", "#{session_name}"])
    if result.returncode != 0:
        return set()
    return {line for line in result.stdout.splitlines() if line}


def new_session_argv(name: str, directory: str, command_string: str) -> list[str]:
    """Return the tmux argv that creates a detached session running a command."""
    return ["tmux", "new-session", "-d", "-s", name, "-c", directory, command_string]


def new_window_argv(name: str, directory: str, command_string: str) -> list[str]:
    """Return the tmux argv that opens a window in the current session."""
    return ["tmux", "new-window", "-c", directory, "-n", name, command_string]


def split_window_argv(directory: str, command_string: str) -> list[str]:
    """Return the tmux argv that opens a pane split in the current window."""
    return ["tmux", "split-window", "-c", directory, command_string]


def open_new_session(
    session: Session, command: str, prefix: str, runner: Runner = run, fork: bool = False
) -> tuple[str, list[Result]]:
    """Create a dedicated tmux session for ``session`` and switch the client to it.

    Returns the chosen session name and the commands' results, the first being
    the critical ``new-session`` and the second the best-effort
    ``switch-client`` (which fails harmlessly with no attached client).
    """
    name = session_name(session.title, existing_session_names(runner), prefix)
    created = runner(
        new_session_argv(
            name,
            session.directory,
            build_open_command(command, session.session_id, fork),
        )
    )
    if created.returncode != 0:
        return name, [created]
    switched = runner(["tmux", "switch-client", "-t", name])
    return name, [created, switched]


def open_new_window(
    session: Session, command: str, fork: bool = False, runner: Runner = run
) -> tuple[str, list[Result]]:
    """Open ``session`` in a new window of the current tmux session."""
    name = window_name(session.title)
    result = runner(
        new_window_argv(
            name,
            session.directory,
            build_open_command(command, session.session_id, fork),
        )
    )
    return name, [result]


def open_split(
    session: Session, command: str, fork: bool = False, runner: Runner = run
) -> tuple[str, list[Result]]:
    """Open ``session`` in a pane split of the current tmux window."""
    result = runner(
        split_window_argv(
            session.directory, build_open_command(command, session.session_id, fork)
        )
    )
    return "split", [result]


def open_session(
    session: Session,
    command: str,
    action: str = "session",
    prefix: str = TMUX_SESSION_PREFIX,
    runner: Runner = run,
    fork: bool = False,
) -> tuple[str, list[Result]]:
    """Dispatch to the configured open action, returning a label and results."""
    if action == "session":
        return open_new_session(session, command, prefix, runner, fork)
    if action == "window":
        return open_new_window(session, command, fork, runner)
    if action == "split":
        return open_split(session, command, fork, runner)
    raise ValueError(f"unknown open action: {action!r}")


def delete_sessions(command: str, session_ids: Iterable[str], runner: Runner = run) -> list[Result]:
    """Delete sessions by delegating to ``opencode session delete`` (never our DB).

    ``command`` is the resolved OpenCode executable.
    """
    return [runner([command, "session", "delete", session_id]) for session_id in session_ids]


def close_sessions(names: Iterable[str], runner: Runner = run) -> list[Result]:
    """Close tmux sessions by name (``tmux kill-session``)."""
    return [runner(["tmux", "kill-session", "-t", name]) for name in names]
