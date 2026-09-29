"""Unit tests for read-only store access and preview rendering."""

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


class DescribeTest(unittest.TestCase):
    """The preview must include metadata and the latest message text."""

    def setUp(self) -> None:
        """Create a store with one session and one user message."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.make_db(Path(self.tmp.name) / "opencode.db")
        support.add_session(self.conn, "ses_a", "Alpha", "/p")
        support.add_message(self.conn, "m1", "ses_a", "user", "hello world", time_created=1)

    def test_describe_includes_metadata_and_message(self) -> None:
        """The preview shows title, id, model and the latest message body."""
        text = db.describe(self.conn, db.get_session(self.conn, "ses_a"))
        self.assertIn("Alpha", text)
        self.assertIn("ses_a", text)
        self.assertIn("prov/m", text)
        self.assertIn("hello world", text)
