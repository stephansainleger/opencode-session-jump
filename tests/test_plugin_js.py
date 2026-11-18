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
  activity: stateForEvent({{ type: "session.text.delta" }}),
  permission: stateForEvent({{ type: "permission.asked" }}),
  permissionReplied: stateForEvent({{ type: "permission.replied" }}),
  question: stateForEvent({{ type: "form.created" }}),
  questionReplied: stateForEvent({{ type: "form.replied" }}),
  questionCancelled: stateForEvent({{ type: "form.cancelled" }}),
  idle: stateForEvent({{ type: "session.idle" }}),
  error: stateForEvent({{ type: "session.execution.failed" }}),
  ignored: stateForEvent({{ type: "session.renamed" }}),
  defaultKeys: Object.keys(plugin).sort(),
  setupIsFunction: typeof plugin.setup === "function",
  pluginId: plugin.id,
}}
console.log(JSON.stringify(got))
"""

NEXTSTATE_PROBE = f"""
import {{ nextState }} from "{PLUGIN}"
const seq = (events) => {{
  let waiting = null
  const states = []
  for (const event of events) {{
    const decision = nextState(waiting, event)
    waiting = decision.waiting
    states.push(decision.state)
  }}
  return {{ states, waiting }}
}}
const out = {{
  permissionBlocked: seq([
    {{ type: "permission.asked" }},
    {{ type: "session.text.delta" }},
    {{ type: "session.step.started" }},
  ]),
  permissionCleared: seq([
    {{ type: "permission.asked" }},
    {{ type: "session.text.delta" }},
    {{ type: "permission.replied" }},
    {{ type: "session.text.delta" }},
  ]),
  questionBlocked: seq([
    {{ type: "form.created" }},
    {{ type: "session.text.delta" }},
  ]),
  questionCleared: seq([
    {{ type: "form.created" }},
    {{ type: "form.cancelled" }},
  ]),
  idleClearsWait: seq([
    {{ type: "permission.asked" }},
    {{ type: "session.idle" }},
  ]),
}}
console.log(JSON.stringify(out))
"""

WRITE_PROBE = f"""
import plugin from "{PLUGIN}"
const events = [
  {{ type: "permission.asked", data: {{ sessionID: "ses_x" }} }},
  {{ type: "session.idle", data: {{ sessionID: "ses_x" }} }},
]
const ctx = {{
  event: {{
    subscribe: async function* () {{
      for (const event of events) yield event
    }},
  }},
}}
await plugin.setup(ctx)
await new Promise((resolve) => setTimeout(resolve, 300))
"""

# A permission wait must survive the activity churn that follows it in the real
# event stream; only the answer may resume ``working``.
WRITE_WAIT_PROBE = f"""
import plugin from "{PLUGIN}"
const events = [
  {{ type: "permission.asked", data: {{ sessionID: "ses_x" }} }},
  {{ type: "session.text.delta", data: {{ sessionID: "ses_x" }} }},
]
const ctx = {{
  event: {{
    subscribe: async function* () {{
      for (const event of events) yield event
    }},
  }},
}}
await plugin.setup(ctx)
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
        self.assertEqual(got["activity"], "working")
        self.assertEqual(got["permission"], "waiting-permission")
        self.assertEqual(got["permissionReplied"], "working")
        self.assertEqual(got["question"], "waiting-question")
        self.assertEqual(got["questionReplied"], "working")
        self.assertEqual(got["questionCancelled"], "working")
        self.assertEqual(got["idle"], "done")
        self.assertEqual(got["error"], "error")
        self.assertIsNone(got["ignored"])

    def test_wait_lock_survives_activity_churn(self) -> None:
        """A pending permission/question is not overwritten by activity events."""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", NEXTSTATE_PROBE],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)

        blocked = got["permissionBlocked"]
        self.assertEqual(blocked["states"], ["waiting-permission", None, None])
        self.assertEqual(blocked["waiting"], "waiting-permission")

        cleared = got["permissionCleared"]
        self.assertEqual(
            cleared["states"], ["waiting-permission", None, "working", "working"]
        )
        self.assertIsNone(cleared["waiting"])

        self.assertEqual(got["questionBlocked"]["states"], ["waiting-question", None])
        self.assertEqual(got["questionBlocked"]["waiting"], "waiting-question")
        self.assertEqual(got["questionCleared"]["states"], ["waiting-question", "working"])
        self.assertIsNone(got["questionCleared"]["waiting"])

        idle = got["idleClearsWait"]
        self.assertEqual(idle["states"], ["waiting-permission", "done"])
        self.assertIsNone(idle["waiting"])

    def test_default_export_is_a_v2_plugin_definition(self) -> None:
        """The default export must be ``{ id, setup }`` for OpenCode 2's loader."""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", MAPPING_PROBE],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["defaultKeys"], ["id", "setup"])
        self.assertTrue(got["setupIsFunction"])
        self.assertEqual(got["pluginId"], "oc-state")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PluginWriteTest(unittest.TestCase):
    """Firing events must publish the state as tmux pane options."""

    def _run_with_fake_tmux(self, probe: str) -> list[str]:
        """Run ``probe`` with a fake tmux shim and return the logged argv lines."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = tmp_path / "tmux.log"
            fake = tmp_path / "tmux"
            fake.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", TMUX_PANE="%99")
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", probe],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return log.read_text().splitlines()

    def test_writes_options_through_tmux(self) -> None:
        """The plugin writes @oc_state, @oc_session_id and @oc_state_at via tmux."""
        lines = self._run_with_fake_tmux(WRITE_PROBE)
        self.assertTrue(
            any("@oc_state waiting-permission" in line for line in lines), lines
        )
        self.assertTrue(any("@oc_state done" in line for line in lines), lines)
        self.assertTrue(any("@oc_session_id ses_x" in line for line in lines), lines)
        self.assertTrue(any("@oc_state_at" in line for line in lines), lines)

    def test_pending_permission_is_not_overwritten(self) -> None:
        """A message update after permission.asked must not republish ``working``."""
        lines = self._run_with_fake_tmux(WRITE_WAIT_PROBE)
        self.assertTrue(
            any("@oc_state waiting-permission" in line for line in lines), lines
        )
        self.assertFalse(
            any("@oc_state working" in line for line in lines),
            f"the pending wait was clobbered by working: {lines}",
        )
