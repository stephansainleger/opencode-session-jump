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
import tempfile
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
ACTION_KEYS = {"ctrl-f": "fork", "ctrl-d": "delete", "ctrl-k": "close"}
ACTIONS = tuple(ACTION_KEYS.values())
CONFIRM_KEY = "ctrl-y"  # fzf's reload clears the multi-selection, so the arm
# key only paints a prompt and the destructive action fires on a second key.
BORDER_LABEL = " sessions "
# Second header line, shown at the bottom of the popup under the column legend.
KEY_HELP = (
    "Enter open · ctrl-f fork · ctrl-d delete · ctrl-k close · ctrl-y confirm · Tab multi"
    " · Esc quit"
)
PREVIEW_LABEL = " preview "
# A tmux status message overlays the popup and defers its repaint until the
# message clears, so this delay caps how long the list stays frozen after an
# action; keep it short.  "deleted N…" is shown once the refresh has happened,
# so the message and the visible change coincide.
NOTIFY_MS = "1000"


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
class PickerResult:
    """What the fzf picker returned: an accepted session, or nothing (abort)."""

    session_id: str = ""
    pane_id: str = ""


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
        "--session", nargs="+", default=[], metavar="SESSION_ID", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--result-file",
        metavar="PATH",
        help=argparse.SUPPRESS,
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


def _exe() -> str:
    """Return the command used to re-invoke ocjump from fzf bindings."""
    return shutil.which("ocjump") or f"{shlex.quote(sys.executable)} -m ocjump"


def _common_args(settings: Settings) -> str:
    """Shell flags reproducing the effective settings for a callback run.

    Only the store path and the two flags that change output matter: the DB,
    ``--no-state`` and a non-default ``--command``.
    """
    parts = ["--db", shlex.quote(str(settings.db))]
    if settings.no_state:
        parts.append("--no-state")
    if settings.command != config.DEFAULT_COMMAND:
        parts += ["--command", shlex.quote(settings.command)]
    return " ".join(parts)


def _preview_command(db_path: Path) -> str:
    """Build the shell snippet fzf runs to preview the highlighted session."""
    return f"{_exe()} --preview {{6}} --db {shlex.quote(str(db_path))}"


def _callback(
    settings: Settings, action: str, sessions: str = "{+6}", result_file: str | None = None
) -> str:
    """Build the ``--action`` callback command applied to ``sessions``.

    ``sessions`` defaults to fzf's marked-rows placeholder; the destructive
    confirmation substitutes the ids captured when the key was armed.  When
    ``result_file`` is given, the callback writes its outcome there instead of
    notifying immediately, so the message can be shown after the list refresh.
    """
    command = f"{_exe()} --action {action} {_common_args(settings)} --session {sessions}"
    if result_file:
        command += f" --result-file {shlex.quote(result_file)}"
    return command


def _reload_command(settings: Settings) -> str:
    """Build the ``--list-lines`` command that refreshes fzf in place."""
    return f"{_exe()} --list-lines {_common_args(settings)}"


