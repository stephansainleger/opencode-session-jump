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

_MODULE_TMP = None


def setUpModule() -> None:
    """Redirect all diagnostic logging to a throwaway state dir for the module."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    os.environ["XDG_STATE_HOME"] = _MODULE_TMP.name


def tearDownModule() -> None:
    """Undo the module-level state dir redirection."""
    os.environ.pop("XDG_STATE_HOME", None)
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()


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


def _settings(tmp_path: Path) -> main_mod.Settings:
    """Build settings pointing at ``tmp_path/opencode.db``."""
    return main_mod.Settings(
        db=tmp_path, command="opencode", open_action="session", session_prefix="oc-"
    )


def _record() -> render.Record:
    """Return a representative record with a bound pane."""
    return render.Record(SESSION, PANE, now_ms=2_000_000)


class PickTest(unittest.TestCase):
    """The single fzf call: argv, stream, color and output parsing."""

    def setUp(self) -> None:
        """Create settings for the picker under a throwaway store."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = _settings(Path(self.tmp.name) / "opencode.db")

    def _capture(self, stdout: str, returncode: int = 0):  # noqa: ANN202 - test helper
        captured = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            captured["input"] = kwargs.get("input")
            return Result(tuple(argv), returncode, stdout, "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            result = main_mod._pick([_record()], self.settings, PALETTE)
        return result, captured

    def test_pick_parses_the_accepted_session(self) -> None:
        """--print-query adds a line; the seven-field line carries id and pane."""
        record = _record()
        line = render.fzf_line(record, render.layout_for([record]), PALETTE)
        result, captured = self._capture("query\n" + line + "\n")
        self.assertEqual(result.session_id, "ses_a")
        self.assertEqual(result.pane_id, "%1")
        self.assertIn("--print-query", captured["argv"])
        self.assertIn("--multi", captured["argv"])
        self.assertTrue(any(arg.startswith("--preview=") for arg in captured["argv"]))
        self.assertTrue(any("{6}" in arg for arg in captured["argv"]))
        header = next(arg for arg in captured["argv"] if arg.startswith("--header="))
        self.assertIn("directory", header)
        self.assertIn("ctrl-d", header)  # shortcuts legend on the second line

    def test_pick_binds_actions_to_reload_in_place(self) -> None:
        """Fork reloads immediately; the confirm key reloads after firing."""
        _, captured = self._capture("")
        binds = {
            arg.split(":", 1)[0].removeprefix("--bind="): arg
            for arg in captured["argv"]
            if arg.startswith("--bind=")
        }
        for key in ("ctrl-f", "ctrl-d", "ctrl-k", "ctrl-y"):
            self.assertIn(key, binds)
        self.assertIn("+reload-sync(", binds["ctrl-f"])
        self.assertIn("+reload-sync(", binds["ctrl-y"])
        self.assertIn("--action delete", binds["ctrl-y"])
        # Arming must not reload: fzf's reload would clear the marks.
        self.assertNotIn("reload-sync", binds["ctrl-d"])
        self.assertNotIn("reload-sync", binds["ctrl-k"])

    def test_destructive_keys_confirm_in_the_footer(self) -> None:
        """ctrl-d / ctrl-k arm a footer prompt; ctrl-y fires; fork is immediate."""
        _, captured = self._capture("")
        binds = {
            arg.split(":", 1)[0].removeprefix("--bind="): arg
            for arg in captured["argv"]
            if arg.startswith("--bind=")
        }
        self.assertNotIn("transform-header", binds["ctrl-f"])
        for key in ("ctrl-d", "ctrl-k"):
            bind = binds[key]
            self.assertIn("transform-header(", bind)
            self.assertIn("ctrl-y", bind)  # points at the confirm key
            self.assertIn("to confirm", bind)
            self.assertIn("Esc to cancel", bind)
            self.assertIn("$#", bind)  # count of marked rows
            self.assertIn(f"--bind={key}:execute-silent(set -- {{+6}}", bind)
        # The confirm key snapshots the ids at arm time, then fires and reloads.
        confirm = binds["ctrl-y"]
        self.assertIn("$(cat ", confirm)
        self.assertIn("rm -f ", confirm)
        self.assertIn("--action close", confirm)

    def test_pick_returns_empty_on_abort(self) -> None:
        """A non-zero fzf exit yields an empty result (cancel)."""
        result, _ = self._capture("", returncode=130)
        self.assertEqual(result.session_id, "")

    def test_pick_returns_empty_without_a_selected_line(self) -> None:
        """A query but no accepted line is treated as no selection."""
        result, _ = self._capture("just a query\n")
        self.assertEqual(result.session_id, "")

    def test_pick_does_not_pipe_fzf_stderr(self) -> None:
        """The fzf UI renders on stderr; piping it blanks the popup (regression)."""
        _, captured = self._capture("")
        self.assertNotEqual(captured["kwargs"].get("capture_output"), True)
        self.assertIsNone(captured["kwargs"].get("stderr"))

    def test_pick_restricts_search_to_text_by_default(self) -> None:
        """Default search matches directory+title only (--nth=4,5)."""
        _, captured = self._capture("")
        self.assertIn("--nth=4,5", captured["argv"])

    def test_pick_search_all_drops_nth(self) -> None:
        """``search=all`` searches every displayed field (no --nth)."""
        settings = main_mod.Settings(
            db=self.settings.db,
            command="opencode",
            open_action="session",
            session_prefix="oc-",
            search="all",
        )
        captured = {}

        def capturing_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            captured["argv"] = argv
            return Result(tuple(argv), 0, "", "")

        with mock.patch.object(main_mod.subprocess, "run", capturing_run):
            main_mod._pick([_record()], settings, PALETTE)
        self.assertFalse(any(arg.startswith("--nth=") for arg in captured["argv"]))

    def test_pick_color_is_opt_out(self) -> None:
        """Colored mode passes --ansi; plain mode omits it entirely."""
        _, colored = self._capture("")
        self.assertIn("--ansi", colored["argv"])
        self.assertIn("\x1b[", colored["input"])

        plain_capture = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202 - test seam
            plain_capture["argv"] = argv
            plain_capture["input"] = kwargs.get("input")
            return Result(tuple(argv), 0, "", "")

        with mock.patch.object(main_mod.subprocess, "run", fake_run):
            main_mod._pick([_record()], self.settings, PALETTE, color=False)
        self.assertNotIn("--ansi", plain_capture["argv"])
        self.assertNotIn("\x1b[", plain_capture["input"])

    def test_use_color_honors_no_color(self) -> None:
        """$NO_COLOR disables color output."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(main_mod._use_color())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(main_mod._use_color())


class BindingsTest(unittest.TestCase):
    """The generated fzf --bind spec and callback commands."""

    def setUp(self) -> None:
        """Build settings for bind generation."""
        self.settings = main_mod.Settings(
            db=Path("/tmp/x.db"), command="opencode", open_action="session",
            session_prefix="oc-",
        )

    def test_callback_carries_the_settings(self) -> None:
        """The callback re-invokes ocjump with the store and the marked ids."""
        cmd = main_mod._callback(self.settings, "delete")
        self.assertIn("--action delete", cmd)
        self.assertIn("--db /tmp/x.db", cmd)
        self.assertIn("--session {+6}", cmd)

    def test_callback_includes_non_default_command(self) -> None:
        """A non-default executable is propagated to the callback."""
        settings = main_mod.Settings(
            db=Path("/tmp/x.db"), command="/opt/opencode", open_action="session",
            session_prefix="oc-",
        )
        self.assertIn("--command /opt/opencode", main_mod._callback(settings, "fork"))

    def test_reload_uses_list_lines(self) -> None:
        """The reload command regenerates the input stream."""
        self.assertIn("--list-lines", main_mod._reload_command(self.settings))

    def test_bindings_use_the_picker_state_dir(self) -> None:
        """The header, pending ids and confirm messages live under the state dir."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            joined = "".join(main_mod._bindings(self.settings, state))
        self.assertIn(str(state / "header"), joined)
        self.assertIn(str(state / "pending-delete"), joined)
        self.assertIn(str(state / "pending-close"), joined)
        self.assertIn(str(state / "msg-delete"), joined)
        self.assertIn(str(state / "msg-close"), joined)


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


class OpenSelectedTest(unittest.TestCase):
    """Enter either jumps to the bound pane or opens the session."""

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
        conn.close()
        self.settings = _settings(path)

    def test_bound_pane_jumps(self) -> None:
        """A pane id matching a live pane focuses it, no DB open."""
        ok = [Result(("tmux",), 0, "", "")] * 3
        with mock.patch.object(main_mod, "list_panes", return_value=[PANE]):
            with mock.patch.object(main_mod, "jump", return_value=ok) as jump:
                with mock.patch.object(main_mod, "_notify"):
                    main_mod._open_selected(
                        main_mod.PickerResult("ses_a", "%1"), self.settings
                    )
        jump.assert_called_once_with(PANE)

    def test_closed_session_opens(self) -> None:
        """No bound pane opens the session with the configured action."""
        ok = Result(("tmux",), 0, "", "")
        with mock.patch.object(main_mod, "list_panes", return_value=[]):
            with mock.patch.object(
                main_mod, "open_session", return_value=("oc-hello", [ok])
            ) as opener:
                with mock.patch.object(main_mod, "_notify"):
                    main_mod._open_selected(
                        main_mod.PickerResult("ses_a", ""), self.settings
                    )
        self.assertEqual(opener.call_args.args[0].session_id, "ses_a")

    def test_empty_selection_does_nothing(self) -> None:
        """An empty result is a no-op (cancel path)."""
        with mock.patch.object(main_mod, "list_panes") as panes:
            main_mod._open_selected(main_mod.PickerResult(), self.settings)
        panes.assert_not_called()


class ActionContractTest(unittest.TestCase):
    """The fzf callbacks resolve targets and delegate correctly."""

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
        self.settings = _settings(path)

    def test_delete_delegates_to_opencode(self) -> None:
        """Delete runs ``delete_sessions`` with the resolved command + ids."""
        ok = [Result(("opencode",), 0, "", "")]
        with mock.patch.object(
            main_mod, "resolve_command", return_value="/usr/bin/opencode"
        ):
            with mock.patch.object(main_mod, "delete_sessions", return_value=ok) as dele:
                with mock.patch.object(main_mod, "_notify"):
                    main_mod._action_delete(["ses_a", "ses_b"], self.settings)
        self.assertEqual(dele.call_args.args[0], "/usr/bin/opencode")
        self.assertEqual(dele.call_args.args[1], ["ses_a", "ses_b"])

    def test_cli_accepts_multiple_session_ids(self) -> None:
        """``--session a b`` (fzf's ``{+6}``) reaches the action as two ids."""
        ok = [Result(("opencode",), 0, "", "")]
        with mock.patch.object(
            main_mod, "resolve_command", return_value="/usr/bin/opencode"
        ):
            with mock.patch.object(main_mod, "delete_sessions", return_value=ok) as dele:
                with mock.patch.object(main_mod, "_notify"):
                    code = main_mod.main(
                        [
                            "--no-state",
                            "--db",
                            str(self.settings.db),
                            "--action",
                            "delete",
                            "--session",
                            "ses_a",
                            "ses_b",
                        ]
                    )
        self.assertEqual(code, 0)
        self.assertEqual(dele.call_args.args[1], ["ses_a", "ses_b"])

    def test_close_targets_the_pane_session(self) -> None:
        """A bound session is closed via its hosting tmux session name."""
        ok = [Result(("tmux",), 0, "", "")]
        with mock.patch.object(main_mod, "_records", return_value=([], {"%1": PANE})):
            with mock.patch.object(main_mod, "_sessions_by_id", return_value={}):
                with mock.patch.object(main_mod, "close_sessions", return_value=ok) as closer:
                    with mock.patch.object(main_mod, "_notify"):
                        main_mod._action_close(["ses_a"], self.settings)
        self.assertEqual(closer.call_args.args[0], [PANE.session_name])

    def test_fork_opens_a_fork(self) -> None:
        """Fork opens a forked session in a new tmux session."""
        ok = Result(("tmux",), 0, "", "")
        with mock.patch.object(main_mod, "resolve_command", return_value="/usr/bin/opencode"):
            with mock.patch.object(
                main_mod, "open_session", return_value=("oc-hello", [ok])
            ) as opener:
                with mock.patch.object(main_mod, "_notify"):
                    main_mod._action_fork(["ses_a"], self.settings)
        self.assertTrue(opener.call_args.kwargs.get("fork"))
