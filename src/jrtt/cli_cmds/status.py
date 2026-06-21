"""jrtt status — show daemon / JLink / RTT state."""

from __future__ import annotations

import argparse
import json
import sys


def run(args: argparse.Namespace) -> int:
    from jrtt.cli_cmds.client import send_request

    try:
        res = send_request(args.pipe, "status", timeout_s=2.0)
    except Exception as e:
        print(f"jrtt: status failed: {e}", file=sys.stderr)
        return 2
    if not res.ok:
        print(f"jrtt: status not ok: {res.code} {res.msg}", file=sys.stderr)
        return 2
    data = res.data or {}
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        # Human-readable
        d = data.get("daemon", {})
        j = data.get("jlink", {})
        r = data.get("rtt", {})
        rb = data.get("ring_buffer", {})
        subs = data.get("subscribers", 0)
        print(f"daemon:    up (pid {d.get('pid')}, uptime {d.get('uptime_s', 0):.0f}s)")
        if j.get("connected"):
            print(f"jlink:     connected (SN {j.get('sn')}, {j.get('tif')}, {j.get('speed_khz')} kHz)")
            print(f"device:    {j.get('chip')}")
        else:
            print(f"jlink:     disconnected ({j.get('last_error') or 'no session'})")
        if r.get("running"):
            bufs = r.get("up_buffers", [])
            print(f"rtt:       active ({', '.join(f'ch{i}={s}B' for i, s in enumerate(bufs))})")
        else:
            print(f"rtt:       inactive ({r.get('reason') or 'not started'})")
        print(f"subscribers: {subs}")
        print(f"ring buffer: {rb.get('size')}/{rb.get('capacity')} lines")
    return 0