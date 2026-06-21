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


def _acquire_daemon_lock() -> int | None:
    """Try to acquire a named Win32 mutex. Returns None on success (first daemon),
    or exit code 5 if another daemon is already running."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    MUTEX_NAME = "jrtt_daemon"
    h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if h is None or h == 0:
        return None  # can't check, let it proceed
    err = ctypes.get_last_error()
    ERROR_ALREADY_EXISTS = 183
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return 5
    return None  # first instance, keep the handle alive for the daemon's lifetime

def run_daemon(*, pipe_name: str, dll_path: str | None, chip: str, tif: str, speed_khz: int) -> int:
    """Entry point for `jrtt -d`."""
    lock_exit = _acquire_daemon_lock()
    if lock_exit is not None:
        print(
            "jrtt: another daemon is already running. Use `jrtt stop` to shut it down.",
            file=sys.stderr,
        )
        return lock_exit
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
                print("[SRV-LOOP] _handle_one_client returned", flush=True)
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
        while not shutdown_evt.wait(timeout=0.5):
            pass
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
    """Accept one connection and service it end-to-end (one op or tail)."""
    parser = FrameParser()

    def handler(srv: PipeServer) -> None:
        # Read first request
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = srv.read_chunk()
            if not chunk:
                return  # client disconnected before sending anything
            buf.extend(chunk)
        try:
            frames = parser.feed(bytes(buf))
        except ProtocolError as e:
            srv.send(encode_frame(Res(id="?", ok=False, code="E_PROTOCOL", msg=str(e))))
            return
        if not frames:
            return
        req = frames[0]
        if not isinstance(req, Req):
            srv.send(encode_frame(Res(id="?", ok=False, code="E_PROTOCOL",
                                      msg=f"expected req, got {type(req).__name__}")))
            return

        # Dispatch
        if req.op == "ping":
            srv.send(encode_frame(Res(id=req.id, ok=True, data={"version": __version__})))
            return
        if req.op == "status":
            srv.send(encode_frame(Res(id=req.id, ok=True,
                                      data=_build_status(registry, ring, started_at))))
            return
        if req.op == "dump":
            srv.send(encode_frame(Res(id=req.id, ok=True, data=_do_dump(ring, req.args))))
            return
        if req.op == "shutdown":
            srv.send(encode_frame(Res(id=req.id, ok=True, data={})))
            shutdown_evt.set()
            return
        if req.op == "tail":
            # Streaming: ack + stream events until client disconnect.
            if not srv.send(encode_frame(Res(id=req.id, ok=True, data={"subscribed": True}))):
                return
            _serve_tail(srv, registry, client_states, states_lock, req, ring=ring, shutdown_evt=shutdown_evt)
            return
        srv.send(encode_frame(Res(id=req.id, ok=False, code="E_UNKNOWN_OP",
                                  msg=f"unknown op: {req.op}")))

    server.serve_one(handler, timeout_s=60.0)


def _serve_tail(
    server: PipeServer,
    registry: SubscriberRegistry,
    client_states: dict,
    states_lock: threading.Lock,
    req: Req,
    *,
    ring: RingBuffer,
    shutdown_evt: threading.Event,
) -> None:
    """Stream ring-buffer events to a tail client.

    Two modes (GNU-tail compatible):
      follow=False  — send the requested slice of the ring buffer, then
                      return. Client sees pipe close and exits. No
                      subscriber registration, no broadcaster involved.
      follow=True   — register as a subscriber; the broadcaster feeds
                      replay + live events until the client disconnects.
    """
    args = req.args or {}
    follow = bool(args.get("follow", False))
    channel = int(args.get("channel", 0))
    regex_pat = args.get("regex")
    replay_last_n = int(args.get("replay_last_n", 0))
    max_lines = args.get("max_lines")
    since_seconds = float(args.get("since_seconds", 0))

    compiled_re: re.Pattern | None = None
    if regex_pat:
        try:
            if isinstance(regex_pat, str):
                compiled_re = re.compile(regex_pat.encode("utf-8"))
            else:
                compiled_re = re.compile(regex_pat)
        except re.error as e:
            server.send(encode_frame(Res(id=req.id, ok=False, code="E_BAD_REGEX", msg=str(e))))
            return

    # Non-following path: snapshot, filter, send, return. No broadcaster.
    if not follow:
        snap = ring.snapshot()
        if since_seconds > 0:
            cutoff = time.time() - since_seconds
            snap = [e for e in snap if e.ts >= cutoff]
        if channel:
            snap = [e for e in snap if e.channel == channel]
        if compiled_re is not None:
            snap = [e for e in snap if compiled_re.search(e.data)]
        # `-n N` is replay_last_n; `--max-lines M` in non-follow mode is
        # also a cap on the slice. Take the larger of the two (whichever
        # the user supplied) and tail-end.
        cap = max(replay_last_n, int(max_lines) if max_lines is not None else 0)
        if cap > 0:
            snap = snap[-cap:]
        if not server.send(encode_frame(Res(id=req.id, ok=True))):
            return
        for e in snap:
            evt = Evt(
                id=req.id,
                name="rtt.line",
                data={"ts": e.ts, "channel": e.channel, "data": _decode_for_wire(e.data)},
            )
            if not server.send(encode_frame(evt)):
                return
        return

    # Following path: subscriber + broadcaster.
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
        cutoff = time.time() - since_seconds
        sub.last_index = sum(1 for e in ring.snapshot() if e.ts < cutoff)

    emitted = 0
    try:
        while True:
            with state.condition:
                while not state.pending_events and not shutdown_evt.is_set():
                    state.condition.wait(timeout=0.5)
                if not state.pending_events:
                    if shutdown_evt.is_set():
                        return
                    continue
                evt = state.pending_events.pop(0)
            if not server.send(encode_frame(evt)):
                return
            emitted += 1
            if max_lines is not None and emitted >= int(max_lines):
                return
    finally:
        registry.remove(req.id)
        with states_lock:
            client_states.pop(req.id, None)