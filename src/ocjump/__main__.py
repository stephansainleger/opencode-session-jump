"""Command-line entry point for ``ocjump``.

Modes: the default interactive fzf picker, ``--list`` (human/JSON/NUL listing)
and ``--preview <session-id>`` used by fzf's preview pane.  The picker only ever
reads the OpenCode store; mutation is limited to focusing or spawning tmux
sessions, windows or panes.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, db, log, render, theme
from .actions import (
    close_sessions,
    delete_sessions,
    existing_session_names,
    jump,
    open_session,
    resolve_command,
    session_name,
)
from .panes import Pane, list_panes, resolve
from .runner import Runner, run

# Interpreter start, so the log can measure shim/python startup per process.
_IMPORT_TIME = time.perf_counter()

FZF_TABSTOP = "2"
SEARCH_TEXT_FIELDS = "4,5"  # directory + title (fields 1..7: glyph,label,age,dir,title,id,pane)
ACTION_KEYS = ("ctrl-f", "ctrl-d", "ctrl-k")
ACTIONS = ("fork", "delete", "close")
BORDER_LABEL = " sessions "
# Second header line, shown at the bottom of the popup under the column legend.
KEY_HELP = "Enter open · ctrl-f fork · ctrl-d delete · ctrl-k close · Tab multi · Esc quit"
PREVIEW_LABEL = " preview "
NOTIFY_MS = "5000"


@dataclass(frozen=True)
class Settings:
    """Effective settings after merging CLI flags and the config file."""

    db: Path
    command: str
    open_action: str
    session_prefix: str
    theme: str = config.DEFAULT_THEME
    sort: str = config.DEFAULT_SORT
    search: str = config.DEFAULT_SEARCH
    no_state: bool = False


@dataclass(frozen=True)
class Selection:
    """Outcome of the picker: the key pressed (``""`` for Enter) and the lines."""

    key: str
    lines: list[str]


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (pure, so it can be reused in tests)."""
    parser = argparse.ArgumentParser(
        prog="ocjump",
        description="List OpenCode sessions and jump to (or open) their tmux pane.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="OpenCode SQLite store (default: $OPENCODE_DB, then auto-detected)",
    )
    parser.add_argument(
        "--command",
        default=None,
        help="OpenCode executable used to open a session (default: opencode)",
    )
    parser.add_argument(
        "--open",
        choices=config.OPEN_ACTIONS,
        default=None,
        help="What to open for a closed session: session (default), window or split",
    )
    parser.add_argument(
        "--sort",
        choices=config.SORT_MODES,
        default=None,
        help="Order: recent (default) or attention (waiting/working first)",
    )
    parser.add_argument("--all", action="store_true", help="Include sub-agent sessions")
    parser.add_argument(
        "--no-state", action="store_true", help="Skip tmux probing (offline / tests)"
    )
    parser.add_argument("--list", action="store_true", help="Print sessions and exit")
    parser.add_argument(
        "--preview", metavar="SESSION_ID", help="Print the preview for one session and exit"
    )
    parser.add_argument("--json", "-j", action="store_true", help="NDJSON output for --list")
    parser.add_argument(
        "--null", "-0", action="store_true", dest="nul", help="NUL-separated output for --list"
    )
    # Hidden callbacks the fzf binds invoke; not part of the user-facing surface.
    parser.add_argument("--list-lines", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--action", choices=ACTIONS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--session", action="append", default=[], metavar="SESSION_ID", help=argparse.SUPPRESS
    )
    parser.add_argument("--version", action="version", version=f"ocjump {__version__}")
    return parser


def _records(
    settings: Settings, include_children: bool
) -> tuple[list[render.Record], dict[str, Pane]]:
    """Load sessions, probe panes and pair them into display records.

    Timings are logged (``records_ms`` / ``connect_ms`` / ``list_ms`` /
    ``panes_ms``) so a slow refresh after an action can be attributed to the
    store read rather than guessed at.
    """
    start = time.perf_counter()
    connect_start = time.perf_counter()
    with db.connect(settings.db) as conn:
        connect_ms = (time.perf_counter() - connect_start) * 1000
        list_start = time.perf_counter()
        sessions = db.list_sessions(conn, include_children=include_children)
        list_ms = (time.perf_counter() - list_start) * 1000
    panes_start = time.perf_counter()
    panes = [] if settings.no_state else list_panes()
    panes_ms = (time.perf_counter() - panes_start) * 1000
    mapping = resolve(sessions, panes)
    now_ms = int(time.time() * 1000)
    records = [
        render.Record(session, mapping.get(session.session_id), now_ms)
        for session in sessions
    ]
    records = render.sort_records(records, settings.sort)
    panes_by_id = {pane.pane_id: pane for pane in panes}
    log.log(
        "records",
        sessions=len(sessions),
        connect_ms=round(connect_ms, 1),
        list_ms=round(list_ms, 1),
        panes_ms=round(panes_ms, 1),
        records_ms=round((time.perf_counter() - start) * 1000, 1),
    )
    return records, panes_by_id


