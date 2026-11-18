"""Presentation of session rows and the preview pane.

The interactive picker feeds tab-separated rows to fzf, hiding the session and
pane identifiers in the last two fields.  The same :class:`Record` renders the
non-interactive ``--list`` table and the ``--json`` / ``-0`` machine formats,
so the three views can never drift apart.  :func:`preview_lines` renders the
fixed metadata card and recent messages shown in fzf's preview window.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from .db import Preview, PreviewMessage, Session, is_direct_message, model_label
from .panes import Pane
from .state import (
    DONE,
    ERROR,
    UNKNOWN,
    WAITING_PERMISSION,
    WAITING_QUESTION,
    WORKING,
    priority,
    style,
)
from .theme import Palette

FIELD_SEP = "\t"
FZF_VISIBLE_FIELDS = "1,2,3,4,5"
ANSI_RESET = "\x1b[0m"
ARCHIVED_SUFFIX = " (arch)"
STATUS_STATES = (
    WORKING,
    WAITING_PERMISSION,
    WAITING_QUESTION,
    DONE,
    ERROR,
    UNKNOWN,
)
PREVIEW_TEXT_LIMIT = 400
PREVIEW_DEFAULT_WIDTH = 80
PREVIEW_MIN_WIDTH = 40
PREVIEW_MAX_WIDTH = 200
PREVIEW_LABEL_WIDTH = 5
PREVIEW_INDENT = "  "
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


def display_title(session: Session) -> str:
    """Return the title with an archived marker when the session is archived.

    The raw title stays untouched in :class:`Session` because pane matching and
    the OpenCode store use it verbatim; only human-facing views add the suffix.
    """
    return f"{session.title}{ARCHIVED_SUFFIX}" if session.archived else session.title


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
        display_title(record.session),
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
        display_title(record.session),
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
        "archived": record.session.archived,
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
            display_title(record.session),
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


def _tmux_colour(value: str) -> str:
    """Return a tmux style sequence for a hex palette value, or ``""``.

    tmux styles accept ``#rrggbb`` but not SGR parameters, so the ``ansi``
    palette (which stores SGR codes) deliberately yields plain text.
    """
    if value.startswith("#") and len(value) == 7:
        return f"#[fg={value}]"
    return ""


def status_line(
    records: list[Record], palette: Palette | None = None, color: bool = True
) -> str:
    """Render one compact status-line segment such as ``OC:49 ●7 ⏸1 ✓41``.

    Categories with no session are omitted so the segment stays short.  When
    ``color`` is true and the palette holds hex colors, every glyph is wrapped
    in a tmux style sequence and reset immediately, leaving the count plain.
    """
    counts: dict[str, int] = {}
    for record in records:
        counts[record.state] = counts.get(record.state, 0) + 1
    parts = [f"OC:{len(records)}"]
    for raw in STATUS_STATES:
        count = counts.get(raw, 0)
        if count == 0:
            continue
        state_style = style(raw)
        token = f"{state_style.glyph}{count}"
        if color and palette is not None:
            colour = _tmux_colour(palette.state_colors.get(state_style.role, ""))
            if colour:
                token = f"{colour}{state_style.glyph}#[default]{count}"
        parts.append(token)
    return " ".join(parts)


def preview_width(width: int | None = None) -> int:
    """Return the usable preview width, honoring ``$FZF_PREVIEW_COLUMNS``.

    A preview command is not attached to a tty, so the width comes from fzf's
    exported variable; tests can pass it explicitly.  The result is clamped so
    a tiny or absurd value cannot produce broken rules.
    """
    if width is None:
        raw = os.environ.get("FZF_PREVIEW_COLUMNS", "")
        try:
            width = int(raw)
        except (TypeError, ValueError):
            width = PREVIEW_DEFAULT_WIDTH
    return max(PREVIEW_MIN_WIDTH, min(PREVIEW_MAX_WIDTH, width))


def _clip_text(text: str, limit: int = PREVIEW_TEXT_LIMIT) -> str:
    """Trim long text at a word boundary and mark the omission."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return f"{cut.rstrip()} {ELLIPSIS}"


def _preview_role_color(role: str) -> str:
    """Return the palette role for a message author."""
    return {"user": "accent", "assistant": "secondary"}.get(role, "muted")


def _box_top(session: Session, width: int, palette: Palette, color: bool) -> str:
    """Return the top border of the metadata card, with the session title."""
    title = _fit(display_title(session), max(1, width - 8))
    dashes = max(1, width - len(title) - 5)
    if color:
        title = colorize(title, palette, "keys")
    return f"╭─ {title} {'─' * dashes}╮"


def _box_bottom(width: int) -> str:
    """Return the closing border of the metadata card."""
    return f"╰{'─' * max(1, width - 2)}╯"


def _box_line(
    label: str, value: str, width: int, palette: Palette, color: bool, keep_tail: bool = False
) -> str:
    """Return one padded metadata row inside the card."""
    content_width = max(1, width - 4)
    label_text = label.ljust(PREVIEW_LABEL_WIDTH)
    value_text = _fit(value, max(1, content_width - len(label_text) - 1), keep_tail)
    plain = f"{label_text} {value_text}".ljust(content_width)
    if color:
        plain = colorize(label_text, palette, "muted") + plain[len(label_text) :]
    return f"│ {plain} │"


