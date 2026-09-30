"""Unit tests for the optional INI configuration."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocjump import config


class ConfigTest(unittest.TestCase):
    """Configuration loading, defaults and validation."""

    def setUp(self) -> None:
        """Create a scratch path for a config file."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.ini"

    def _write(self, text: str) -> None:
        """Write ``text`` as the configuration file."""
        self.path.write_text(text, encoding="utf-8")

    def test_missing_file_returns_defaults(self) -> None:
        """An absent config file yields the documented defaults."""
        cfg = config.load(self.path)
        self.assertEqual(cfg.open_action, config.DEFAULT_OPEN_ACTION)
        self.assertEqual(cfg.command, config.DEFAULT_COMMAND)
        self.assertEqual(cfg.session_prefix, config.DEFAULT_SESSION_PREFIX)
        self.assertEqual(cfg.theme, config.DEFAULT_THEME)
        self.assertEqual(cfg.sort, config.DEFAULT_SORT)
        self.assertEqual(cfg.search, config.DEFAULT_SEARCH)
        self.assertIsNone(cfg.db)

    def test_reads_values(self) -> None:
        """All recognized keys are read from the file."""
        self._write(
            "[ocjump]\n"
            "open = split\n"
            "command = /opt/opencode\n"
            "session_prefix = ocx-\n"
            "theme = ansi\n"
            "sort = attention\n"
            "search = all\n"
            "db = /tmp/x.db\n"
        )
        cfg = config.load(self.path)
        self.assertEqual(cfg.open_action, "split")
        self.assertEqual(cfg.command, "/opt/opencode")
        self.assertEqual(cfg.session_prefix, "ocx-")
        self.assertEqual(cfg.theme, "ansi")
        self.assertEqual(cfg.sort, "attention")
        self.assertEqual(cfg.search, "all")
        self.assertEqual(cfg.db, "/tmp/x.db")

    def test_invalid_choices_raise(self) -> None:
        """Unknown open/sort/search values are rejected with a clear error."""
        for key in ("open", "sort", "search"):
            with self.subTest(key=key):
                self._write(f"[ocjump]\n{key} = nope\n")
                with self.assertRaises(config.ConfigError):
                    config.load(self.path)

    def test_malformed_file_raises(self) -> None:
        """A file without a section header is rejected."""
        self._write("open = x\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_env_override_path(self) -> None:
        """$OCJUMP_CONFIG selects the file when no explicit path is given."""
        self._write("[ocjump]\nopen = window\n")
        with mock.patch.dict(os.environ, {config.ENV_CONFIG: str(self.path)}):
            self.assertEqual(config.load().open_action, "window")
