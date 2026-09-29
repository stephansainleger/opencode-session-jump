"""Integration tests against a real, throwaway tmux server.

These exercise the parts that touch the real world: ``tmux list-panes`` format
parsing (including user options), window creation and pane focusing.  They run
on an isolated socket so the user's tmux server is never affected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from ocjump import actions, db, panes
from ocjump.runner import Result

SOCKET = f"ocjump-test-{os.getpid()}"
_TMUX_ENV = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def tmux(*args: str) -> Result:
    """Run a tmux command on the isolated test socket."""
    proc = subprocess.run(
        ["tmux", "-L", SOCKET, *args], capture_output=True, text=True, env=_TMUX_ENV
    )
    return Result(("tmux", *args), proc.returncode, proc.stdout, proc.stderr)


def local_runner(argv) -> Result:  # noqa: ANN001
    """Runner compatible with ocjump, redirected to the isolated socket."""
    assert argv[0] == "tmux", argv
    return tmux(*argv[1:])


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxIntegrationTest(unittest.TestCase):
    """Real-tmux behaviour of pane discovery, opening and jumping."""

    @classmethod
    def setUpClass(cls) -> None:
        """Start an isolated tmux server and a scratch directory."""
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        created = tmux("new-session", "-d", "-s", "it", "-x", "120", "-y", "30")
        if created.returncode != 0:
            cls._tmp.cleanup()
            raise unittest.SkipTest(f"cannot start test tmux server: {created.stderr.strip()}")

    @classmethod
    def tearDownClass(cls) -> None:
        """Tear down the isolated server, its socket file and scratch directory."""
        tmux("kill-server")
        for stale in Path(tempfile.gettempdir()).glob(f"tmux-*/{SOCKET}"):
            stale.unlink(missing_ok=True)
        cls._tmp.cleanup()

    def _spawn_fake_opencode(self, title: str, state: str = "working") -> tuple[str, Path]:
        """Start a long-lived process named ``opencode`` and tag its pane."""
        workdir = Path(tempfile.mkdtemp(dir=self.dir))
        fake = workdir / "opencode"
        shutil.copy("/bin/sleep", fake)
        fake.chmod(0o755)
        created = tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            "it",
            "-c",
            str(workdir),
            "-n",
            "fake",
            f"exec {fake} 60",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        pane_id = created.stdout.strip()
        tmux("set-option", "-p", "-t", pane_id, "@oc_session_id", "ses_fake")
        tmux("set-option", "-p", "-t", pane_id, "@oc_state", state)
        tmux("set-option", "-p", "-t", pane_id, "@oc_state_at", "99")
        tmux("select-pane", "-t", pane_id, "-T", title)
        self._wait_for_command(pane_id, "opencode")
        return pane_id, workdir

    def _wait_for_command(self, pane_id: str, expected: str) -> None:
        """Poll until the pane's foreground command settles (process start lag)."""
        for _ in range(50):
            current = tmux("display-message", "-p", "-t", pane_id, "#{pane_current_command}")
            if current.stdout.strip() == expected:
                return
            time.sleep(0.05)
        self.fail(f"pane {pane_id} never reported command {expected!r}")

    def test_list_panes_reads_state_and_options(self) -> None:
        """list-panes surfaces the plugin-published state and bound id."""
        self._spawn_fake_opencode("OC | Fake Session")
        found = panes.list_panes(local_runner)
        fake = next(p for p in found if p.title == "OC | Fake Session")
        self.assertEqual(fake.state, "working")
        self.assertEqual(fake.bound_session_id, "ses_fake")
        self.assertEqual(fake.state_at, 99)

    def test_title_and_directory_mapping(self) -> None:
        """Without a bound id, title plus directory still resolves the session."""
        _, workdir = self._spawn_fake_opencode("OC | Mapped Title")
        session = db.Session(
            session_id="ses_other",
            title="Mapped Title",
            directory=str(workdir),
            time_updated=0,
        )
        live = next(p for p in panes.list_panes(local_runner) if p.title == "OC | Mapped Title")
        unbound = panes.Pane(
            pane_id=live.pane_id,
            session_name=live.session_name,
            window_index=live.window_index,
            pane_index=live.pane_index,
            current_path=live.current_path,
            title=live.title,
            bound_session_id="",
            state=live.state,
            state_at=live.state_at,
        )
        resolved = panes.match_session(unbound, [session], {"ses_other": session})
        self.assertEqual(resolved, "ses_other")

    def test_open_new_session_creates_a_session(self) -> None:
        """open_new_session creates a dedicated, uniquely-named tmux session."""
        before = tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        session = db.Session(
            session_id="ses_fake", title="Opened", directory=str(self.dir), time_updated=0
        )
        name, results = actions.open_new_session(
            session, "sh -c 'exec sleep 60'", actions.TMUX_SESSION_PREFIX, local_runner
        )
        self.assertEqual(results[0].returncode, 0, results[0].stderr)
        after = tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(len(after), len(before) + 1)
        self.assertIn(name, after)
        self.assertTrue(name.startswith("oc-"))

    def test_jump_tolerates_a_detached_server(self) -> None:
        """select-window/select-pane succeed even when switch-client cannot."""
        pane_id, _ = self._spawn_fake_opencode("OC | Jump Target")
        pane = panes.Pane(
            pane_id=pane_id,
            session_name="it",
            window_index=1,
            pane_index=0,
            current_path="/",
            title="OC | Jump Target",
            bound_session_id="",
            state="done",
            state_at=None,
        )
        results = actions.jump(pane, local_runner)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[1].returncode, 0)  # select-window
        self.assertEqual(results[2].returncode, 0)  # select-pane


if __name__ == "__main__":
    unittest.main()
