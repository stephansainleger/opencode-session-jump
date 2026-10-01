"""Unit tests for the ocjump command-line surface (non-interactive modes)."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from ocjump import config
from ocjump.__main__ import main


class CliTest(unittest.TestCase):
    """The listing and preview modes run against a throwaway store."""

    def setUp(self) -> None:
        """Create a store with two sessions and one message."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db_path = Path(self.tmp.name) / "opencode.db"
        conn = support.make_db(self.db_path)
        support.add_session(conn, "ses_a", "Alpha", "/p", time_updated=20)
        support.add_session(conn, "ses_b", "Beta", "/p", time_updated=10)
        support.add_message(conn, "m1", "ses_a", "user", "hello", time_created=1)
        conn.close()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run ``main`` on the throwaway store and capture stdout."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--no-state", "--db", str(self.db_path)] + argv)
        return code, buffer.getvalue()

    def test_json_listing_is_ndjson(self) -> None:
        """--list --json emits one parsed object per line, newest first."""
        code, out = self._run(["--list", "--json"])
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([row["title"] for row in rows], ["Alpha", "Beta"])

    def test_human_listing(self) -> None:
        """The default listing is a readable table."""
        code, out = self._run(["--list"])
        self.assertEqual(code, 0)
        self.assertIn("Alpha", out)

    def test_records_timings_are_logged(self) -> None:
        """A listing records its read timings for later diagnosis."""
        self._run(["--list"])
        log = (self.state / "ocjump" / "ocjump.log").read_text(encoding="utf-8")
        self.assertIn('"event": "records"', log)
        self.assertIn("records_ms", log)

    def test_null_listing_is_terminated(self) -> None:
        """-0 terminates every record with a NUL byte."""
        code, out = self._run(["--list", "-0"])
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("\0"))

    def test_preview_mode(self) -> None:
        """--preview prints the session metadata and message text."""
        code, out = self._run(["--preview", "ses_a"])
        self.assertEqual(code, 0)
        self.assertIn("hello", out)
        self.assertIn("Alpha", out)

    def test_version_exits_zero(self) -> None:
        """--version prints and exits successfully."""
        with self.assertRaises(SystemExit) as caught:
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_invalid_theme_is_reported(self) -> None:
        """A bad theme in the config file fails fast with a non-zero code."""
        config_file = Path(self.tmp.name) / "config.ini"
        config_file.write_text("[ocjump]\ntheme = nonsense\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {config.ENV_CONFIG: str(config_file)}):
            with redirect_stderr(io.StringIO()):
                code = main(["--no-state", "--db", str(self.db_path), "--list"])
        self.assertEqual(code, 1)


class CallbackTest(unittest.TestCase):
    """Hidden subcommands the fzf binds call back into."""

    def setUp(self) -> None:
        """Create a store with two sessions; redirect the log."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db_path = Path(self.tmp.name) / "opencode.db"
        conn = support.make_db(self.db_path)
        support.add_session(conn, "ses_a", "Alpha", "/p", time_updated=20)
        support.add_session(conn, "ses_b", "Beta", "/p", time_updated=10)
        conn.close()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run ``main`` capturing stdout."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--no-state", "--db", str(self.db_path)] + argv)
        return code, buffer.getvalue()

    def test_list_lines_emits_fzf_rows(self) -> None:
        """--list-lines prints one seven-field line per session."""
        code, out = self._run(["--list-lines"])
        self.assertEqual(code, 0)
        rows = [line for line in out.splitlines() if line]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0].split("\t")), 7)

    def test_action_does_not_write_stdout(self) -> None:
        """A callback action must stay silent (stdout would corrupt fzf)."""
        code, out = self._run(["--action", "delete", "--session", "ses_a"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
