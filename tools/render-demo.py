#!/usr/bin/env python3
"""Regenerate the README demo assets (``docs/ocjump.gif`` + ``docs/ocjump.png``).

The recording must show the *real* picker with the *real* theme, so this script
drives the actual executable inside a throwaway tmux server and records it with
``asciinema``:

1. build a minimal OpenCode store (``tests/support.py``) full of plausible
   fictional sessions covering every state;
2. start an isolated tmux server (empty config, explicit PATH) and tag fake
   OpenCode panes with ``@oc_session_id`` / ``@oc_state`` so the state column is
   genuine;
3. run ``asciinema rec`` around a script that opens the popup, filters, moves,
   triggers the delete confirmation and cancels;
4. render the cast to a GIF with ``agg`` using an ad-hoc theme built from
   ``ocjump.theme`` (single source of truth) and the terminal font.

Requires ``asciinema`` and ``agg`` on PATH (see the README's Development section).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
DOCS = REPO / "docs"
SOCKET = "ocjump-demo"
WORK = Path("/tmp/ocjump-demo")
FONT = "DejaVu Sans Mono"
FONT_SIZE = 16
COLS = 120
ROWS = 34

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
import support  # noqa: E402
from ocjump import theme  # noqa: E402

# Fictional sessions: (id, title, directory, age_seconds, state, pane?).
# Ages are relative to the moment the script runs, so the ages always read
# naturally (2m, 5m, 1h…) instead of a fixed snapshot.
DEMO_SESSIONS = [
    ("ses_demo_api", "Add pagination to the /users endpoint", "~/code/api", 130, "working", True),
    ("ses_demo_blog", "Which database should I use?", "~/code/blog", 300, "waiting-question", True),
    (
        "ses_demo_infra",
        "Permission to run docker compose up?",
        "~/code/infra",
        480,
        "waiting-permission",
        True,
    ),
    ("ses_demo_docs", "Fix the broken anchor links", "~/code/docs", 3600, "done", True),
    ("ses_demo_legacy", "Migrate the old config loader", "~/code/legacy", 172800, "", True),
    ("ses_demo_crash", "Investigate the nightly build failure", "~/code/ci", 7200, "error", True),
    ("ses_demo_refactor", "Refactor the billing module", "~/code/billing", 5400, "", True),
]


def log(msg: str) -> None:
    """Print a progress line."""
    print(f"[render-demo] {msg}", flush=True)


def make_store() -> None:
    """Write the throwaway OpenCode store with the fictional sessions."""
    db = WORK / "opencode.db"
    db.unlink(missing_ok=True)
    conn = support.make_db(db)
    now_ms = int(time.time() * 1000)
    for session_id, title, directory, age_s, _state, _pane in DEMO_SESSIONS:
        support.add_session(
            conn,
            session_id,
            title,
            directory.replace("~", str(Path.home())),
            time_updated=now_ms - age_s * 1000,
        )
        support.add_message(
            conn,
            f"{session_id}-m1",
            session_id,
            "user",
            "Continuing from where we left off — here is the context.",
        )
        support.add_message(
            conn,
            f"{session_id}-m2",
            session_id,
            "assistant",
            "Understood. I have the relevant files open and I am ready to proceed.",
        )
    conn.close()


def tmux(*args: str, capture: bool = True):
    """Run a tmux command on the isolated demo socket."""
    return subprocess.run(
        ["tmux", "-L", SOCKET, "-f", "/dev/null", *args],
        capture_output=capture,
        text=True,
        env=DEMO_ENV,
    )


def start_tmux() -> None:
    """Start the isolated server and tag fake OpenCode panes with their state."""
    tmux("kill-server")
    tmux("new-session", "-d", "-s", "demo", "-x", str(COLS), "-y", str(ROWS))
    workdir = WORK / "proj"
    workdir.mkdir(exist_ok=True)
    fake = WORK / "opencode"
    if not fake.exists():
        shutil.copy("/bin/sleep", fake)
        fake.chmod(0o755)
    for index, (session_id, title, _dir, _age, state, pane) in enumerate(DEMO_SESSIONS):
        if not pane:
            continue
        created = tmux(
            "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "demo",
            "-c", str(workdir), "-n", f"oc{index}", f"exec {fake} 600",
        )
        pane_id = created.stdout.strip()
        tmux("set-option", "-p", "-t", pane_id, "@oc_session_id", session_id)
        tmux("set-option", "-p", "-t", pane_id, "@oc_state", state)
        tmux("set-option", "-p", "-t", pane_id, "@oc_state_at", str(int(time.time())))
        tmux("select-pane", "-t", pane_id, "-T", f"OC | {title}")
    tmux("kill-window", "-t", "demo:0")  # remove the empty initial window
    # A borderless-ish status line keeps the shot focused on the popup.
    tmux("set-option", "-g", "status", "off")
    tmux("set-option", "-g", "pane-border-status", "off")
    time.sleep(0.3)


def fake_opencode() -> Path:
    """Return a fake ``opencode`` binary used as ``--command`` (delete is a no-op)."""
    fake = WORK / "fake-opencode"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def record(cast: Path) -> None:
    """Record the picker session into ``cast``.

    ``asciinema`` records the command's stdout, but the popup is drawn on the
    tmux client's tty.  ``tools/_demo_attach.py`` allocates a pty, attaches tmux
    there, relays it to stdout and injects the keystrokes — so the recording
    captures the real popup.
    """
    cast.unlink(missing_ok=True)
    asciinema = shutil.which("asciinema")
    if asciinema is None:
        raise SystemExit("asciinema not found on PATH")
    env = {
        **DEMO_ENV,
        "TERM": "xterm-256color",
        "DEMO_SOCKET": SOCKET,
        "DEMO_COLS": str(COLS),
        "DEMO_ROWS": str(ROWS),
    }
    subprocess.run(
        [
            asciinema, "rec", "--overwrite", "--quiet",
            "--cols", str(COLS), "--rows", str(ROWS), "--idle-time-limit", "2",
            "-c", f"python3 {TOOLS / '_demo_attach.py'}", str(cast),
        ],
        check=True, env=env, cwd=str(REPO),
    )


def render(cast: Path, gif: Path, png: Path, at: str) -> None:
    """Render the cast to a GIF (full) and a frozen frame PNG at time ``at``."""
    agg = shutil.which("agg")
    if agg is None:
        raise SystemExit("agg not found on PATH")
    adhoc = _adhoc_theme()
    base = [
        agg, "--theme", adhoc,
        "--font-family", f"{FONT},Symbols Nerd Font,DejaVu Sans,Noto Color Emoji",
        "--font-size", str(FONT_SIZE), "--line-height", "1.4",
        "--cols", str(COLS), "--rows", str(ROWS),
        "--idle-time-limit", "2", "--fps-cap", "15", "--last-frame-duration", "3",
    ]
    subprocess.run([*base, str(cast), str(gif)], check=True)
    subprocess.run([*base, "--select", at, "--no-loop", str(cast), str(png)], check=True)
    log(f"wrote {gif.relative_to(REPO)} and {png.relative_to(REPO)}")


def _adhoc_theme() -> str:
    """Build the agg ad-hoc theme from the ``opencode`` palette (hex triplets).

    ``agg`` only takes ANSI colors, but the picker emits 24-bit SGR, so only the
    background/foreground defaults matter here; they are read from the palette so
    the GIF's chrome cannot drift from ``theme.py``.
    """
    palette = theme.load("opencode")
    # fzf_color is a comma-separated ``key:#hex`` list; pull bg/fg from it.
    fields = dict(item.split(":", 1) for item in palette.fzf_color.split(","))
    background = fields["bg"].replace("#", "")
    foreground = fields["fg"].replace("#", "")
    # ANSI 0-15 mirror OpenCode's own TUI theme (unused by 24-bit rows, but the
    # base-16 fallback and any bold text resolve here).
    normal = [
        "#1e1e1e", "#e06c75", "#7fd88f", "#f5a742", "#5c9cf5", "#9d7cd8", "#56b6c2", "#eeeeee",
    ]
    bright = [
        "#3c3c3c", "#e06c75", "#7fd88f", "#f5a742", "#5c9cf5", "#9d7cd8", "#56b6c2", "#ffffff",
    ]
    return ",".join([background, foreground] + [c.lstrip("#") for c in normal + bright])


# Environment for every tmux invocation: drop the caller's tmux context so the
# throwaway server is never confused with the user's own.
DEMO_ENV: dict[str, str] = {}


def main() -> int:
    """Entry point: build, record and render the demo."""
    global DEMO_ENV
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", default="6.9", help="time (s) of the frozen PNG frame")
    args = parser.parse_args()

    for tool in ("asciinema", "agg"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH (see README Development)")

    shutil.rmtree(WORK, ignore_errors=True)
    DOCS.mkdir(exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    DEMO_ENV = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    DEMO_ENV["PATH"] = os.environ["PATH"]

    log("building fake store and panes")
    make_store()
    start_tmux()
    fake_opencode()
    # The picker under demo uses the whole tmux popup, so point it at the fake
    # store/command via the binding environment.
    demo_popup = (
        f"ocjump --db {WORK / 'opencode.db'} --command {WORK / 'fake-opencode'}"
    )
    tmux("bind", "j", "display-popup", "-E", "-w", "92%", "-h", "85%", demo_popup)

    cast = TOOLS / "ocjump-demo.cast"
    gif = DOCS / "ocjump.gif"
    png = DOCS / "ocjump.png"

    log("recording (asciinema)")
    record(cast)
    log("rendering (agg)")
    render(cast, gif, png, args.frame)

    tmux("kill-server")
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
