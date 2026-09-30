# AGENTS.md

## Project

`opencode-session-jump` — a fuzzy picker (`ocjump`) that lists every persisted
OpenCode session and, on selection, either **jumps** to the tmux pane already
displaying that session or **opens** it in a dedicated new tmux session.

The live state (`working` / `waiting` / `done` / `error`) is published by a
dedicated OpenCode plugin, `plugin/oc-state.js`, which writes tmux pane options
(`@oc_session_id`, `@oc_state`, `@oc_state_at`). `ocjump` reads those options.

## Commands

```bash
./install.sh                 # symlink ~/.local/bin/ocjump + install the plugin
ocjump                       # interactive picker (run inside tmux)
ocjump --list                # non-interactive listing
ocjump --list --json         # NDJSON listing

PYTHONPATH=src python -m unittest discover -s tests -v   # unit + integration tests
ruff check .                              # lint (docstrings enforced via D rules)
node --check plugin/oc-state.js           # syntax-check the companion plugin
```

## Working procedure

**Definition of green.** A change is only done when all three pass from the repo
root:

```bash
ruff check .
PYTHONPATH=src python -m unittest discover -s tests
node --check plugin/oc-state.js
```

Run this block after **every** batch of edits and before writing any summary.
Never present or commit an intermediate red state (no truncated edits, no
"will fix next").

**Change discipline.**

- One concern per edit; keep diffs small and atomic.
- When changing a shared signature, update its callers *and* tests, then run the
  green block before touching anything else — no cascading unverified refactors.
- For refactors, adjust the tests first (they go red), then wire the code.
- Wrap at 100 columns (`ruff` E501); do not let line-length errors accumulate.

**Faithful reproduction (tmux / opencode / PATH).**

- An isolated `tmux -L <socket>` server inherits the caller's environment. Set
  `PATH` explicitly (`env -u TMUX -u TMUX_PANE PATH=... tmux -L <socket> ...`) or
  you will mask real bugs — a tmux server whose `PATH` lacks `~/.opencode/bin`
  makes `opencode` "command not found" in a new session.
- Always start throwaway servers with `-f /dev/null`
  (`tmux -L <socket> -f /dev/null new-session …`) and `kill-server` in teardown.
  Without the empty config the server reads the user's `~/.tmux.conf` and runs
  TPM and every plugin; `tmux-copycat`'s `list-keys` then starts servers on
  sockets being torn down, each re-reading the config and re-running TPM — an
  infinite fork storm that pins every CPU core.
- Reproduce in the **same context as the user**: run the picker inside a real
  `display-popup` (`tmux display-popup -E -w 80% -h 75% 'ocjump'`), not only in a
  pane. Verify with the real store (read-only) and the real executable.
- Check the environment before guessing: `tmux show-environment -g PATH`,
  `man fzf` (its UI is drawn on **stderr**, `--tabstop` defaults to 8, `--color`
  accepts `#rrggbb`), and OpenCode's own logs under
  `~/.local/share/opencode/log/`.

**Diagnostics.** Failures inside a closing popup are invisible: surface them with
`tmux display-message` and append an NDJSON record to
`~/.local/state/ocjump/ocjump.log` (see `src/ocjump/log.py`).

## Conventions

- Python 3.10+, **standard library only** at runtime (sqlite3, subprocess,
  argparse, dataclasses).
- Every function and module MUST carry a PEP 257 docstring (enforced by `ruff`
  with the `D` rules). Document contract and *why*, not line-by-line *how*.
- Read the OpenCode store **read-only** (`file:...?mode=ro`, `PRAGMA query_only`)
  so the picker never mutates a database owned by the running `opencode` binary.
  Resolve the path via `--db` → `$OPENCODE_DB` → `opencode db path` (handles the
  channel suffix) → `$XDG_DATA_HOME/opencode/opencode.db`; always validate the
  schema and fail with an actionable `SchemaError` on an unsupported store.
- The session↔pane mapping has two tiers, in order: the plugin-published
  `@oc_session_id`, then a title-prefix + working-directory match accepted only
  when exactly one candidate exists. Ambiguity degrades to "unbound" — never
  guess a jump.
- The action for a closed session is configurable (`session` / `window` /
  `split`) via `--open` or `~/.config/ocjump/config.ini` (INI, stdlib only).
  CLI flags always override the config file.
- Display order is `state · age · directory · title`: the short directory goes
  before the long title so it stays visible. Colors (state glyph + fzf theme)
  honor `NO_COLOR`; machine formats (`--json`, `-0`) must never contain ANSI.
- Colors live in `ocjump.theme` (palettes), never in `ocjump.state`: state
  styles carry a semantic *role* (`success`, `warning`…) that a palette
  resolves. The default palette mirrors OpenCode's TUI theme; `ansi` is the
  truecolor-free fallback.
- `plugin/oc-state.js` subscribes to OpenCode's event API directly; that API is
  the plugin's only coupling to OpenCode, so a change there must be reflected
  in `stateForEvent`.
