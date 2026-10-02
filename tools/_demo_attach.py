"""asciinema -c entry point: attach tmux on a pty, relay it, and inject keys.

asciinema records the command's stdout, but the tmux popup is drawn on the tty
the client attaches to.  So we allocate our own pty, attach tmux there, copy the
pty output to our stdout (what asciinema records), and write the demo keystrokes
into the pty master — the same technique the integration tests use.
"""

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

SOCKET = os.environ["DEMO_SOCKET"]
COLS = int(os.environ.get("DEMO_COLS", "120"))
ROWS = int(os.environ.get("DEMO_ROWS", "34"))
env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
env["TERM"] = "xterm-256color"


def tmux(*args: str) -> None:
    subprocess.run(
        ["tmux", "-L", SOCKET, "-f", "/dev/null", *args],
        env=env, capture_output=True,
    )


pid, fd = pty.fork()
if pid == 0:
    os.execvpe("tmux", ["tmux", "-L", SOCKET, "-f", "/dev/null", "attach", "-t", "demo"], env)
    os._exit(127)

fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

# Collect the keys the driver should send, each as (delay_before, bytes).
script: list[tuple[float, bytes]] = [(1.2, b"\x02j")]  # prefix + j: open popup
script.append((2.4, b"api"))                            # filter (typed)
script.append((1.6, b"\x15"))                           # clear query
script.append((1.4, b"\x04"))                           # ctrl-d: arm confirmation
script.append((2.2, b"\x1b"))                           # Esc: cancel
script.append((0.8, b"\x1b"))                           # Esc: close popup
script.append((0.6, b"\x1b"))                           # Esc: detach


def pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            os.write(sys.stdout.fileno(), data)


for delay, keys in script:
    pump(delay)
    os.write(fd, keys)

pump(1.5)
try:
    os.close(fd)
except OSError:
    pass
try:
    os.waitpid(pid, 0)
except ChildProcessError:
    pass
