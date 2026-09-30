"""Live-state vocabulary shared by the picker and its companion plugin.

The live state of a session is published by ``plugin/oc-state.js`` into the
tmux pane options ``@oc_state`` / ``@oc_state_at``.  This module is the single
owner of the raw values the plugin writes and of their human presentation, so
the two sides can be kept in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass

WORKING = "working"
WAITING_PERMISSION = "waiting-permission"
WAITING_QUESTION = "waiting-question"
DONE = "done"
ERROR = "error"
UNKNOWN = ""

ALL_RAW_VALUES = (
    WORKING,
    WAITING_PERMISSION,
    WAITING_QUESTION,
    DONE,
    ERROR,
    UNKNOWN,
)


@dataclass(frozen=True)
class StateStyle:
    """Presentation of a raw state: a short label, a glyph and a color *role*.

    ``role`` is a semantic name (``success``, ``warning``…) resolved to an
    actual color by a :class:`ocjump.theme.Palette`, so this module stays free
    of any color value.
    """

    label: str
    glyph: str
    role: str


# Labels stay short: the picker aligns columns, so every extra character here
# is taken away from the title column.
_STYLES = {
    WORKING: StateStyle("working", "\u25cf", "success"),  # filled circle
    WAITING_PERMISSION: StateStyle("wait:perm", "\u23f8", "warning"),  # pause
    WAITING_QUESTION: StateStyle("wait:ask", "?", "accent"),
    DONE: StateStyle("done", "\u2713", "secondary"),  # check mark
    ERROR: StateStyle("error", "\u2717", "error"),  # ballot X
    UNKNOWN: StateStyle("unknown", "\u00b7", "muted"),  # middle dot
}


def style(raw: str) -> StateStyle:
    """Return the display style for a raw ``@oc_state`` value.

    Unknown, stale or empty values fall back to the neutral style instead of
    raising, so a plugin from another version can never break the picker.
    """
    return _STYLES.get(raw, _STYLES[UNKNOWN])


# Lower number = needs attention sooner.  Both waiting kinds outrank work in
# progress; finished/errored sessions sink; historical rows sink further.
_PRIORITY = {
    WAITING_PERMISSION: 0,
    WAITING_QUESTION: 0,
    WORKING: 1,
    DONE: 2,
    ERROR: 3,
    UNKNOWN: 4,
}


def priority(raw: str) -> int:
    """Return the attention priority of a raw state (lower = more urgent)."""
    return _PRIORITY.get(raw, _PRIORITY[UNKNOWN])
