# opencode-session-jump

A fuzzy picker over every persisted [OpenCode](https://opencode.ai) session.
Select one and `ocjump` either **jumps** to the tmux pane already displaying
it, or **opens** it in a dedicated new tmux session rooted at the session's
directory. Each row also carries the session's live state — `working`,
`waiting` (permission or question), `done`, `error` — published by a small
companion OpenCode plugin.

> **Platform:** Linux + tmux ≥ 3.2 only (it drives `tmux` and reads the
> OpenCode SQLite store directly). Tested against OpenCode **1.18.x**.

## Why

Sessions pile up quickly: dozens across many projects, some still working,
some blocked on a permission prompt, some long finished. OpenCode itself only
lists past sessions without telling which need you, and switching back to a
running one means hunting through tmux windows. `ocjump` answers one question
fast: **what is running, what is waiting, and where is it?**

## Requirements

- [OpenCode](https://opencode.ai) CLI (V1, tested against 1.18.x).
- `tmux` ≥ 3.2 (for `display-popup`).
- [`fzf`](https://github.com/junegunn/fzf) (the picker; mouse double-click accepts).
- `python3` ≥ 3.10 — **standard library only**, no dependencies.
- `node` is optional, only to run the companion plugin's tests.

## Install (standalone)

```sh
git clone <repo>          # into a directory of your choice
cd opencode-session-jump
./install.sh
```

`install.sh` is idempotent. It creates:

- `~/.local/bin/ocjump` — a shim running `python3 -m ocjump` from this checkout.
- `~/.config/opencode/plugins/oc-state.js` — the live-state plugin.

Add the tmux binding to `~/.tmux.conf`, then reload (`prefix` + `r`):

```tmux
bind j display-popup -E -w 80% -h 75% 'ocjump'
```

Any free prefix key works; `bind -n M-o ...` gives a prefix-less trigger.

Restart OpenCode so the plugin loads (plugins are read at startup).

## Usage

```sh
ocjump                 # interactive picker (run inside tmux)
ocjump --list          # human-readable table
ocjump --list --json   # NDJSON, one object per line
ocjump --list -0       # NUL-separated fields for xargs/while-read
ocjump --preview ID    # render the preview for one session
ocjump --all           # include sub-agent sessions
ocjump --no-state      # skip tmux probing (offline / tests)
ocjump --open split    # action for a closed session: session | window | split
ocjump --command opencode   # executable used to open a session
```

Inside the picker: type to fuzzy-filter, `Enter` (or **double-click**)
accepts, `Esc` cancels. The preview pane shows the session metadata and its
last messages.

Columns are `state · age · directory · title`. Label, age and directory are
padded to fixed widths so every title starts at the same column; the (short)
directory precedes the (often long) title so it always stays visible. A
directory too long for its column is trimmed from the left, keeping the
project name (`…/my-project`). The state glyph is colored (green = working,
yellow = waiting, blue = done, red = error, grey = unknown).

**Colors.** The default `opencode` theme uses OpenCode's own TUI palette
(primary `#fab283`, secondary `#5c9cf5`, accent `#9d7cd8`, success `#7fd88f`,
warning `#f5a742`, error `#e06c75`) in 24-bit color, and styles the fzf chrome:
a rounded border labelled `sessions`, and a **preview pane visually set apart**
by a blue left border and a `preview` label. Set `theme = ansi` in
`config.ini` for a safe base-16 fallback on terminals without truecolor.

Colors follow the [`NO_COLOR`][nc] convention: setting it disables all ANSI
output. The state glyph is the only colored *input* field — machine formats
(`--json`, `-0`) are never colored.

[nc]: https://no-color.org/

### Open action

When the selected session is **not** open in any pane, `ocjump` opens it
according to `--open` (or the config file):

| Action    | Effect                                                        |
| --------- | ------------------------------------------------------------- |
| `session` | *(default)* create a dedicated `oc-<slug>` tmux session and switch to it |
| `window`  | open a new window in the current tmux session                 |
| `split`   | open a pane split in the current tmux window                  |

The OpenCode executable is resolved to an **absolute path** before spawning,
because a tmux popup inherits the *server* PATH which frequently omits
`~/.opencode/bin`.

### States

| Label       | Meaning                                           |
| ----------- | ------------------------------------------------- |
| `working`   | a turn is in progress                             |
| `wait:perm` | blocked on a tool-permission prompt               |
| `wait:ask`  | blocked on a question that needs an answer        |
| `done`      | the last turn completed                           |
| `error`     | the last turn ended in an error                   |
| `unknown`   | no live pane / plugin not loaded (historical row) |

State is live: it is written to the tmux pane by `plugin/oc-state.js` as the
session runs. Historical sessions that are not open in any pane show
`unknown`; `ocjump` deliberately does not guess a live state from the database.

### Output fields

`--json` (NDJSON) and `-0` (NUL-separated) expose the complete record. Fields
are ordered timestamp first, then the common keys:

| Field       | Type              | Meaning                                  |
| ----------- | ----------------- | ---------------------------------------- |
| `updated`   | UTC float (s)     | last session update                      |
| `session_id`| string            | `ses_…` identifier                       |
| `state`     | string            | live state (see above), empty if unbound |
| `state_at`  | UTC float or null | when the state was last published        |
| `pane_id`   | string            | tmux pane id (`%NN`), empty if unbound   |
| `title`     | string            | session title                            |
| `directory` | string            | session working directory                |

The `-0` records are the seven display fields separated by tabs:
`glyph, label, age, directory, title, session_id, pane_id` (never colored).

## Configuration

Optional, under `~/.config/ocjump/config.ini` (override the path with
`$OCJUMP_CONFIG`). CLI flags always win.

```ini
[ocjump]
open = session        ; session | window | split
command = opencode
session_prefix = oc-
theme = opencode      ; opencode | ansi
# db = /path/to/opencode.db
```

| Key              | Default    | Meaning                                  |
| ---------------- | ---------- | ---------------------------------------- |
| `open`           | `session`  | action for a closed session              |
| `command`        | `opencode` | executable used to open a session        |
| `session_prefix` | `oc-`      | prefix for created tmux session names    |
| `theme`          | `opencode` | color palette (`opencode` or `ansi`)     |
| `db`             | *(auto)*   | override the OpenCode store path         |

## How it works

### Data source

Sessions are read **read-only** from the OpenCode SQLite store. The path is
resolved as: `--db` → `$OPENCODE_DB` → `opencode db path` (authoritative,
handles the channel suffix) → `$XDG_DATA_HOME/opencode/opencode.db`
(`opencode-<channel>.db` for non-stable `$OPENCODE_CHANNEL`). The connection
uses `mode=ro` + `PRAGMA query_only=1`, and the schema is validated up-front so
an unsupported OpenCode version fails with a clear message.

### Session ↔ pane mapping

Each OpenCode TUI sets its pane title to `OC | <session title>`. `ocjump`
resolves the displayed session in three tiers, in order:

1. the plugin-published `@oc_session_id` pane option (authoritative);
2. a (possibly truncated) title-prefix match restricted to sessions in the
   same working directory — accepted only when exactly one candidate exists;
3. otherwise the row is simply shown as unbound.

Ambiguity degrades to "unbound" rather than to a wrong jump.

### Live-state plugin

`plugin/oc-state.js` subscribes to OpenCode's event bus and writes three tmux
pane options on the pane running the TUI:

- `@oc_state` — one of `working`, `waiting-permission`, `waiting-question`,
  `done`, `error`;
- `@oc_session_id` — the current session id;
- `@oc_state_at` — epoch seconds of the last transition.

The plugin is a no-op outside tmux. It subscribes to OpenCode's event API
directly, so an OpenCode event-API change may require updating it.

## Development

```sh
ruff check .                                   # lint (docstrings enforced)
PYTHONPATH=src python -m unittest discover -s tests -v
node --check plugin/oc-state.js
```

The suite mixes unit tests with real integration tests: a throwaway tmux
server (isolated socket) exercises `list-panes` parsing, session creation and
pane focusing, and a fake `tmux` shim exercises the node plugin's writing
path. No running OpenCode instance is required.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
