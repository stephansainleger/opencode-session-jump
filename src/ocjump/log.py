"""Best-effort diagnostic log for the interactive picker.

A tmux popup closes the moment the command exits, so anything written to
stderr (including a traceback) is lost.  Every interactive run therefore
appends a short NDJSON record here, which is the only way to reconstruct what
happened after the fact.  Logging must never raise: a broken log must not break
the picker.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

APP_DIR = "ocjump"
LOG_NAME = "ocjump.log"


def log_path() -> Path:
    """Return the diagnostic log path under the XDG state directory."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / APP_DIR / LOG_NAME


def log(event: str, **fields: object) -> None:
    """Append one timestamped NDJSON record; swallow every error."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
