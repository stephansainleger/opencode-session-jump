"""Presentation of session rows: fzf input, human table and NDJSON.

The interactive picker feeds tab-separated rows to fzf, hiding the session and
pane identifiers in the last two fields.  The same :class:`Record` renders the
non-interactive ``--list`` table and the ``--json`` / ``-0`` machine formats,
so the three views can never drift apart.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .db import Session
from .panes import Pane
from .state import priority, style
from .theme import Palette

FIELD_SEP = "\t"
FZF_VISIBLE_FIELDS = "1,2,3,4,5"
ANSI_RESET = "\x1b[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove SGR color codes (needed when re-feeding a line outside fzf)."""
    return _ANSI_RE.sub("", text)


def colorize(text: str, palette: Palette, role: str) -> str:
    """Wrap ``text`` in the palette color for ``role`` (for header/chrome)."""
    return f"\x1b[{palette.sgr(role)}m{text}{ANSI_RESET}"
DIRECTORY_MAX = 30

# Header words set a floor on the column widths so the legend always fits and
# stays aligned with the rows below it.
HEADER_LABEL = "state"
HEADER_AGE = "age"
HEADER_DIRECTORY = "directory"

ELLIPSIS = "\u2026"


@dataclass(frozen=True)
class Layout:
    """Fixed column widths (visible characters) shared by every picker row."""

    label: int
    age: int
    directory: int


def _fit(text: str, width: int, keep_tail: bool = False) -> str:
    """Trim ``text`` to ``width`` characters, adding an ellipsis when cut.

    ``keep_tail`` keeps the end of the string (used for directories, where the
    leaf — the project name — matters most).
    """
    if len(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS[:width]
    if keep_tail:
        return ELLIPSIS + text[-(width - 1) :]
    return text[: width - 1] + ELLIPSIS


def layout_for(records: list[Record]) -> Layout:
    """Compute column widths so every title starts at the same column.

    The label width is the longest state label, the age width the longest age
    token, and the directory width the longest directory capped at
    :data:`DIRECTORY_MAX`.
    """
    if not records:
        return Layout(len(HEADER_LABEL), len(HEADER_AGE), len(HEADER_DIRECTORY))
    label = max(
        [len(style(record.state).label) for record in records] + [len(HEADER_LABEL)]
    )
    age = max(
        [len(format_age(record.now_ms, record.session.time_updated)) for record in records]
        + [len(HEADER_AGE)]
    )
    directory = min(
        max(
            [len(abbreviate_dir(record.session.directory)) for record in records]
            + [len(HEADER_DIRECTORY)]
        ),
        DIRECTORY_MAX,
    )
    return Layout(label, age, directory)


@dataclass(frozen=True)
class Record:
    """One row of the picker: a session, its bound pane and its live state."""

    session: Session
    pane: Pane | None
    now_ms: int

    @property
    def state(self) -> str:
        """Raw ``@oc_state`` value of the bound pane, or ``""``."""
        return self.pane.state if self.pane else ""

    @property
    def pane_id(self) -> str:
        """Tmux pane id currently displaying the session, or ``""``."""
        return self.pane.pane_id if self.pane else ""


def sort_records(records: list[Record], mode: str) -> list[Record]:
    """Order records for display.

    ``recent`` keeps the given order (already newest-first); ``attention`` puts
    the sessions that need you on top, then falls back to recency.
    """
    if mode != "attention":
        return list(records)
    return sorted(
        records, key=lambda record: (priority(record.state), -record.session.time_updated)
    )


def format_age(now_ms: int, then_ms: int) -> str:
    """Render ``then_ms`` as a compact age relative to ``now_ms`` (milliseconds)."""
    seconds = max(0, (now_ms - then_ms) // 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"


def abbreviate_dir(path: str, home: str | None = None) -> str:
    """Replace a leading home directory with ``~`` for compact display."""
    home = home if home is not None else os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def cell_state(record: Record) -> str:
    """Return the plain ``glyph label`` cell text for a record."""
    state_style = style(record.state)
    return f"{state_style.glyph} {state_style.label}"


def _plain_fields(record: Record) -> list[str]:
    """Return the seven fields, ordered so the short directory precedes the title.

    The directory is placed before the (often long) title so it stays visible
    at the left of the picker; ``session_id`` and ``pane_id`` are last so they
    can be hidden by ``--with-nth`` while remaining addressable as ``{6}``/``{7}``.
    """
    state_style = style(record.state)
    return [
        state_style.glyph,
        state_style.label,
        format_age(record.now_ms, record.session.time_updated),
        abbreviate_dir(record.session.directory),
        record.session.title,
        record.session.session_id,
        record.pane_id,
    ]


def fzf_line(
    record: Record, layout: Layout, palette: Palette, color: bool = False
) -> str:
    """Render ``record`` as a fixed-width, tab-separated fzf input line.

    Label, age and directory are padded to ``layout`` so every title starts at
    the same column.  When ``color`` is true the state glyph is wrapped in its
    palette color; only that one-character field is colored, so the padding of
    the other columns is never disturbed.
    """
    state_style = style(record.state)
    glyph = state_style.glyph
    if color:
        glyph = f"\x1b[{palette.sgr(state_style.role)}m{glyph}{ANSI_RESET}"
    directory = _fit(
        abbreviate_dir(record.session.directory), layout.directory, keep_tail=True
    )
    fields = [
        glyph,
        state_style.label.ljust(layout.label),
        format_age(record.now_ms, record.session.time_updated).ljust(layout.age),
        directory.ljust(layout.directory),
        record.session.title,
        record.session.session_id,
        record.pane_id,
    ]
    return FIELD_SEP.join(fields)


def fzf_header(layout: Layout) -> str:
    """Return an fzf header legend aligned with :func:`fzf_line` columns."""
    fields = [
        " ",
        HEADER_LABEL.ljust(layout.label),
        HEADER_AGE.ljust(layout.age),
        HEADER_DIRECTORY.ljust(layout.directory),
        "title",
    ]
    return FIELD_SEP.join(fields)


def nul_record(record: Record) -> str:
    """Render ``record`` as NUL-terminated, tab-separated fields (never colored)."""
    return FIELD_SEP.join(_plain_fields(record)) + "\0"


def json_record(record: Record) -> str:
    """Render ``record`` as one NDJSON object (timestamps as UTC float seconds)."""
    state_at = None
    if record.pane and record.pane.state_at:
        state_at = record.pane.state_at / 1000.0
    payload = {
        "updated": record.session.time_updated / 1000.0,
        "session_id": record.session.session_id,
        "state": record.state,
        "state_at": state_at,
        "pane_id": record.pane_id,
        "title": record.session.title,
        "directory": record.session.directory,
    }
    return json.dumps(payload, ensure_ascii=False)


def human_table(records: list[Record], palette: Palette, color: bool = False) -> str:
    """Render records as a fixed-width table for ``--list`` on a terminal.

    Padding is computed on plain text so ANSI color (state column only, and
    only when ``color`` is true) never misaligns the table.
    """
    if not records:
        return "no sessions\n"
    rows = [
        [
            cell_state(record),
            format_age(record.now_ms, record.session.time_updated),
            abbreviate_dir(record.session.directory),
            record.session.title,
        ]
        for record in records
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = []
    for record, row in zip(records, rows):
        columns = [row[column].ljust(widths[column]) for column in range(len(row))]
        if color:
            columns[0] = f"\x1b[{palette.sgr(style(record.state).role)}m{columns[0]}{ANSI_RESET}"
        lines.append("  ".join(columns).rstrip())
    return "\n".join(lines) + "\n"
