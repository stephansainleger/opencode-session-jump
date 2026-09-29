"""Unit tests for the best-effort diagnostic log."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocjump import log


class LogTest(unittest.TestCase):
    """Logging appends NDJSON and never raises, even when unwritable."""

    def setUp(self) -> None:
        """Redirect the log into a temporary state directory."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_records_append_with_their_fields(self) -> None:
        """Each call appends one NDJSON record carrying its fields."""
        log.log("selection", session_id="ses_a")
        log.log("done")
        lines = (self.state / "ocjump" / "ocjump.log").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "selection")
        self.assertEqual(record["session_id"], "ses_a")
        self.assertIn("ts", record)

    def test_unwritable_target_never_raises(self) -> None:
        """A broken state path is swallowed so the picker keeps working."""
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(blocker)}):
            log.log("should_not_raise")
