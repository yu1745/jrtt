"""jrtt ping — health probe."""

from __future__ import annotations

import argparse
import sys
import time


def run(args: argparse.Namespace) -> int:
    from jrtt.cli_cmds.client import send_request

    t0 = time.monotonic()
    try:
        res = send_request(args.pipe, "ping", timeout_s=2.0)
    except Exception as e:
        print(f"jrtt: ping failed: {e}", file=sys.stderr)
        return 2
    dt_ms = (time.monotonic() - t0) * 1000
    if not res.ok:
        print(f"jrtt: ping not ok: {res.code} {res.msg}", file=sys.stderr)
        return 2
    version = res.data.get("version", "?") if res.data else "?"
    print(f"pong roundtrip={dt_ms:.1f}ms version={version}")
    return 0