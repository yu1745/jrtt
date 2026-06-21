"""jrtt dump — snapshot ring buffer."""

from __future__ import annotations

import argparse
import json
import sys
import time


def run(args: argparse.Namespace) -> int:
    from jrtt.cli_cmds.client import send_request

    req_args: dict = {}
    if hasattr(args, "last") and args.last is not None:
        req_args["last"] = args.last
    if hasattr(args, "since") and args.since is not None:
        # parse e.g. "30s", "2m", "1h"
        s = args.since
        unit = s[-1]
        try:
            n = int(s[:-1])
        except ValueError:
            print(f"jrtt: bad --since value: {s}", file=sys.stderr)
            return 1
        if unit == "s":
            req_args["since_seconds"] = n
        elif unit == "m":
            req_args["since_seconds"] = n * 60
        elif unit == "h":
            req_args["since_seconds"] = n * 3600
        else:
            print(f"jrtt: bad --since unit: {unit}", file=sys.stderr)
            return 1
    if hasattr(args, "channel") and args.channel is not None:
        req_args["channel"] = args.channel

    try:
        res = send_request(args.pipe, "dump", req_args, timeout_s=5.0)
    except Exception as e:
        print(f"jrtt: dump failed: {e}", file=sys.stderr)
        return 2
    if not res.ok:
        print(f"jrtt: dump not ok: {res.code} {res.msg}", file=sys.stderr)
        return 3 if res.code and res.code.startswith("E_JLINK") else 2

    lines = (res.data or {}).get("lines", [])
    json_out = getattr(args, "json", False)
    if json_out:
        for ln in lines:
            print(json.dumps(ln, ensure_ascii=False))
    else:
        for ln in lines:
            ts = ln.get("ts", 0)
            data = ln.get("data", "")
            if isinstance(data, str):
                sys.stdout.write(f"{time.strftime('%H:%M:%S', time.localtime(ts))}.{int((ts%1)*1000):03d} {data}")
                if not data.endswith("\n"):
                    sys.stdout.write("\n")
            else:
                sys.stdout.write(repr(data) + "\n")
    return 0