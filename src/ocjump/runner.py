"""Subprocess execution with an injectable runner seam for tests.

Every external command the picker issues (``tmux``, ``fzf``, ``ai-audit``) goes
through :func:`run`.  Tests substitute their own :data:`Runner` instead of
touching the real system, while the production default shells out for real.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    """Outcome of a single command invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], Result]


def run(argv: Sequence[str], check: bool = False) -> Result:
    """Execute ``argv``, capturing output; raise only when ``check`` is set.

    Raises:
        RuntimeError: ``check`` is true and the command exited non-zero.
    """
    proc = subprocess.run(list(argv), capture_output=True, text=True)
    result = Result(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
    if check and result.returncode != 0:
        joined = " ".join(result.argv)
        raise RuntimeError(
            f"command failed ({result.returncode}): {joined}\n{result.stderr.strip()}"
        )
    return result