def _bindings(settings: Settings, state_dir: Path) -> list[str]:
    """Return one ``--bind`` spec per action key.

    ``fork`` runs immediately (``execute-silent`` never switches screens, so the
    popup is not blanked) then refreshes the list in place (``reload-sync``).

    Destructive keys are a two-step: ``ctrl-d`` / ``ctrl-k`` snapshot the marked
    ids and paint a confirmation in the footer (``transform-header``); the action
    fires only on ``ctrl-y``.  Two keys are needed because ``reload-sync`` clears
    fzf's multi-selection: the arm step must not reload, so the marks (and the
    cursor) stay visible while the user confirms.  The frozen state lives in
    files under ``state_dir`` and the branching is done in the shell snippets.
    """
    fresh = _reload_command(settings)
    header_file = shlex.quote(str(state_dir / "header"))
    result_file = state_dir / "result"
    result = shlex.quote(str(result_file))
    pending = {a: shlex.quote(str(state_dir / f"pending-{a}")) for a in ACTIONS}
    message = {a: shlex.quote(str(state_dir / f"msg-{a}")) for a in ACTIONS}
    callback = {
        a: _callback(
            settings, a, sessions=f"$(cat {pending[a]})", result_file=str(result_file)
        )
        for a in ("delete", "close")
    }
    bindings = [
        f"ctrl-f:execute-silent(rm -f {pending['delete']} {pending['close']}; "
        f"{_callback(settings, 'fork')})+reload-sync({fresh})"
    ]
    for key, action, label in (("ctrl-d", "delete", "Delete"), ("ctrl-k", "close", "Close")):
        other = "close" if action == "delete" else "delete"
        arm = (
            f"set -- {{+6}}; [ $# -gt 0 ] && "
            f"printf '%s\\n' \"$@\" > {pending[action]} && "
            f"printf '%s' \"{label} $# session(s)?  press {CONFIRM_KEY} "
            f"to confirm  ·  Esc to cancel\" > {message[action]} && "
            f"rm -f {pending[other]}"
        )
        paint = (
            f"if [ -e {pending[action]} ]; then cat {message[action]}; "
            f"else cat {header_file}; fi"
        )
        bindings.append(f"{key}:execute-silent({arm})+transform-header({paint})")
    confirm = (
        f"rm -f {result}; "
        f"if [ -e {pending['delete']} ]; then {callback['delete']}; "
        f"rm -f {pending['delete']}; "
        f"elif [ -e {pending['close']} ]; then {callback['close']}; "
        f"rm -f {pending['close']}; fi"
    )
    # The outcome is notified only once the refresh has happened, so the message
    # is a faithful marker of when the visible list actually changed.
    notify = (
        f"if [ -s {result} ]; then tmux display-message -d {NOTIFY_MS} "
        f"\"$(cat {result})\"; rm -f {result}; fi"
    )
    bindings.append(
        f"{CONFIRM_KEY}:execute-silent({confirm})"
        f"+transform-header(cat {header_file})+reload-sync({fresh})"
        f"+execute-silent({notify})"
    )
    return bindings


def _parser_selection(stdout: str) -> PickerResult:
    """Parse fzf output into the accepted session (``--print-query`` adds a line)."""
    for line in stdout.split("\n"):
        fields = line.split(render.FIELD_SEP)
        if len(fields) == 7:
            return PickerResult(session_id=fields[5], pane_id=fields[6])
    return PickerResult()


def _pick(
    records: list[render.Record],
    settings: Settings,
    palette: theme.Palette,
    color: bool = True,
) -> PickerResult:
    """Run the single long-lived fzf picker and return the accepted session.

    Key bindings perform actions in place (execute-silent + reload-sync), so a
    single fzf process serves the whole picker session — no relaunch, no blank
    popup.  Enter accepts (the caller then opens/jumps); Esc aborts.
    """
    layout = render.layout_for(records)
    input_data = _render_lines(records, palette, color)
    key_help = render.colorize(KEY_HELP, palette, "keys") if color else KEY_HELP
    header = f"{render.fzf_header(layout)}\n{key_help}"
    state_dir = Path(tempfile.mkdtemp(prefix="ocjump-"))
    try:
        (state_dir / "header").write_text(header, encoding="utf-8")
        argv = [
            "fzf",
            f"--delimiter={render.FIELD_SEP}",
            f"--with-nth={render.FZF_VISIBLE_FIELDS}",
            "--print-query",
            f"--preview={_preview_command(settings.db)}",
            "--preview-window=right:50%:wrap:border-left",
            f"--preview-label={PREVIEW_LABEL}",
            "--border=rounded",
            f"--border-label={BORDER_LABEL}",
            f"--header={header}",
            f"--tabstop={FZF_TABSTOP}",
            "--multi",
        ]
        argv += [f"--bind={binding}" for binding in _bindings(settings, state_dir)]
        if settings.search == "text":
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
            return PickerResult()
        return _parser_selection(proc.stdout)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def _notify(message: str, runner: Runner = run) -> None:
    """Show ``message`` briefly in the tmux status line (best effort, no raise).

    A popup disappears with the command, so stderr is useless for feedback;
    the status line survives long enough for the user to read the outcome.
    """
    runner(["tmux", "display-message", "-d", NOTIFY_MS, f"ocjump: {message}"])