def _use_color() -> bool:
    """Return whether ANSI color should be emitted (honors ``$NO_COLOR``)."""
    return not os.environ.get("NO_COLOR")


def _print_list(
    records: list[render.Record], args: argparse.Namespace, palette: theme.Palette
) -> None:
    """Write the non-interactive listing in the requested format."""
    if args.json:
        for record in records:
            print(render.json_record(record))
    elif args.nul:
        sys.stdout.write("".join(render.nul_record(record) for record in records))
    else:
        color = _use_color() and sys.stdout.isatty()
        sys.stdout.write(render.human_table(records, palette, color=color))


def _render_lines(
    records: list[render.Record], palette: theme.Palette, color: bool
) -> str:
    """Render ``records`` as the newline-terminated fzf input stream."""
    layout = render.layout_for(records)
    return "".join(
        render.fzf_line(record, layout, palette, color=color) + "\n" for record in records
    )


def _preview_command(db_path: Path) -> str:
    """Build the shell snippet fzf runs to preview the highlighted session."""
    exe = shutil.which("ocjump") or f"{shlex.quote(sys.executable)} -m ocjump"
    return f"{exe} --preview {{6}} --db {shlex.quote(str(db_path))}"


def _pick(
    records: list[render.Record],
    db_path: Path,
    palette: theme.Palette,
    search: str = config.DEFAULT_SEARCH,
    color: bool = True,
) -> Selection | None:
    """Run fzf and return the chosen key + lines, or ``None`` on abort."""
    layout = render.layout_for(records)
    input_data = _render_lines(records, palette, color)
    key_help = render.colorize(KEY_HELP, palette, "keys") if color else KEY_HELP
    header = f"{render.fzf_header(layout)}\n{key_help}"
    argv = [
        "fzf",
        f"--delimiter={render.FIELD_SEP}",
        f"--with-nth={render.FZF_VISIBLE_FIELDS}",
        f"--preview={_preview_command(db_path)}",
        "--preview-window=right:50%:wrap:border-left",
        f"--preview-label={PREVIEW_LABEL}",
        "--border=rounded",
        f"--border-label={BORDER_LABEL}",
        f"--header={header}",
        f"--tabstop={FZF_TABSTOP}",
        "--multi",
        f"--expect={','.join(ACTION_KEYS)}",
    ]
    if search == "text":
        # Restrict matching to directory (4) + title (5) so typing "working"
        # no longer matches every row via the state label.
        argv.append(f"--nth={SEARCH_TEXT_FIELDS}")
    if color:
        argv += ["--ansi", f"--color={palette.fzf_color}"]
    # Capture only stdout (the selection). fzf renders its interface on stderr,
    # so stderr MUST be inherited from the caller (the tmux popup); piping it
    # would swallow the UI and leave the popup blank.
    log.log("pick_spawn", items=len(records))
    pick_start = time.perf_counter()
    proc = subprocess.run(argv, input=input_data, stdout=subprocess.PIPE, text=True)
    log.log(
        "pick_exit",
        rc=proc.returncode,
        pick_ms=round((time.perf_counter() - pick_start) * 1000, 1),
    )
    if proc.returncode != 0:
        return None
    # With --expect, fzf prints the pressed key first (empty for Enter), then
    # one line per selected item.
    lines = proc.stdout.split("\n")
    key = lines[0] if lines else ""
    chosen = [line for line in lines[1:] if line]
    if not chosen:
        return None
    return Selection(key, chosen)


def _confirm(prompt: str, lines: list[str]) -> bool:
    """Ask a yes/no confirmation on a second fzf screen (Enter=yes, Esc=no).

    Keeping the confirmation inside the popup means a whole key press is not
    swallowed by tmux's client-level prompt, and the picker can refresh after.
    """
    stripped = [render.strip_ansi(line) for line in lines]
    argv = ["fzf", f"--header={prompt}", "--no-info"]
    log.log("confirm_spawn", items=len(stripped))
    confirm_start = time.perf_counter()
    proc = subprocess.run(
        argv, input="\n".join(stripped) + "\n", stdout=subprocess.PIPE, text=True
    )
    log.log(
        "confirm_exit",
        rc=proc.returncode,
        confirm_ms=round((time.perf_counter() - confirm_start) * 1000, 1),
    )
    return proc.returncode == 0


