"""Spawn the daemon subprocess when needed."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from jrtt.ipc import pipe_exists


def ensure_daemon(pipe_name: str, chip: str, *, dll: str | None = None, timeout_s: float = 5.0) -> bool:
    """Make sure a jrtt daemon is running and listening on pipe_name.

    Returns True if a daemon is (or becomes) available; False on failure.

    Detection order:
      1. WaitNamedPipe(pipe_name, 0) — if True, daemon is already running.
      2. Otherwise spawn `jrtt -d` as a detached child.
      3. Wait up to timeout_s for the new daemon to come up.
    """
    if pipe_exists(pipe_name, wait_ms=10):
        return True

    # Spawn detached daemon
    args = [sys.executable, "-m", "jrtt", "-d", "--chip", chip, "--pipe", pipe_name]
    if dll:
        args.extend(["--dll", dll])
    try:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so daemon survives
        # parent exit (agent shells are short-lived).
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        print(f"jrtt: failed to spawn daemon: {e}", file=sys.stderr)
        return False

    # Poll for pipe to appear
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pipe_exists(pipe_name, wait_ms=50):
            return True
    return False