def _report(message: str, result_file: str | None) -> None:
    """Deliver an action outcome now, or queue it for the post-refresh notify.

    With ``result_file`` set, the message is written there and shown by a later
    binding action (after ``reload-sync``) so the status line never announces
    "deleted" before the visible list actually reflects it.  Without it, the
    message is shown immediately.
    """
    if result_file:
        Path(result_file).write_text(f"ocjump: {message}", encoding="utf-8")
    else:
        _notify(message)


def _open_selected(result: PickerResult, settings: Settings) -> None:
    """Open or jump to the accepted session (best effort; logs the outcome).

    ``Enter`` always means "go there": jump when the session is already shown
    in a pane, otherwise open it with the configured action.
    """
    if not result.session_id:
        return
    log.log("selection", session_id=result.session_id, pane_id=result.pane_id)
    if result.pane_id:
        panes_by_id = {pane.pane_id: pane for pane in list_panes()}
        if result.pane_id in panes_by_id:
            results = jump(panes_by_id[result.pane_id])
            codes = [result_.returncode for result_ in results]
            log.log("jump", pane_id=result.pane_id, codes=codes)
            failures = [result_ for result_ in results if result_.returncode != 0]
            if failures:
                _notify(f"jump failed ({failures[0].returncode}): {failures[0].stderr.strip()}")
            return
    try:
        with db.connect(settings.db) as conn:
            session = db.get_session(conn, result.session_id)
    except (FileNotFoundError, KeyError, OSError, db.SchemaError) as exc:
        log.log("open_error", session_id=result.session_id, error=repr(exc))
        _notify(f"open failed: {exc}")
        return
    if not Path(session.directory).is_dir():
        log.log("open_missing_dir", session_id=result.session_id, directory=session.directory)
        _notify(f"directory no longer exists: {session.directory}")
        return
    resolved = resolve_command(settings.command)
    if resolved is None:
        log.log("command_not_found", command=settings.command)
        _notify(f"command not found: {settings.command} (set --command)")
        return
    label, results = open_session(
        session, resolved, settings.open_action, settings.session_prefix
    )
    created = results[0]
    if created.returncode != 0:
        log.log(
            "open_failed",
            session_id=result.session_id,
            code=created.returncode,
            stderr=created.stderr.strip(),
        )
        _notify(f"open failed ({created.returncode}): {created.stderr.strip()}")
        return
    log.log(
        "open_ok",
        session_id=result.session_id,
        directory=session.directory,
        action=settings.open_action,
        target=label,
    )
    _notify(f"opened {session.title} ({settings.open_action} {label})")


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


def _action_delete(
    session_ids: list[str], settings: Settings, result_file: str | None = None
) -> None:
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
        _report(f"delete failed ({failed[0].returncode}): {failed[0].stderr.strip()}", result_file)
    else:
        _report(f"deleted {len(session_ids)} session(s)", result_file)


def _action_close(
    session_ids: list[str], settings: Settings, result_file: str | None = None
) -> None:
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
        _report("no tmux session to close for the selection", result_file)
        return
    results = close_sessions(names)
    codes = [result.returncode for result in results]
    log.log("close", names=names, codes=codes)
    failed = [result for result in results if result.returncode != 0]
    if failed:
        _report(f"close failed ({failed[0].returncode}): {failed[0].stderr.strip()}", result_file)
    else:
        _report(f"closed {len(names)} tmux session(s)", result_file)


def _action_contract(
    action: str, session_ids: list[str], settings: Settings, result_file: str | None = None
) -> None:
    """Run a bound action; only forks/delete/close are valid callbacks."""
    log.log("action", name=action, count=len(session_ids))
    if action == "fork":
        _action_fork(session_ids, settings)
    elif action == "delete":
        _action_delete(session_ids, settings, result_file)
    elif action == "close":
        _action_close(session_ids, settings, result_file)


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
        _action_contract(args.action, args.session, settings, args.result_file)
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
        records, _ = _records(settings, args.all)
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
    # A single fzf process serves the whole picker: actions run in place
    # (execute-silent + reload-sync) and Enter returns the accepted session.
    log.log("start", sessions=len(records))
    result = _pick(records, settings, palette, color=_use_color())
    if not result.session_id:
        log.log("cancelled")
        return 0
    _open_selected(result, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