def _preview_header(
    session: Session, preview: Preview, palette: Palette, color: bool, width: int
) -> list[str]:
    """Return the six fixed lines kept visible at the top of the preview."""
    direct = sum(1 for message in preview.messages if is_direct_message(message))
    if preview.total == 0:
        summary = "no messages"
    elif direct == 0:
        summary = "no direct messages"
    else:
        summary = f"last {direct} direct message" + ("" if direct == 1 else "s")
    if preview.omitted:
        summary += f" · {preview.omitted} older messages omitted"
    summary = _fit(summary, width)
    if color:
        summary = colorize(summary, palette, "muted")
    return [
        _box_top(session, width, palette, color),
        _box_line("id", session.session_id, width, palette, color),
        _box_line("dir", abbreviate_dir(session.directory), width, palette, color, True),
        _box_line(
            "agent",
            f"{session.agent or '?'} · {model_label(session.model)}",
            width,
            palette,
            color,
        ),
        _box_bottom(width),
        summary,
    ]


def _preview_rule(
    role: str, age: str, width: int, palette: Palette, color: bool
) -> str:
    """Return a full-width rule announcing one message and its age."""
    label = role or "?"
    plain_prefix = f"── {label} · {age} "
    if len(plain_prefix) >= width:
        label = _fit(label, max(1, width - 8))
        plain_prefix = f"── {label} · {age} "
    fill = max(3, width - len(plain_prefix))
    if color:
        label = colorize(label, palette, _preview_role_color(role))
    return f"── {label} · {age} {'─' * fill}"


def _group_preview_messages(
    messages: tuple[PreviewMessage, ...],
) -> list[list[PreviewMessage]]:
    """Merge consecutive assistant messages into one display block.

    OpenCode emits one assistant message per step (reasoning/tool), often many
    in a row before the final text.  Treating them as one block removes the
    per-step rules that used to dominate the preview while keeping user turns
    separate.
    """
    blocks: list[list[PreviewMessage]] = []
    for message in messages:
        if (
            blocks
            and message.role == "assistant"
            and blocks[-1][0].role == "assistant"
        ):
            blocks[-1].append(message)
        else:
            blocks.append([message])
    return blocks


def _text_lines(message: PreviewMessage) -> list[str]:
    """Render the user-facing text parts of one message as indented lines."""
    lines: list[str] = []
    for part in message.parts:
        if part.kind != "text":
            continue
        text = _clip_text(part.text)
        lines.extend(
            f"{PREVIEW_INDENT}{line}" if line else "" for line in text.splitlines()
        )
    return lines


def _tool_summary(messages: list[PreviewMessage]) -> str:
    """Return a compact ``bash×3, edit×1`` count of the block's tool calls."""
    counts: dict[str, int] = {}
    for message in messages:
        for part in message.parts:
            if part.kind == "tool":
                counts[part.tool] = counts.get(part.tool, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{tool}×{count}" for tool, count in ordered)


def _assistant_summary(
    block: list[PreviewMessage], width: int, palette: Palette, color: bool
) -> str:
    """Return the single muted line standing in for a text-less assistant run."""
    steps = len(block)
    label = f"⋯ {steps} intermediate step" + ("" if steps == 1 else "s")
    tools = _tool_summary(block)
    if tools:
        label += f" · {tools}"
    label = _fit(label, max(1, width - len(PREVIEW_INDENT)))
    if color:
        label = colorize(label, palette, "muted")
    return f"{PREVIEW_INDENT}{label}"


def _block_lines(
    block: list[PreviewMessage],
    width: int,
    palette: Palette,
    color: bool,
    now_ms: int,
) -> list[str]:
    """Render one user turn or one merged assistant block."""
    role = block[0].role
    text_lines: list[str] = []
    for message in block:
        text_lines.extend(_text_lines(message))
    rule = _preview_rule(
        role,
        format_age(now_ms, block[-1].time_created),
        width,
        palette,
        color,
    )
    if role == "assistant":
        if text_lines:
            return ["", rule] + text_lines
        has_steps = any(
            part.kind in {"reasoning", "tool"}
            for message in block
            for part in message.parts
        )
        if has_steps:
            return ["", rule, _assistant_summary(block, width, palette, color)]
        return []
    if text_lines:
        return ["", rule] + text_lines
    return []


def preview_lines(
    session: Session,
    preview: Preview,
    palette: Palette,
    color: bool = True,
    width: int | None = None,
    now_ms: int | None = None,
) -> str:
    """Render the session preview: fixed metadata card plus recent exchanges.

    The message window already reaches back to the configured number of direct
    messages.  Consecutive assistant messages are merged into one block; direct
    text is shown indented, and a block without text becomes one muted summary
    line naming its step count and tool calls, so intermediate work never
    crowds the conversation.  ``now_ms`` makes relative ages deterministic in
    tests.
    """
    width = preview_width(width)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    lines = _preview_header(session, preview, palette, color, width)
    body: list[str] = []
    for block in _group_preview_messages(preview.messages):
        body.extend(_block_lines(block, width, palette, color, now))
    if not body and preview.total:
        body.extend(["", f"{PREVIEW_INDENT}(no text content)"])
    return "\n".join(lines + body)
