"""CLI entry point: argparse + role dispatch.

Two roles:
  1. Daemon  — `jrtt -d` (no subcommand)
  2. CLI     — any other invocation; spawns daemon if missing, then connects

CLI subcommands:
  tail   — stream RTT lines (GNU tail-compatible flags)
  dump   — snapshot ring buffer
  status — daemon/JLink/RTT state
  ping   — health probe
  stop   — shut down daemon
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from jrtt import __version__
from jrtt.cli_cmds import tail as cmd_tail
from jrtt.cli_cmds import dump as cmd_dump
from jrtt.cli_cmds import status as cmd_status
from jrtt.cli_cmds import ping as cmd_ping
from jrtt.cli_cmds import stop as cmd_stop
from jrtt.daemon.daemon import run_daemon


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jrtt",
        description="CLI + daemon for SEGGER J-Link RTT. Agent-first.",
    )
    p.add_argument("--version", action="version", version=f"jrtt {__version__}")
    # Global flag: enter daemon role (no subcommand)
    p.add_argument(
        "-d",
        "--daemon",
        action="store_true",
        help="Run as daemon (foreground). With a subcommand, behaves as if no -d (idempotent).",
    )
    # Common connection options
    p.add_argument(
        "--pipe",
        default=r"\\.\pipe\jrtt",
        help="Named pipe name (default: %(default)s)",
    )
    p.add_argument(
        "--dll",
        default=None,
        help="Path to JLink_x64.dll (default: auto-detect)",
    )
    p.add_argument(
        "--chip",
        default="N32G430C8",
        help="Target chip name as SEGGER knows it (default: %(default)s)",
    )
    p.add_argument(
        "--tif",
        choices=["swd", "jtag"],
        default="swd",
        help="Target interface (default: %(default)s)",
    )
    p.add_argument(
        "--speed",
        type=int,
        default=4000,
        help="SWD/JTAG speed in kHz, 0=adaptive (default: %(default)s)",
    )

    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("tail", help="Stream RTT lines (GNU tail-compatible)").add_argument_group("tail")
    sub.add_parser("dump", help="Snapshot ring buffer")
    sub.add_parser("status", help="Show daemon/JLink/RTT state")
    sub.add_parser("ping", help="Health probe")
    sub.add_parser("stop", help="Shut down daemon")

    return p


def main(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv))

    # Role: daemon if -d given and no subcommand
    if args.daemon and args.cmd is None:
        return run_daemon(
            pipe_name=args.pipe,
            dll_path=args.dll,
            chip=args.chip,
            tif=args.tif,
            speed_khz=args.speed,
        )

    # CLI role: ensure daemon exists, then dispatch
    from jrtt.spawn import ensure_daemon

    if not ensure_daemon(pipe_name=args.pipe, chip=args.chip):
        print("jrtt: failed to ensure daemon is running", file=sys.stderr)
        return 2

    if args.cmd == "ping":
        return cmd_ping.run(args)
    if args.cmd == "status":
        return cmd_status.run(args)
    if args.cmd == "dump":
        return cmd_dump.run(args)
    if args.cmd == "tail":
        return cmd_tail.run(args)
    if args.cmd == "stop":
        return cmd_stop.run(args)

    parser.print_help()
    return 1