"""List OpenCode sessions and jump to (or open) their tmux pane.

The public entry point is :func:`ocjump.__main__.main`; the package is split
into focused modules: ``db`` (read-only store access), ``panes`` (tmux discovery
and session mapping), ``state`` (live-state vocabulary), ``actions`` (tmux
side effects) and ``render`` (fzf lines and previews).
"""

__version__ = "0.1.0"
