"""Unit tests for read-only store access and preview rendering."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from ocjump import db


def _clean_env(**extra: str) -> dict[str, str]:
    """Return an environment without the OpenCode store variables, plus overrides."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {db.ENV_DB, db.ENV_CHANNEL, db.ENV_DISABLE_CHANNEL_DB, "XDG_DATA_HOME"}
    }
    env.update(extra)
    return env


class ResolveDbPathTest(unittest.TestCase):
    """Path resolution precedence: explicit flag, env var, probe, default."""

    def test_explicit_wins(self) -> None:
        """An explicit --db value overrides the environment and probe."""
        probed = lambda: Path("/probe/p.db")  # noqa: E731 - tiny inline probe
        self.assertEqual(db.resolve_db_path("/x/y.db", probe=probed), Path("/x/y.db"))

    def test_env_used_when_no_flag(self) -> None:
        """$OPENCODE_DB is used when no flag is given."""
        with mock.patch.dict(os.environ, {db.ENV_DB: "/env/z.db"}):
            self.assertEqual(db.resolve_db_path(None), Path("/env/z.db"))

    def test_probe_used_when_default_missing(self) -> None:
        """The probe is consulted only when the conventional file is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _clean_env(XDG_DATA_HOME=tmp)
            with mock.patch.dict(os.environ, env, clear=True):
                resolved = db.resolve_db_path(None, probe=lambda: Path("/probe/opencode.db"))
        self.assertEqual(resolved, Path("/probe/opencode.db"))

    def test_probe_skipped_when_default_exists(self) -> None:
        """An existing conventional store is used without the slow probe."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "opencode" / "opencode.db"
            store.parent.mkdir(parents=True)
            store.touch()

            def boom() -> Path:  # noqa: ANN202 - must never be called
                raise AssertionError("probe must not run when the default exists")

            with mock.patch.dict(os.environ, _clean_env(XDG_DATA_HOME=tmp), clear=True):
                self.assertEqual(db.resolve_db_path(None, probe=boom), store)

    def test_default_when_nothing_set(self) -> None:
        """With no flag, env or probe, the conventional path is used."""
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(db.resolve_db_path(None), db.default_db_path())

    def test_channel_adds_suffix(self) -> None:
        """A non-stable channel yields ``opencode-<channel>.db``."""
        with mock.patch.dict(os.environ, _clean_env(**{db.ENV_CHANNEL: "dev"})):
            self.assertEqual(db.default_db_path().name, "opencode-dev.db")

    def test_stable_channel_keeps_plain_name(self) -> None:
        """Stable/latest channels keep the plain ``opencode.db`` name."""
        with mock.patch.dict(os.environ, _clean_env(**{db.ENV_CHANNEL: "latest"})):
            self.assertEqual(db.default_db_path().name, "opencode.db")

    def test_disable_channel_db_overrides_channel(self) -> None:
        """$OPENCODE_DISABLE_CHANNEL_DB forces the plain name."""
        env = _clean_env(**{db.ENV_CHANNEL: "dev", db.ENV_DISABLE_CHANNEL_DB: "1"})
        with mock.patch.dict(os.environ, env):
            self.assertEqual(db.default_db_path().name, "opencode.db")

    def test_xdg_data_home_is_honored(self) -> None:
        """A custom XDG data dir relocates the conventional store."""
        with mock.patch.dict(os.environ, _clean_env(XDG_DATA_HOME="/data")):
            self.assertEqual(db.default_db_path(), Path("/data/opencode/opencode.db"))


class ConnectTest(unittest.TestCase):
    """Opening a store must fail fast and validate the schema."""

    def test_missing_file_raises(self) -> None:
        """A non-existent store raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            db.connect(Path("/nonexistent/opencode.db"))

    def test_missing_session_table_raises_schema_error(self) -> None:
        """A legacy/pre-SQLite store is reported as a schema error."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE other (id TEXT)")
            conn.commit()
            conn.close()
            with self.assertRaises(db.SchemaError):
                db.connect(path)

    def test_missing_columns_raises_schema_error(self) -> None:
        """A session table lacking required columns is rejected."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE session (id TEXT, title TEXT)")
            conn.commit()
            conn.close()
            with self.assertRaises(db.SchemaError):
                db.connect(path)


class ListSessionsTest(unittest.TestCase):
    """Listing must order by recency and hide children/archived by default."""

    def setUp(self) -> None:
        """Create a store with two parents, one child and one archived row."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.make_db(Path(self.tmp.name) / "opencode.db")
        support.add_session(self.conn, "ses_a", "Alpha", "/p", time_updated=10)
        support.add_session(self.conn, "ses_b", "Beta", "/p", time_updated=20)
        support.add_session(self.conn, "ses_child", "Child", "/p", parent_id="ses_a")
        support.add_session(self.conn, "ses_gone", "Gone", "/p", time_archived=5)

    def test_recent_first_and_hidden_rows_excluded(self) -> None:
        """Parents come back newest-first; children and archived rows are gone."""
        titles = [s.title for s in db.list_sessions(self.conn)]
        self.assertEqual(titles, ["Beta", "Alpha"])

    def test_include_children_reveals_subagents(self) -> None:
        """include_children reveals sub-agents but still hides archived rows."""
        titles = [s.title for s in db.list_sessions(self.conn, include_children=True)]
        self.assertIn("Child", titles)
        self.assertNotIn("Gone", titles)

    def test_include_archived_reveals_and_flags_rows(self) -> None:
        """include_archived adds archived rows while leaving children hidden."""
        sessions = db.list_sessions(self.conn, include_archived=True)
        gone = next(session for session in sessions if session.session_id == "ses_gone")
        self.assertTrue(gone.archived)
        self.assertEqual([s.title for s in sessions], ["Gone", "Beta", "Alpha"])
        self.assertFalse(any(session.session_id == "ses_child" for session in sessions))

    def test_limit_caps_the_rows(self) -> None:
        """A positive limit keeps the newest rows only."""
        titles = [s.title for s in db.list_sessions(self.conn, limit=1)]
        self.assertEqual(titles, ["Beta"])

    def test_zero_limit_means_no_cap(self) -> None:
        """The default ``limit=0`` returns every visible session."""
        self.assertEqual(len(db.list_sessions(self.conn, limit=0)), 2)