def _notify(message: str, runner: Runner = run) -> None:
    """Show ``message`` briefly in the tmux status line (best effort, no raise).

    A popup disappears with the command, so stderr is useless for feedback;
    the status line survives long enough for the user to read the outcome.
    """
    runner(["tmux", "display-message", "-d", NOTIFY_MS, f"ocjump: {message}"])


def _handle_selection(selected: str, panes_by_id: dict[str, Pane], settings: Settings) -> int:
    """Act on an fzf selection: jump to its pane or open the session.

    Every outcome is logged and surfaced: silent failure is indistinguishable
    from "nothing happened" once the popup closes.
    """
    fields = selected.split(render.FIELD_SEP)
    if len(fields) != 7:
        log.log("bad_selection", nfields=len(fields), line=selected)
        _notify("unexpected selection (see log)")
        return 1
    session_id, pane_id = fields[5], fields[6]
    log.log("selection", session_id=session_id, pane_id=pane_id)

    if pane_id and pane_id in panes_by_id:
        results = jump(panes_by_id[pane_id])
        codes = [result.returncode for result in results]
        log.log("jump", pane_id=pane_id, codes=codes)
        failures = [result for result in results if result.returncode != 0]
        if not failures:
            return 0
        _notify(f"jump failed ({failures[0].returncode}): {failures[0].stderr.strip()}")
        return 1

    try:
        with db.connect(settings.db) as conn:
            session = db.get_session(conn, session_id)
    except (FileNotFoundError, KeyError, OSError, db.SchemaError) as exc:
        log.log("open_error", session_id=session_id, error=repr(exc))
        _notify(f"open failed: {exc}")
        return 1

    if not Path(session.directory).is_dir():
        log.log("open_missing_dir", session_id=session_id, directory=session.directory)
        _notify(f"directory no longer exists: {session.directory}")
        return 1

    resolved = resolve_command(settings.command)
    if resolved is None:
        log.log("command_not_found", command=settings.command)
        _notify(f"command not found: {settings.command} (set --command)")
        return 1

    label, results = open_session(
        session, resolved, settings.open_action, settings.session_prefix
    )
    created = results[0]
    if created.returncode != 0:
        log.log(
            "open_failed",
            session_id=session_id,
            code=created.returncode,
            stderr=created.stderr.strip(),
        )
        _notify(f"open failed ({created.returncode}): {created.stderr.strip()}")
        return 1
    log.log(
        "open_ok",
        session_id=session_id,
        directory=session.directory,
        action=settings.open_action,
        target=label,
    )
    _notify(f"opened {session.title} ({settings.open_action} {label})")
    return 0


def _field(line: str, index: int) -> str:
    """Return the 1-based display field ``index`` of an fzf line (or ``""``)."""
    fields = line.split(render.FIELD_SEP)
    return fields[index - 1] if len(fields) >= index else ""


def _open_forked(selection: Selection, settings: Settings) -> str:
    """Open a fork of the first selected session in a new tmux session."""
    session_id = _field(selection.lines[0], 6)
    try:
        with db.connect(settings.db) as conn:
            session = db.get_session(conn, session_id)
    except (FileNotFoundError, KeyError, OSError, db.SchemaError) as exc:
        log.log("fork_error", session_id=session_id, error=repr(exc))
        _notify(f"fork failed: {exc}")
        return "refresh"
    resolved = resolve_command(settings.command)
    if resolved is None:
        log.log("command_not_found", command=settings.command)
        _notify(f"command not found: {settings.command} (set --command)")
        return "refresh"
    label, results = open_session(
        session, resolved, "session", settings.session_prefix, fork=True
    )
    created = results[0]
    if created.returncode != 0:
        log.log(
            "fork_failed",
            session_id=session_id,
            code=created.returncode,
            stderr=created.stderr.strip(),
        )
        _notify(f"fork failed ({created.returncode}): {created.stderr.strip()}")
        return "refresh"
    log.log("fork_ok", session_id=session_id, target=label)
    _notify(f"forked {session.title} ({label})")
    return "exit"


