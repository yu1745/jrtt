"""Daemon orchestrator: JLinkSession + RTT reader + ring buffer + pipe accept.

Public entry: `run_daemon(pipe_name, dll_path, chip, tif, speed_khz)` — blocks
until shutdown.

Architecture:
    1. Open J-Link via pylink, start RTT.
    2. Spawn reader thread (polls RTT every 5ms, fills ring buffer).
    3. Spawn broadcaster thread (drains ring buffer into subscriber queues).
    4. Spawn pipe server (one client = one handler invocation in serve_one).
    5. Wait for shutdown signal; clean up.

Per-client handler `_handle_one_client` does request/response or long-lived
subscription (tail) over a single NDJSON-framed byte stream.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from jrtt import __version__
from jrtt.daemon.reader import RttDaemonReader
from jrtt.daemon.ring_buffer import RingBuffer
from jrtt.daemon.subscriber import Subscriber, SubscriberRegistry
from jrtt.ipc import PipeError, PipeServer
from jrtt.jlink import TIF, JLinkSession
from jrtt.protocol import (
    Evt,
    FrameParser,
    ProtocolError,
    Req,
    Res,
    encode_frame,
)


_TIF_MAP = {"swd": TIF.SWD, "jtag": TIF.JTAG}


@dataclass
class _ClientState:
    """Per-connection state for an active tail subscriber."""
    sub: Optional[Subscriber] = None
    pending_events: list[Evt] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)


# PipeServer wraps kernel32 — these helper accessors keep our handlers clean.


def _pipe_read_request(server: PipeServer, parser: FrameParser, timeout_s: float = 30.0) -> Optional[Req]:
    """Read enough bytes to form exactly one Req. Returns None on disconnect."""
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if buf.endswith(b"\n"):
            break
        try:
            chunk = server.read_chunk()
        except Exception:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    if not buf.endswith(b"\n"):
        return None
    try:
        frames = parser.feed(bytes(buf))
    except ProtocolError:
        return None
    if not frames:
        return None
    return frames[0] if isinstance(frames[0], Req) else None


def _pipe_send(server: PipeServer, frame) -> bool:
    try:
        server.send(encode_frame(frame))
        return True
    except Exception:
        return False


def _decode_for_wire(data: bytes) -> str:
    """Best-effort decode bytes → str for JSON wire. Falls back to hex repr."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return {"hex": data.hex()}


def _build_status(registry: SubscriberRegistry, ring: RingBuffer, started_at: float) -> dict:
    uptime_s = time.time() - started_at
    return {
        "daemon": {"pid": os.getpid(), "uptime_s": uptime_s, "version": __version__},
        "jlink": {"connected": True},  # if we got here, JLink is open
        "rtt": {"running": True},
        "ring_buffer": {"size": len(ring), "capacity": ring.capacity},
        "subscribers": registry.count,
    }


def _do_dump(ring: RingBuffer, args: dict) -> dict:
    last = args.get("last")
    since_s = args.get("since_seconds", 0)
    if since_s:
        since_ts = time.time() - since_s
        entries = ring.since(since_ts)
    elif last:
        entries = ring.last_n(int(last))
    else:
        entries = ring.snapshot()
    return {
        "lines": [
            {"ts": e.ts, "channel": e.channel, "data": _decode_for_wire(e.data)}
            for e in entries
        ]
    }


def run_daemon(*, pipe_name: str, dll_path: str | None, chip: str, tif: str, speed_khz: int) -> int:
    """Entry point for `jrtt -d`."""
    tif_enum = _TIF_MAP.get(tif.lower())
    if tif_enum is None:
        print(f"jrtt: bad --tif {tif}", file=sys.stderr)
        return 1

    print(
        f"jrtt daemon starting; pipe={pipe_name!r} chip={chip!r} tif={tif} speed={speed_khz}kHz",
        flush=True,
    )

    # 1. Open J-Link + start RTT
    try:
        session = JLinkSession(dll_path=dll_path)
        session.open(chip=chip, tif=tif_enum, speed_khz=speed_khz)
        session.start_rtt()
    except Exception as e:
        print(f"jrtt: failed to open J-Link: {e}", file=sys.stderr)
        return 3

    # 2. Ring + reader thread
    ring = RingBuffer(capacity=4096)
    registry = SubscriberRegistry()
    reader = RttDaemonReader(session=session, ring=ring, poll_ms=5)
    reader.start()

    # 3. Broadcaster: turns ring entries into Evt objects on subscriber queues
    client_states: dict[str, _ClientState] = {}
    states_lock = threading.Lock()
    shutdown_evt = threading.Event()
    started_at = time.time()

    def broadcaster():
        last_len = 0
        while not shutdown_evt.is_set():
            snap = ring.snapshot()
            if len(snap) > last_len:
                # New entries since last drain
                out = registry.collect_new(snap, since_ts=0.0)
                last_len = len(snap)
                with states_lock:
                    for sid, entries in out.items():
                        st = client_states.get(sid)
                        if st is None:
                            continue
                        for e in entries:
                            evt = Evt(
                                id=sid,
                                name="rtt.line",
                                data={
                                    "ts": e.ts,
                                    "channel": e.channel,
                                    "data": _decode_for_wire(e.data),
                                },
                            )
                            with st.condition:
                                st.pending_events.append(evt)
                                st.condition.notify_all()
            time.sleep(0.01)

    # 4. Pipe server accept loop (each client handled inline)
    server = PipeServer(pipe_name)

    def serve_loop_wrapper():
        while not shutdown_evt.is_set():
            try:
                server.create()
                _handle_one_client(
                    server, registry, client_states, states_lock, shutdown_evt,
                    ring=ring, started_at=started_at,
                )
            except PipeError:
                return
            except Exception as e:
                print(f"jrtt: serve_loop error: {e}", file=sys.stderr)
                return

    broadcaster_thread = threading.Thread(target=broadcaster, name="jrtt-broadcast", daemon=True)
    broadcaster_thread.start()
    server_thread = threading.Thread(target=serve_loop_wrapper, name="jrtt-pipe", daemon=True)
    server_thread.start()

    print(f"jrtt daemon ready (version {__version__})", flush=True)

    try:
        shutdown_evt.wait()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
        shutdown_evt.set()

    server.stop()
    server_thread.join(timeout=2.0)
    reader.stop(timeout_s=2.0)
    try:
        session.close()
    except Exception:
        pass
    print("jrtt daemon exited cleanly", flush=True)
    return 0


