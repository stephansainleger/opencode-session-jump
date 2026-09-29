"""Unit tests for tmux command construction and the execution wrappers."""

import tempfile
import unittest
from pathlib import Path

from ocjump import actions, db
from ocjump.panes import Pane
from ocjump.runner import Result


def _pane() -> Pane:
    """Return a representative pane for jump-command tests."""
    return Pane(
        pane_id="%7",
        session_name="work",
        window_index=2,
        pane_index=1,
        current_path="/p",
        title="OC | Hello",
        bound_session_id="ses_a",
        state="working",
        state_at=None,
    )


def _session() -> db.Session:
    """Return a representative session for open-window tests."""
    return db.Session(session_id="ses_a", title="Hello world", directory="/p", time_updated=0)


class RecordingRunner:
    """Test double that records argv and returns a canned successful Result."""

    def __init__(self, returncode: int = 0) -> None:
        """Initialise the runner with the return code every call should report."""
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv):  # noqa: ANN001, ANN204 - test seam
        """Record the argv and return a successful Result."""
        self.calls.append(list(argv))
        return Result(tuple(argv), self.returncode, "", "")


class JumpCommandsTest(unittest.TestCase):
    """Jumping must move the client, select the window, then the pane."""

    def test_three_steps_in_order_and_executed(self) -> None:
        """The three tmux steps are built in order and all run."""
        commands = [
            ["tmux", "switch-client", "-t", "work"],
            ["tmux", "select-window", "-t", "work:2"],
            ["tmux", "select-pane", "-t", "%7"],
        ]
        self.assertEqual(actions.jump_commands(_pane()), commands)
        runner = RecordingRunner()
        actions.jump(_pane(), runner)
        self.assertEqual(runner.calls, commands)


class OpenSessionTest(unittest.TestCase):
    """Opening must resume the exact session in a dedicated tmux session."""

    def test_build_open_command(self) -> None:
        """The session flag is appended to the configured executable."""
        self.assertEqual(
            actions.build_open_command("opencode", "ses_a"), "opencode --session ses_a"
        )

    def test_session_name_variants(self) -> None:
        """Titles map to safe, capped, collision-free ``oc-`` names."""
        cases = [
            ("Hello, World!", (), "oc-hello-world"),
            ("x" * 100, (), "oc-" + "x" * actions.SESSION_SLUG_MAX),
            ("Hello", ("oc-hello", "oc-hello-2"), "oc-hello-3"),
            ("###", (), "oc-session"),
        ]
        for title, existing, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(actions.session_name(title, existing), expected)

    def test_new_session_argv(self) -> None:
        """The argv creates a detached session rooted at the directory."""
        self.assertEqual(
            actions.new_session_argv("oc-x", "/p", "opencode --session ses_a"),
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "oc-x",
                "-c",
                "/p",
                "opencode --session ses_a",
            ],
        )

    def test_open_new_window_argv(self) -> None:
        """A new window is named and rooted at the directory."""
        self.assertEqual(
            actions.new_window_argv("OC x", "/p", "opencode --session ses_a"),
            ["tmux", "new-window", "-c", "/p", "-n", "OC x", "opencode --session ses_a"],
        )

    def test_split_window_argv(self) -> None:
        """A split is rooted at the directory with no name."""
        self.assertEqual(
            actions.split_window_argv("/p", "opencode --session ses_a"),
            ["tmux", "split-window", "-c", "/p", "opencode --session ses_a"],
        )

    def test_window_name_is_flattened_and_capped(self) -> None:
        """Newlines collapse and the window name is capped."""
        name = actions.window_name("Hello\nworld" + "x" * 100)
        self.assertFalse(name.startswith("\n"))
        self.assertLessEqual(len(name), 40)
        self.assertTrue(name.startswith("OC "))

    def test_open_new_session_creates_then_switches(self) -> None:
        """Opening lists sessions, creates a unique one and switches the client."""
        runner = RecordingRunner()
        name, results = actions.open_new_session(
            _session(), "opencode", actions.TMUX_SESSION_PREFIX, runner
        )
        self.assertEqual(name, "oc-hello-world")
        self.assertEqual(results[0].returncode, 0)
        self.assertEqual(runner.calls[0], ["tmux", "list-sessions", "-F", "#{session_name}"])
        self.assertEqual(
            runner.calls[1],
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "oc-hello-world",
                "-c",
                "/p",
                "opencode --session ses_a",
            ],
        )
        self.assertEqual(runner.calls[2], ["tmux", "switch-client", "-t", "oc-hello-world"])

    def test_open_session_dispatches_window(self) -> None:
        """``action='window'`` opens a named window in the current session."""
        runner = RecordingRunner()
        label, results = actions.open_session(_session(), "opencode", "window", "oc-", runner)
        self.assertEqual(label, "OC Hello world")
        self.assertEqual(results[0].returncode, 0)
        self.assertEqual(
            runner.calls[0],
            [
                "tmux",
                "new-window",
                "-c",
                "/p",
                "-n",
                "OC Hello world",
                "opencode --session ses_a",
            ],
        )

    def test_open_session_dispatches_split(self) -> None:
        """``action='split'`` opens a pane split in the current window."""
        runner = RecordingRunner()
        label, results = actions.open_session(_session(), "opencode", "split", "oc-", runner)
        self.assertEqual(label, "split")
        self.assertEqual(results[0].returncode, 0)
        self.assertEqual(
            runner.calls[0],
            ["tmux", "split-window", "-c", "/p", "opencode --session ses_a"],
        )

    def test_open_session_rejects_unknown_action(self) -> None:
        """An unknown action is a programming error, not a silent no-op."""
        with self.assertRaises(ValueError):
            actions.open_session(_session(), "opencode", "bogus")


class ResolveCommandTest(unittest.TestCase):
    """A tmux server PATH often lacks ``~/.opencode/bin``; resolution must cope."""

    def setUp(self) -> None:
        """Create a scratch directory to hold fake executables."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _executable(self, name: str) -> Path:
        """Create an executable file named ``name`` in the scratch directory."""
        path = self.dir / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_absolute_path_returned_when_executable(self) -> None:
        """A path with a separator is used verbatim when executable."""
        path = self._executable("mybin")
        self.assertEqual(actions.resolve_command(str(path)), str(path))

    def test_absolute_path_missing_returns_none(self) -> None:
        """A path with a separator that is not executable resolves to None."""
        self.assertIsNone(actions.resolve_command("/no/such/binary"))

    def test_found_in_fallback_dirs(self) -> None:
        """A bare name missing from PATH is found in the fallback directories."""
        path = self._executable("frobnicate")
        self.assertEqual(actions.resolve_command("frobnicate", [str(self.dir)]), str(path))

    def test_not_found_returns_none(self) -> None:
        """An unknown name with no fallback directory resolves to None."""
        self.assertIsNone(actions.resolve_command("definitely-not-real-xyz", []))
