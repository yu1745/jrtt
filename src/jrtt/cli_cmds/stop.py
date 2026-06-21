"""jrtt stop — ask daemon to shut down."""

from __future__ import annotations

import argparse


def run(args: argparse.Namespace) -> int:
    from jrtt.cli_cmds.client import send_request

    try:
        res = send_request(args.pipe, "shutdown", timeout_s=2.0)
    except Exception as e:
        print(f"jrtt: stop failed: {e}", file=sys.stderr)
        return 2
    if not res.ok:
        print(f"jrtt: stop not ok: {res.code} {res.msg}", file=sys.stderr)
        return 2
    print("daemon shutdown requested")
    return 0