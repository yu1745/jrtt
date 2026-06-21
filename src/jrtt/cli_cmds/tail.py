"""jrtt tail — subscribe + stream RTT lines."""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time


def _parse_since(s: str) -> int:
    """Parse e.g. '10s', '2m', '1h' into seconds. Returns 0 if s is None/empty."""
    if not s:
        return 0
    unit = s[-1]
    try:
        n = int(s[:-1])
    except ValueError:
        raise ValueError(f"bad --since value: {s!r}")
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    raise ValueError(f"bad --since unit: {unit!r}")


def run(args: argparse.Namespace) -> int:
    from jrtt.cli_cmds.client import subscribe_events

    # argparse for tail subcommand (sub-flag bag, populated later via globals or wrapper).
    # We accept the values from the parent parse (set via the wrapper in cli.py
    # when "tail" is detected). For now, use getattr with defaults.
    channel = getattr(args, "channel", None)
    regex_pat = getattr(args, "regex", None)
    since_dur = getattr(args, "since", None)
    max_lines = getattr(args, "max_lines", None)
    json_out = getattr(args, "json", False)
    follow = getattr(args, "follow", False)  # default: print and exit (GNU tail)
    replay_n = getattr(args, "lines", None)
    if replay_n is None:
        replay_n = 10  # GNU-tail default: last 10 lines
    replay_n = int(replay_n)  # -n N → replay_last_n (0 = no replay)

    req_args: dict = {"channel": channel or 0, "follow": follow, "replay_last_n": int(replay_n or 0)}
    if regex_pat:
        try:
            req_args["regex"] = regex_pat.encode("utf-8").decode("unicode_escape")  # keep as string
        except Exception:
            req_args["regex"] = regex_pat
    if since_dur:
        try:
            req_args["since_seconds"] = _parse_since(since_dur)
        except ValueError as e:
            print(f"jrtt: {e}", file=sys.stderr)
            return 1
    if max_lines is not None:
        req_args["max_lines"] = int(max_lines)

    stopped = {"v": False}

    def _on_sigint(sig, frame):
        stopped["v"] = True

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:
        pass  # not main thread (e.g. tests)

    emitted = 0
    try:
        for evt in subscribe_events(args.pipe, "tail", req_args, timeout_s=300.0):
            if stopped["v"]:
                break
            data = evt.data
            ch = data.get("channel", 0)
            payload = data.get("data", "")
            ts = data.get("ts", time.time())
            if json_out:
                sys.stdout.write(json.dumps({"ts": ts, "channel": ch, "data": payload}) + "\n")
            else:
                if isinstance(payload, str):
                    sys.stdout.write(payload)
                    if not payload.endswith("\n"):
                        sys.stdout.write("\n")
                else:
                    sys.stdout.write(repr(payload) + "\n")
            sys.stdout.flush()
            emitted += 1
            if max_lines and emitted >= max_lines:
                break
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"jrtt: tail failed: {e}", file=sys.stderr)
        return 2
    return 0