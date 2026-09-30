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
from .actions import jump, open_session, resolve_command
from .panes import Pane, list_panes, resolve
from .runner import Runner, run

FZF_TABSTOP = "2"
SEARCH_TEXT_FIELDS = "4,5"  # directory + title (fields 1..7: glyph,label,age,dir,title,id,pane)
BORDER_LABEL = " sessions "
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
    parser.add_argument("--version", action="version", version=f"ocjump {__version__}")
    return parser


def _records(
    settings: Settings, include_children: bool
) -> tuple[list[render.Record], dict[str, Pane]]:
    """Load sessions, probe panes and pair them into display records."""
    with db.connect(settings.db) as conn:
        sessions = db.list_sessions(conn, include_children=include_children)
    panes = [] if settings.no_state else list_panes()
    mapping = resolve(sessions, panes)
    now_ms = int(time.time() * 1000)
    records = [
        render.Record(session, mapping.get(session.session_id), now_ms)
        for session in sessions
    ]
    records = render.sort_records(records, settings.sort)
    panes_by_id = {pane.pane_id: pane for pane in panes}
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
) -> str | None:
    """Run fzf over ``records`` and return the selected line, or ``None``."""
    layout = render.layout_for(records)
    input_data = "".join(
        render.fzf_line(record, layout, palette, color=color) + "\n" for record in records
    )
    argv = [
        "fzf",
        f"--delimiter={render.FIELD_SEP}",
        f"--with-nth={render.FZF_VISIBLE_FIELDS}",
        f"--preview={_preview_command(db_path)}",
        "--preview-window=right:50%:wrap:border-left",
        f"--preview-label={PREVIEW_LABEL}",
        "--border=rounded",
        f"--border-label={BORDER_LABEL}",
        f"--header={render.fzf_header(layout)}",
        f"--tabstop={FZF_TABSTOP}",
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
    proc = subprocess.run(argv, input=input_data, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip("\n") or None


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

    if args.preview:
        with db.connect(settings.db) as conn:
            session = db.get_session(conn, args.preview)
            print(db.describe(conn, session))
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
    log.log("start", sessions=len(records), panes=len(panes_by_id))
    selected = _pick(records, settings.db, palette, settings.search, color=_use_color())
    if selected is None:
        log.log("cancelled")
        return 0
    log.log("picked", line=selected)
    return _handle_selection(selected, panes_by_id, settings)


if __name__ == "__main__":
    raise SystemExit(main())
