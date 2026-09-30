"""Unit tests for the interactive glue between fzf and the tmux actions.

fzf itself cannot run headless, so its invocation is observed by patching
``subprocess.run`` and the action functions are replaced by recorders.  The
diagnostic log is redirected to a temporary state directory.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from ocjump import __main__ as main_mod
from ocjump import db, render, theme
from ocjump.panes import Pane
from ocjump.runner import Result

SESSION = db.Session(
    session_id="ses_a", title="Hello", directory="/p", time_updated=1_000_000
)
PALETTE = theme.load("ansi")

PANE = Pane(
    pane_id="%1",
    session_name="work",
    window_index=0,
    pane_index=0,
    current_path="/p",
    title="OC | Hello",
    bound_session_id="ses_a",
    state="working",
    state_at=None,
)


class PickTest(unittest.TestCase):
    """The fzf call must hide ids and use ocjump itself for the preview."""

    def test_pick_builds_fzf_argv_and_returns_selection(self) -> None:
        """The fzf call previews field 6 and parses the --expect output."""
        record = render.Record(SESSION, PANE, now_ms=2_000_000)
        line = render.fzf_line(record, render.layout_for([record]), PALETTE)
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            # --expect prints the pressed key first (empty for Enter), then items.
            return Result(tuple(argv), 0, "\n" + line + "\n", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            selection = main_mod._pick([record], Path("/tmp/x.db"), PALETTE)

        self.assertEqual(selection.key, "")
        self.assertEqual(selection.lines, [line])
        self.assertIn("--delimiter=\t", captured["argv"])
        self.assertIn("--multi", captured["argv"])
        self.assertIn("--expect=ctrl-d,ctrl-f,ctrl-k", captured["argv"])
        self.assertTrue(any(arg.startswith("--preview=") for arg in captured["argv"]))
        self.assertTrue(any("{6}" in arg for arg in captured["argv"]))
        header = next(arg for arg in captured["argv"] if arg.startswith("--header="))
        self.assertIn("directory", header)
        self.assertIn("ctrl-d", header)  # shortcuts legend on the second line

    def test_pick_returns_the_pressed_key(self) -> None:
        """A non-Enter key is reported so the dispatcher can act on it."""
        record = render.Record(SESSION, PANE, 2_000_000)
        line = render.fzf_line(record, render.layout_for([record]), PALETTE)

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            return Result(tuple(argv), 0, "ctrl-d\n" + line + "\n", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            selection = main_mod._pick([record], Path("/tmp/x.db"), PALETTE)
        self.assertEqual(selection.key, "ctrl-d")
        self.assertEqual(selection.lines, [line])

    def test_pick_does_not_pipe_fzf_stderr(self) -> None:
        """The fzf UI renders on stderr; piping it blanks the popup (regression)."""
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["kwargs"] = kwargs
            return Result(tuple(argv), 0, "", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._pick([render.Record(SESSION, PANE, 2_000_000)], Path("/tmp/x.db"), PALETTE)

        self.assertNotEqual(captured["kwargs"].get("capture_output"), True)
        self.assertIsNone(captured["kwargs"].get("stderr"))

    def test_pick_returns_none_on_abort(self) -> None:
        """A non-zero fzf exit is treated as a cancel."""

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            return Result(tuple(argv), 130, "", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            self.assertIsNone(main_mod._pick([], Path("/tmp/x.db"), PALETTE))

    def test_pick_restricts_search_to_text_by_default(self) -> None:
        """Default search matches directory+title only (--nth=4,5)."""
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            return Result(tuple(argv), 0, "", "")

        record = render.Record(SESSION, PANE, now_ms=2_000_000)
        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._pick([record], Path("/tmp/x.db"), PALETTE)
        self.assertIn("--nth=4,5", captured["argv"])

    def test_pick_search_all_drops_nth(self) -> None:
        """``search=all`` searches every displayed field (no --nth)."""
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            return Result(tuple(argv), 0, "", "")

        record = render.Record(SESSION, PANE, now_ms=2_000_000)
        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._pick([record], Path("/tmp/x.db"), PALETTE, "all")
        self.assertFalse(any(arg.startswith("--nth=") for arg in captured["argv"]))

    def test_pick_enables_ansi_when_colored(self) -> None:
        """Colored mode passes --ansi and a color scheme, and colors the input."""
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            return Result(tuple(argv), 0, "", "")

        record = render.Record(SESSION, PANE, now_ms=2_000_000)
        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._pick([record], Path("/tmp/x.db"), PALETTE, color=True)

        self.assertIn("--ansi", captured["argv"])
        self.assertTrue(any(arg.startswith("--color=") for arg in captured["argv"]))
        self.assertIn("\x1b[", captured["input"])
        header = next(arg for arg in captured["argv"] if arg.startswith("--header="))
        self.assertIn("\x1b[", header)  # shortcut legend is colored

    def test_pick_omits_color_when_disabled(self) -> None:
        """Plain mode sends neither --ansi nor ANSI codes."""
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            return Result(tuple(argv), 0, "", "")

        record = render.Record(SESSION, PANE, now_ms=2_000_000)
        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._pick([record], Path("/tmp/x.db"), PALETTE, color=False)

        self.assertNotIn("--ansi", captured["argv"])
        self.assertNotIn("\x1b[", captured["input"])
        header = next(arg for arg in captured["argv"] if arg.startswith("--header="))
        self.assertNotIn("\x1b[", header)

    def test_use_color_honors_no_color(self) -> None:
        """$NO_COLOR disables color output."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(main_mod._use_color())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(main_mod._use_color())


