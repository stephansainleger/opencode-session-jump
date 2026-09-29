"""Integration tests for the companion OpenCode plugin (plugin/oc-state.js).

The pure event→state mapping is checked directly, and the tmux-writing path is
verified by running the plugin against a fake ``tmux`` shim that logs argv, so
no real tmux server or OpenCode instance is required.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN = (Path(__file__).resolve().parent.parent / "plugin" / "oc-state.js").as_uri()

MAPPING_PROBE = f"""
import plugin, {{ stateForEvent }} from "{PLUGIN}"
const got = {{
  created: stateForEvent({{ type: "session.created" }}),
  message: stateForEvent({{ type: "message.updated" }}),
  permission: stateForEvent({{ type: "permission.asked" }}),
  question: stateForEvent({{ type: "question.asked" }}),
  idle: stateForEvent({{ type: "session.idle" }}),
  error: stateForEvent({{ type: "session.error" }}),
  ignored: stateForEvent({{ type: "session.updated" }}),
  defaultKeys: Object.keys(plugin).sort(),
  serverIsFunction: typeof plugin.server === "function",
  pluginId: plugin.id,
}}
console.log(JSON.stringify(got))
"""

WRITE_PROBE = f"""
import {{ OcStatePlugin }} from "{PLUGIN}"
const plugin = await OcStatePlugin()
await plugin.event({{ event: {{ type: "permission.asked", sessionID: "ses_x" }} }})
await plugin.event({{ event: {{ type: "session.idle", sessionID: "ses_x" }} }})
await new Promise((resolve) => setTimeout(resolve, 300))
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PluginMappingTest(unittest.TestCase):
    """The mapping must translate OpenCode events to the documented states."""

    def test_event_to_state_mapping(self) -> None:
        """Each event type maps to its documented state, others to null."""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", MAPPING_PROBE],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["created"], "working")
        self.assertEqual(got["message"], "working")
        self.assertEqual(got["permission"], "waiting-permission")
        self.assertEqual(got["question"], "waiting-question")
        self.assertEqual(got["idle"], "done")
        self.assertEqual(got["error"], "error")
        self.assertIsNone(got["ignored"])

    def test_default_export_is_a_server_plugin_record(self) -> None:
        """The default export must be ``{ id, server }`` for OpenCode's loader."""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", MAPPING_PROBE],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["defaultKeys"], ["id", "server"])
        self.assertTrue(got["serverIsFunction"])
        self.assertEqual(got["pluginId"], "oc-state")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PluginWriteTest(unittest.TestCase):
    """Firing events must publish the state as tmux pane options."""

    def test_writes_options_through_tmux(self) -> None:
        """The plugin writes @oc_state, @oc_session_id and @oc_state_at via tmux."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = tmp_path / "tmux.log"
            fake = tmp_path / "tmux"
            fake.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", TMUX_PANE="%99")
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", WRITE_PROBE],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = log.read_text().splitlines()
            self.assertTrue(
                any("@oc_state waiting-permission" in line for line in lines),
                lines,
            )
            self.assertTrue(any("@oc_state done" in line for line in lines), lines)
            self.assertTrue(any("@oc_session_id ses_x" in line for line in lines), lines)
            self.assertTrue(any("@oc_state_at" in line for line in lines), lines)