def _delete_selected(selection: Selection, settings: Settings) -> str:
    """Delete the selected sessions (delegated to ``opencode session delete``)."""
    ids = [_field(line, 6) for line in selection.lines]
    if not _confirm(f"Delete {len(ids)} session(s)?  Enter=yes  Esc=no", selection.lines):
        return "refresh"
    resolved = resolve_command(settings.command)
    if resolved is None:
        log.log("command_not_found", command=settings.command)
        _notify(f"command not found: {settings.command} (set --command)")
        return "refresh"
    _notify(f"deleting {len(ids)} session(s)…")
    delete_start = time.perf_counter()
    results = delete_sessions(resolved, ids)
    delete_ms = (time.perf_counter() - delete_start) * 1000
    codes = [result.returncode for result in results]
    log.log("delete", count=len(ids), codes=codes, delete_ms=round(delete_ms, 1))
    if all(code == 0 for code in codes):
        _notify(f"deleted {len(ids)} session(s)")
    else:
        failed = [result for result in results if result.returncode != 0]
        _notify(f"delete failed ({failed[0].returncode}): {failed[0].stderr.strip()}")
    return "refresh"


def _close_selected(
    selection: Selection, panes_by_id: dict[str, Pane], settings: Settings
) -> str:
    """Close the tmux session(s) hosting the selection, after confirmation."""
    existing = existing_session_names()
    names: list[str] = []
    for line in selection.lines:
        pane_id = _field(line, 7)
        if pane_id and pane_id in panes_by_id:
            name = panes_by_id[pane_id].session_name
        else:
            name = session_name(_field(line, 5), (), settings.session_prefix)
            if name not in existing:
                continue
        if name not in names:
            names.append(name)
    if not names:
        _notify("no tmux session to close for the selection")
        return "refresh"
    prompt = f"Close {len(names)} tmux session(s)?  Enter=yes  Esc=no"
    if not _confirm(prompt, selection.lines):
        return "refresh"
    results = close_sessions(names)
    codes = [result.returncode for result in results]
    log.log("close", names=names, codes=codes)
    if all(code == 0 for code in codes):
        _notify(f"closed {len(names)} tmux session(s)")
    else:
        failed = [result for result in results if result.returncode != 0]
        _notify(f"close failed ({failed[0].returncode}): {failed[0].stderr.strip()}")
    return "refresh"


def _sessions_by_id(settings: Settings) -> dict[str, db.Session]:
    """Return selectable sessions keyed by id (for close target derivation)."""
    try:
        with db.connect(settings.db) as conn:
            return {session.session_id: session for session in db.list_sessions(conn)}
    except (FileNotFoundError, OSError, db.SchemaError):
        return {}


def _action_fork(session_ids: list[str], settings: Settings) -> None:
    """Fork the first selected session into a new tmux session (fzf callback)."""
    if not session_ids:
        return
    session_id = session_ids[0]
    try:
        with db.connect(settings.db) as conn:
            session = db.get_session(conn, session_id)
    except (FileNotFoundError, KeyError, OSError, db.SchemaError) as exc:
        log.log("fork_error", session_id=session_id, error=repr(exc))
        _notify(f"fork failed: {exc}")
        return
    resolved = resolve_command(settings.command)
    if resolved is None:
        log.log("command_not_found", command=settings.command)
        _notify(f"command not found: {settings.command} (set --command)")
        return
    label, results = open_session(
        session, resolved, "session", settings.session_prefix, fork=True
    )
    created = results[0]
    log.log("fork", session_id=session_id, code=created.returncode, target=label)
    if created.returncode != 0:
        _notify(f"fork failed ({created.returncode}): {created.stderr.strip()}")
    else:
        _notify(f"forked {session.title} ({label})")


def _action_delete(session_ids: list[str], settings: Settings) -> None:
    """Delete the selected sessions (fzf callback)."""
    if not session_ids:
        return
    resolved = resolve_command(settings.command)
    if resolved is None:
        log.log("command_not_found", command=settings.command)
        _notify(f"command not found: {settings.command} (set --command)")
        return
    _notify(f"deleting {len(session_ids)} session(s)…")
    start = time.perf_counter()
    results = delete_sessions(resolved, session_ids)
    codes = [result.returncode for result in results]
    log.log(
        "delete",
        count=len(session_ids),
        codes=codes,
        delete_ms=round((time.perf_counter() - start) * 1000, 1),
    )
    failed = [result for result in results if result.returncode != 0]
    if failed:
        _notify(f"delete failed ({failed[0].returncode}): {failed[0].stderr.strip()}")
    else:
        _notify(f"deleted {len(session_ids)} session(s)")