class NotifyTest(unittest.TestCase):
    """Feedback must go to the tmux status line, not to a vanishing stderr."""

    def test_notify_calls_display_message(self) -> None:
        """_notify invokes ``tmux display-message`` with the prefixed text."""
        calls = []

        def recorder(argv):  # noqa: ANN001, ANN202 - test seam
            calls.append(list(argv))
            return Result(tuple(argv), 0, "", "")

        main_mod._notify("hello", recorder)
        self.assertEqual(calls[0][:2], ["tmux", "display-message"])
        self.assertTrue(calls[0][-1].endswith("ocjump: hello"))


class HandleSelectionTest(unittest.TestCase):
    """A selection either jumps to its pane or opens a new window."""

    def setUp(self) -> None:
        """Create a store whose session directory exists, and redirect the log."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state"
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        resolver = mock.patch.object(
            main_mod, "resolve_command", return_value="/usr/bin/opencode"
        )
        resolver.start()
        self.addCleanup(resolver.stop)
        self.workdir = Path(self.tmp.name) / "work"
        self.workdir.mkdir()
        path = Path(self.tmp.name) / "opencode.db"
        conn = support.make_db(path)
        support.add_session(conn, "ses_a", "Hello", str(self.workdir))
        support.add_session(conn, "ses_gone", "Gone", "/no/such/dir")
        conn.close()
        self.settings = main_mod.Settings(
            db=path, command="opencode", open_action="session", session_prefix="oc-"
        )
        self.session = db.Session(
            session_id="ses_a", title="Hello", directory=str(self.workdir), time_updated=1
        )

    def _selected(self, pane: Pane | None) -> str:
        """Render an fzf selection line for this session and pane."""
        record = render.Record(self.session, pane, now_ms=2_000_000)
        return render.fzf_line(record, render.layout_for([record]), PALETTE)

    def test_open_pane_jumps(self) -> None:
        """A line carrying a pane id focuses that pane."""
        ok = [Result(("tmux",), 0, "", "")] * 3
        with mock.patch.object(main_mod, "jump", return_value=ok) as jump:
            with mock.patch.object(main_mod, "_notify"):
                code = main_mod._handle_selection(
                    self._selected(PANE), {"%1": PANE}, self.settings
                )
        self.assertEqual(code, 0)
        jump.assert_called_once_with(PANE)

    def test_jump_failure_is_surfaced(self) -> None:
        """A failing jump returns non-zero and notifies the user."""
        bad = [Result(("tmux",), 1, "", "boom")] * 3
        with mock.patch.object(main_mod, "jump", return_value=bad):
            with mock.patch.object(main_mod, "_notify") as notify:
                code = main_mod._handle_selection(
                    self._selected(PANE), {"%1": PANE}, self.settings
                )
        self.assertEqual(code, 1)
        self.assertIn("jump failed", notify.call_args.args[0])

    def test_closed_session_opens(self) -> None:
        """A line without a pane id opens the session via the configured action."""
        ok = Result(("tmux",), 0, "", "")
        with mock.patch.object(
            main_mod, "open_session", return_value=("oc-hello", [ok, ok])
        ) as opener:
            with mock.patch.object(main_mod, "_notify") as notify:
                code = main_mod._handle_selection(self._selected(None), {}, self.settings)
        self.assertEqual(code, 0)
        self.assertEqual(opener.call_args.args[0].session_id, "ses_a")
        self.assertIn("opened", notify.call_args.args[0])

    def test_open_failure_is_surfaced(self) -> None:
        """A failing open run returns non-zero and notifies the user."""
        bad = Result(("tmux",), 1, "", "no such directory")
        with mock.patch.object(main_mod, "open_session", return_value=("oc-hello", [bad])):
            with mock.patch.object(main_mod, "_notify") as notify:
                code = main_mod._handle_selection(self._selected(None), {}, self.settings)
        self.assertEqual(code, 1)
        self.assertIn("open failed", notify.call_args.args[0])

    def test_command_not_found_is_surfaced(self) -> None:
        """An unresolvable opencode command is reported instead of a dead window."""
        with mock.patch.object(main_mod, "resolve_command", return_value=None):
            with mock.patch.object(main_mod, "_notify") as notify:
                code = main_mod._handle_selection(self._selected(None), {}, self.settings)
        self.assertEqual(code, 1)
        self.assertIn("command not found", notify.call_args.args[0])

    def test_missing_directory_is_surfaced(self) -> None:
        """A session whose directory vanished is reported, not silently dropped."""
        gone = db.Session(
            session_id="ses_gone", title="Gone", directory="/no/such/dir", time_updated=1
        )
        record = render.Record(gone, None, now_ms=2_000_000)
        selected = render.fzf_line(record, render.layout_for([record]), PALETTE)
        with mock.patch.object(main_mod, "open_session") as opener:
            with mock.patch.object(main_mod, "_notify") as notify:
                code = main_mod._handle_selection(selected, {}, self.settings)
        self.assertEqual(code, 1)
        opener.assert_not_called()
        self.assertIn("directory no longer exists", notify.call_args.args[0])

    def test_bad_selection_is_surfaced(self) -> None:
        """A malformed fzf line is logged and reported, not silently dropped."""
        with mock.patch.object(main_mod, "_notify") as notify:
            code = main_mod._handle_selection("only\tthree", {}, self.settings)
        self.assertEqual(code, 1)
        self.assertIn("unexpected selection", notify.call_args.args[0])


class DispatchTest(unittest.TestCase):
    """Keyboard actions map to the right handler and loop outcome."""

    LINE = "a\tb\tc\td\te\tses_a\t%1"

    def setUp(self) -> None:
        """Build settings for the dispatcher."""
        self.settings = main_mod.Settings(
            db=Path("/tmp/x.db"), command="opencode", open_action="session",
            session_prefix="oc-",
        )

    def test_enter_opens_and_exits(self) -> None:
        """Enter runs the open handler and leaves the loop."""
        with mock.patch.object(main_mod, "_handle_selection", return_value=0) as handler:
            outcome = main_mod._dispatch(
                main_mod.Selection("", [self.LINE]), {"%1": PANE}, self.settings
            )
        self.assertEqual(outcome, "exit")
        handler.assert_called_once()

    def test_enter_failure_refreshes(self) -> None:
        """A failed open keeps the popup so the user can retry."""
        with mock.patch.object(main_mod, "_handle_selection", return_value=1):
            outcome = main_mod._dispatch(
                main_mod.Selection("", [self.LINE]), {"%1": PANE}, self.settings
            )
        self.assertEqual(outcome, "refresh")

    def test_action_keys_dispatch(self) -> None:
        """ctrl-f / ctrl-d / ctrl-k route to their handlers."""
        cases = {
            "ctrl-f": "_open_forked",
            "ctrl-d": "_delete_selected",
            "ctrl-k": "_close_selected",
        }
        for key, handler_name in cases.items():
            with self.subTest(key=key):
                with mock.patch.object(
                    main_mod, handler_name, return_value="refresh"
                ) as handler:
                    outcome = main_mod._dispatch(
                        main_mod.Selection(key, [self.LINE]), {"%1": PANE}, self.settings
                    )
                self.assertEqual(outcome, "refresh")
                handler.assert_called_once()


class ConfirmTest(unittest.TestCase):
    """The confirmation screen returns Enter=yes, Esc=no."""

    def _confirm(self, returncode: int):  # noqa: ANN202 - test helper
        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            return Result(tuple(argv), returncode, "", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            return main_mod._confirm("sure?", ["line"])

    def test_enter_confirms(self) -> None:
        """A zero exit code means the user confirmed."""
        self.assertTrue(self._confirm(0))

    def test_escape_cancels(self) -> None:
        """A non-zero exit code means the user cancelled."""
        self.assertFalse(self._confirm(130))

    def test_ansi_is_stripped(self) -> None:
        """Colored selection lines are shown plain on the confirmation screen."""
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["input"] = kwargs.get("input")
            return Result(tuple(argv), 0, "", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._confirm("sure?", ["\x1b[32mline\x1b[0m"])
        self.assertNotIn("\x1b[", captured["input"])


class ActionHandlersTest(unittest.TestCase):
    """The ctrl-* handlers resolve targets, confirm, and delegate correctly."""

    def setUp(self) -> None:
        """Create a store and settings, and redirect the diagnostic log."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state"
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workdir = Path(self.tmp.name) / "work"
        self.workdir.mkdir()
        path = Path(self.tmp.name) / "opencode.db"
        conn = support.make_db(path)
        support.add_session(conn, "ses_a", "Hello", str(self.workdir))
        conn.close()
        self.settings = main_mod.Settings(
            db=path, command="opencode", open_action="session", session_prefix="oc-"
        )
        session = db.Session("ses_a", "Hello", str(self.workdir), 1)
        record = render.Record(session, PANE, now_ms=2_000_000)
        self.line = render.fzf_line(record, render.layout_for([record]), PALETTE)

    def test_delete_confirmed_delegates_to_opencode(self) -> None:
        """Confirmation runs ``delete_sessions`` with the resolved command + ids."""
        ok = [Result(("opencode",), 0, "", "")]
        with mock.patch.object(main_mod, "_confirm", return_value=True):
            with mock.patch.object(
                main_mod, "resolve_command", return_value="/usr/bin/opencode"
            ):
                with mock.patch.object(main_mod, "delete_sessions", return_value=ok) as dele:
                    with mock.patch.object(main_mod, "_notify"):
                        outcome = main_mod._delete_selected(
                            main_mod.Selection("ctrl-d", [self.line]), self.settings
                        )
        self.assertEqual(outcome, "refresh")
        self.assertEqual(dele.call_args.args[0], "/usr/bin/opencode")
        self.assertEqual(dele.call_args.args[1], ["ses_a"])

    def test_delete_cancelled_does_nothing(self) -> None:
        """A cancelled confirmation never deletes."""
        with mock.patch.object(main_mod, "_confirm", return_value=False):
            with mock.patch.object(main_mod, "delete_sessions") as dele:
                outcome = main_mod._delete_selected(
                    main_mod.Selection("ctrl-d", [self.line]), self.settings
                )
        self.assertEqual(outcome, "refresh")
        dele.assert_not_called()

    def test_close_confirmed_targets_the_pane_session(self) -> None:
        """A bound session is closed via its hosting tmux session name."""
        ok = [Result(("tmux",), 0, "", "")]
        with mock.patch.object(main_mod, "_confirm", return_value=True):
            with mock.patch.object(main_mod, "close_sessions", return_value=ok) as closer:
                with mock.patch.object(main_mod, "_notify"):
                    outcome = main_mod._close_selected(
                        main_mod.Selection("ctrl-k", [self.line]), {"%1": PANE}, self.settings
                    )
        self.assertEqual(outcome, "refresh")
        self.assertEqual(closer.call_args.args[0], [PANE.session_name])

    def test_fork_opens_a_fork_and_exits(self) -> None:
        """``ctrl-f`` opens a forked session and leaves the loop."""
        ok = Result(("tmux",), 0, "", "")
        with mock.patch.object(main_mod, "resolve_command", return_value="/usr/bin/opencode"):
            with mock.patch.object(
                main_mod, "open_session", return_value=("oc-hello", [ok])
            ) as opener:
                with mock.patch.object(main_mod, "_notify"):
                    outcome = main_mod._open_forked(
                        main_mod.Selection("ctrl-f", [self.line]), self.settings
                    )
        self.assertEqual(outcome, "exit")
        self.assertTrue(opener.call_args.kwargs.get("fork"))