def _handle_one_client(
    server: PipeServer,
    registry: SubscriberRegistry,
    client_states: dict,
    states_lock: threading.Lock,
    shutdown_evt: threading.Event,
    *,
    ring: RingBuffer,
    started_at: float,
) -> None:
    """Service one connected client through a complete session."""
    parser = FrameParser()
    req = _pipe_read_request(server, parser)
    if req is None:
        return

    # One-shot ops
    if req.op == "ping":
        _pipe_send(server, Res(id=req.id, ok=True, data={"version": __version__}))
        return
    if req.op == "status":
        _pipe_send(server, Res(id=req.id, ok=True, data=_build_status(registry, ring, started_at)))
        return
    if req.op == "dump":
        _pipe_send(server, Res(id=req.id, ok=True, data=_do_dump(ring, req.args)))
        return
    if req.op == "shutdown":
        _pipe_send(server, Res(id=req.id, ok=True, data={}))
        shutdown_evt.set()
        return

    # Streaming op: tail
    if req.op == "tail":
        _serve_tail(server, registry, client_states, states_lock, req)
        return

    _pipe_send(server, Res(id=req.id, ok=False, code="E_UNKNOWN_OP", msg=f"unknown op: {req.op}"))


def _serve_tail(
    server: PipeServer,
    registry: SubscriberRegistry,
    client_states: dict,
    states_lock: threading.Lock,
    req: Req,
) -> None:
    """Register subscriber, ack client, then stream events until disconnect."""
    args = req.args or {}
    channel = int(args.get("channel", 0))
    regex_pat = args.get("regex")
    replay_last_n = int(args.get("replay_last_n", 0))
    max_lines = args.get("max_lines")
    since_seconds = float(args.get("since_seconds", 0))

    compiled_re: re.Pattern | None = None
    if regex_pat:
        try:
            compiled_re = re.compile(regex_pat.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8", errors="replace") if isinstance(regex_pat, str) else regex_pat)
        except re.error as e:
            _pipe_send(server, Res(id=req.id, ok=False, code="E_BAD_REGEX", msg=str(e)))
            return

    sub = Subscriber(
        id=req.id,
        channel=channel,
        regex=compiled_re,
        replay_last_n=replay_last_n,
    )
    state = _ClientState(sub=sub)
    registry.add(sub)
    with states_lock:
        client_states[req.id] = state
    if since_seconds:
        # Advance sub cursor past anything older than (now - since_seconds)
        cutoff = time.time() - since_seconds
        sub.last_index = sum(1 for e in ring.snapshot() if e.ts < cutoff)

    # Send ack Res so the client knows subscription is live
    if not _pipe_send(server, Res(id=req.id, ok=True, data={"subscribed": True})):
        registry.remove(req.id)
        with states_lock:
            client_states.pop(req.id, None)
        return

    emitted = 0
    try:
        while True:
            # Wait for pending events
            with state.condition:
                while not state.pending_events and not shutdown_evt.is_set():
                    state.condition.wait(timeout=0.5)
                if not state.pending_events:
                    if shutdown_evt.is_set():
                        return
                    continue
                evt = state.pending_events.pop(0)
            if not _pipe_send(server, evt):
                return
            emitted += 1
            if max_lines is not None and emitted >= int(max_lines):
                return
    finally:
        registry.remove(req.id)
        with states_lock:
            client_states.pop(req.id, None)