def _action_close(session_ids: list[str], settings: Settings) -> None:
    """Close the tmux session(s) hosting the selection (fzf callback)."""
    if not session_ids:
        return
    _, panes_by_id = _records(settings, include_children=False)
    bound = {pane.bound_session_id: pane for pane in panes_by_id.values()}
    sessions = _sessions_by_id(settings)
    existing = existing_session_names()
    names: list[str] = []
    for session_id in session_ids:
        pane = bound.get(session_id)
        if pane is not None:
            name = pane.session_name
        else:
            session = sessions.get(session_id)
            name = session_name(session.title, (), settings.session_prefix) if session else ""
            if name not in existing:
                continue
        if name and name not in names:
            names.append(name)
    if not names:
        _notify("no tmux session to close for the selection")
        return
    results = close_sessions(names)
    codes = [result.returncode for result in results]
    log.log("close", names=names, codes=codes)
    failed = [result for result in results if result.returncode != 0]
    if failed:
        _notify(f"close failed ({failed[0].returncode}): {failed[0].stderr.strip()}")
    else:
        _notify(f"closed {len(names)} tmux session(s)")


def _action_contract(action: str, session_ids: list[str], settings: Settings) -> None:
    """Run a bound action; only forks/delete/close are valid callbacks."""
    log.log("action", name=action, count=len(session_ids))
    if action == "fork":
        _action_fork(session_ids, settings)
    elif action == "delete":
        _action_delete(session_ids, settings)
    elif action == "close":
        _action_close(session_ids, settings)


def _dispatch(
    selection: Selection, panes_by_id: dict[str, Pane], settings: Settings
) -> str:
    """Perform the action bound to the pressed key; return ``refresh`` or ``exit``."""
    if selection.key == "":
        code = _handle_selection(selection.lines[0], panes_by_id, settings)
        return "exit" if code == 0 else "refresh"
    if selection.key == "ctrl-f":
        return _open_forked(selection, settings)
    if selection.key == "ctrl-d":
        return _delete_selected(selection, settings)
    if selection.key == "ctrl-k":
        return _close_selected(selection, panes_by_id, settings)
    return "refresh"


def _load_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI flags over the config file into effective settings."""
    cfg = config.load()
    return Settings(
        db=db.resolve_db_path(args.db or cfg.db, probe=db.probe_db_path),
        command=args.command or cfg.command,
        open_action=args.open or cfg.open_action,
        session_prefix=cfg.session_prefix,
        theme=cfg.theme,
        sort=args.sort or cfg.sort,
        search=cfg.search,
        no_state=args.no_state,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the ``ocjump`` CLI and return a process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        settings = _load_settings(args)
        palette = theme.load(settings.theme)
    except (config.ConfigError, theme.ThemeError) as exc:
        print(f"ocjump: {exc}", file=sys.stderr)
        _notify(f"config error: {exc}")
        return 1

    if args.list_lines:
        records, _ = _records(settings, args.all)
        sys.stdout.write(_render_lines(records, palette, color=_use_color()))
        return 0

    if args.action:
        _action_contract(args.action, args.session, settings)
        return 0

    if args.preview:
        log.log(
            "preview_start",
            session=args.preview,
            startup_ms=round((time.perf_counter() - _IMPORT_TIME) * 1000, 1),
        )
        preview_start = time.perf_counter()
        with db.connect(settings.db) as conn:
            session = db.get_session(conn, args.preview)
            print(db.describe(conn, session))
        log.log(
            "preview",
            session=args.preview,
            preview_ms=round((time.perf_counter() - preview_start) * 1000, 1),
        )
        return 0

    try:
        records, panes_by_id = _records(settings, args.all)
    except (FileNotFoundError, sqlite3.Error, db.SchemaError) as exc:
        log.log("startup_error", error=repr(exc))
        _notify(f"cannot read store: {exc}")
        return 1
    if args.list or args.json or args.nul:
        _print_list(records, args, palette)
        return 0

    if not shutil.which("fzf"):
        print("ocjump: fzf not found on PATH", file=sys.stderr)
        return 1
    while True:
        log.log("start", sessions=len(records), panes=len(panes_by_id))
        selection = _pick(
            records, settings.db, palette, settings.search, color=_use_color()
        )
        if selection is None:
            log.log("cancelled")
            return 0
        log.log("picked", key=selection.key, count=len(selection.lines))
        if _dispatch(selection, panes_by_id, settings) == "exit":
            return 0
        try:
            records, panes_by_id = _records(settings, args.all)
        except (FileNotFoundError, sqlite3.Error, db.SchemaError) as exc:
            log.log("refresh_error", error=repr(exc))
            _notify(f"cannot read store: {exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