class GetSessionTest(unittest.TestCase):
    """Fetching by id success and failure paths."""

    def setUp(self) -> None:
        """Create a store with a single session."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.make_db(Path(self.tmp.name) / "opencode.db")
        support.add_session(self.conn, "ses_a", "Alpha", "/p")

    def test_found(self) -> None:
        """An existing id returns its session."""
        self.assertEqual(db.get_session(self.conn, "ses_a").title, "Alpha")

    def test_missing_raises_keyerror(self) -> None:
        """An unknown id raises KeyError."""
        with self.assertRaises(KeyError):
            db.get_session(self.conn, "ses_nope")

    def test_archived_flag_is_exposed(self) -> None:
        """Fetching an archived session reports the flag."""
        support.add_session(self.conn, "ses_arch", "Old", "/p", time_archived=5)
        self.assertTrue(db.get_session(self.conn, "ses_arch").archived)


class PreviewTest(unittest.TestCase):
    """The preview tail keeps message order and part types."""

    def setUp(self) -> None:
        """Create a store with one session and three messages."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.make_db(Path(self.tmp.name) / "opencode.db")
        support.add_session(self.conn, "ses_a", "Alpha", "/p")
        support.add_message(self.conn, "m1", "ses_a", "user", "first", time_created=1000)
        support.add_message(self.conn, "m2", "ses_a", "assistant", "second", time_created=2000)
        support.add_message(self.conn, "m3", "ses_a", "user", "third", time_created=3000)

    def _add_message(self, message_id: str, role: str, time_created: int) -> None:
        """Insert a message without parts, for typed-part tests."""
        self.conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, "ses_a", time_created, time_created, json.dumps({"role": role})),
        )
        self.conn.commit()

    def _add_part(self, part_id: str, message_id: str, payload: dict) -> None:
        """Insert one raw part payload attached to ``message_id``."""
        self.conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (part_id, message_id, "ses_a", 0, 0, json.dumps(payload)),
        )
        self.conn.commit()

    def test_tail_is_chronological_and_counts_omitted(self) -> None:
        """Only the newest direct messages are kept, oldest first within the window."""
        preview = db.load_preview(self.conn, "ses_a", direct_limit=2)
        self.assertEqual([message.role for message in preview.messages], ["assistant", "user"])
        self.assertEqual(preview.total, 3)
        self.assertEqual(preview.omitted, 1)

    def test_window_spans_intermediate_assistants(self) -> None:
        """Four direct messages pull in every assistant between them."""
        self._add_message("m4", "user", 4000)
        self._add_message("m5", "assistant", 5000)
        self._add_part("p5", "m5", {"type": "reasoning", "text": "plan"})
        self._add_message("m6", "assistant", 6000)
        self._add_part("p6", "m6", {"type": "text", "text": "answer one"})
        self._add_message("m7", "user", 7000)
        self._add_message("m8", "assistant", 8000)
        self._add_part(
            "p8", "m8", {"type": "tool", "tool": "bash", "state": {"status": "completed"}}
        )
        self._add_message("m9", "assistant", 9000)
        self._add_part("p9", "m9", {"type": "text", "text": "answer two"})
        self._add_message("m10", "user", 10000)
        preview = db.load_preview(self.conn, "ses_a", direct_limit=4)
        self.assertEqual(
            [message.role for message in preview.messages],
            ["assistant", "user", "assistant", "assistant", "user"],
        )
        self.assertEqual(len(preview.messages), 5)
        self.assertEqual(preview.omitted, 5)
        self.assertEqual(
            sum(1 for message in preview.messages if db.is_direct_message(message)), 4
        )

    def test_parts_keep_their_kind_and_tool_status(self) -> None:
        """Text, reasoning and tool parts are not flattened together."""
        self._add_message("m4", "assistant", 4000)
        self._add_part("p4a", "m4", {"type": "text", "text": "visible"})
        self._add_part("p4b", "m4", {"type": "reasoning", "text": "plan"})
        self._add_part(
            "p4c", "m4", {"type": "tool", "tool": "bash", "state": {"status": "completed"}}
        )
        preview = db.load_preview(self.conn, "ses_a", direct_limit=1)
        parts = preview.messages[0].parts
        self.assertEqual([part.kind for part in parts], ["text", "reasoning", "tool"])
        self.assertEqual(parts[0].text, "visible")
        self.assertEqual(parts[1].text, "plan")
        self.assertEqual((parts[2].tool, parts[2].status), ("bash", "completed"))

    def test_empty_session(self) -> None:
        """A session without messages yields an empty preview and zero total."""
        support.add_session(self.conn, "ses_empty", "Empty", "/p")
        preview = db.load_preview(self.conn, "ses_empty")
        self.assertEqual(preview.messages, ())
        self.assertEqual((preview.total, preview.omitted), (0, 0))

    def test_negative_direct_limit_raises(self) -> None:
        """A non-positive direct limit is rejected at the boundary."""
        with self.assertRaises(ValueError):
            db.load_preview(self.conn, "ses_a", direct_limit=0)
