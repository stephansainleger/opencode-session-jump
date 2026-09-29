"""Color palettes for the picker (state glyphs and the fzf theme).

Two presets ship: ``opencode`` (the exact dark palette of OpenCode's default
TUI theme, extracted from the binary) and ``ansi`` (safe base-16 colors for
limited terminals).  A palette maps semantic state *roles* to colors, keeping
the state vocabulary (:mod:`ocjump.state`) independent from any palette.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class ThemeError(ValueError):
    """The requested theme name is unknown."""


def _to_sgr(value: str) -> str:
    """Convert a palette color to SGR parameters.

    ``#rrggbb`` becomes a 24-bit foreground sequence; anything else is assumed
    to already be SGR parameters (e.g. ``"1;32"`` or ``"90"``).
    """
    if value.startswith("#") and len(value) == 7:
        red, green, blue = (int(value[index : index + 2], 16) for index in (1, 3, 5))
        return f"38;2;{red};{green};{blue}"
    return value


@dataclass(frozen=True)
class Palette:
    """A named set of state colors plus a ready-to-use fzf ``--color`` spec."""

    name: str
    state_colors: Mapping[str, str]
    fzf_color: str

    def sgr(self, role: str) -> str:
        """Return the SGR parameters for a state ``role`` (never raises)."""
        return _to_sgr(self.state_colors.get(role, self.state_colors["muted"]))


# OpenCode default theme (packages/web theme.json defaults, dark mode).
OPENCODE = Palette(
    name="opencode",
    state_colors={
        "success": "#7fd88f",
        "warning": "#f5a742",
        "accent": "#9d7cd8",
        "secondary": "#5c9cf5",
        "error": "#e06c75",
        "muted": "#808080",
    },
    fzf_color=(
        "fg:#eeeeee,bg:#0a0a0a,fg+:#eeeeee,bg+:#1e1e1e,"
        "hl:#9d7cd8,hl+:#fab283,"
        "header:#808080,info:#808080,separator:#3c3c3c,"
        "prompt:#5c9cf5,query:#eeeeee,pointer:#fab283,marker:#9d7cd8,spinner:#fab283,"
        "border:#484848,label:#fab283,"
        "preview-border:#5c9cf5,preview-label:#5c9cf5,preview-bg:#141414,preview-fg:#eeeeee"
    ),
)

# Safe base-16 palette for terminals without 24-bit color.
ANSI = Palette(
    name="ansi",
    state_colors={
        "success": "1;32",
        "warning": "1;33",
        "accent": "35",
        "secondary": "34",
        "error": "1;31",
        "muted": "90",
    },
    fzf_color=(
        "hl:33,hl+:214,header:8,info:240,separator:59,"
        "prompt:39,pointer:161,marker:161,spinner:148,border:240,label:39,"
        "preview-border:39,preview-label:39"
    ),
)

THEMES: Mapping[str, Palette] = {"opencode": OPENCODE, "ansi": ANSI}


def names() -> tuple[str, ...]:
    """Return the available theme names (for messages and validation)."""
    return tuple(THEMES)


def load(name: str) -> Palette:
    """Return the palette called ``name``.

    Raises:
        ThemeError: no such theme is registered.
    """
    try:
        return THEMES[name]
    except KeyError as exc:
        raise ThemeError(f"unknown theme {name!r}; expected one of {', '.join(names())}") from exc
