"""Tests for the Named Pipe transport.

These run on Windows only and use a unique pipe name per test so they
don't interfere with each other or with a real daemon.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid

import pytest

from jrtt.ipc import PipeClient, PipeServer, PipeError, Framing
from jrtt.protocol import Req, Res, encode_frame, decode_frame


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Named Pipes are Windows-only")


def _unique_pipe_name() -> str:
    # Win32 named pipe syntax: \\.\pipe\<name>. In Python source this is
    # written as either r"\\.\pipe\..." (raw, 2 backslashes) or
    # "\\\\.\\pipe\\..." (escaped). Using the raw form here.
    return r"\\.\pipe\jrtt_test_" + uuid.uuid4().hex[:8]


# ---- client connect to nonexistent pipe ----


def test_client_raises_when_no_server() -> None:
    name = _unique_pipe_name()
    with pytest.raises(PipeError):
        with PipeClient(name, timeout_ms=200):
            pass


# ---- single request/response roundtrip ----


def test_server_client_roundtrip() -> None:
    name = _unique_pipe_name()
    server = PipeServer(name)
    received: list[bytes] = []

    def handle(client_bytes: bytes) -> bytes:
        received.append(client_bytes)
        # Echo back a simple response
        return b"world\n"

    server_thread = threading.Thread(target=lambda: server.serve_one(handle, timeout_s=2.0), daemon=True)
    server_thread.start()

    with PipeClient(name, timeout_ms=2000) as client:
        client.send(b"hello\n")
        reply = client.recv(timeout_s=1.0)
    server_thread.join(timeout=2.0)
    assert reply == b"world\n"
    assert received == [b"hello\n"]


# ---- multi-frame stream ----


def test_server_handles_multiple_frames_in_one_read() -> None:
    name = _unique_pipe_name()
    server = PipeServer(name)
    received_lines: list[str] = []

    def handle(chunk: bytes) -> bytes:
        for line in chunk.splitlines(keepends=True):
            received_lines.append(line.decode().rstrip("\n"))
        return b"ack\n"

    server_thread = threading.Thread(
        target=lambda: server.serve_one(handle, timeout_s=2.0), daemon=True
    )
    server_thread.start()

    payload = b"line1\nline2\nline3\n"
    with PipeClient(name, timeout_ms=2000) as client:
        client.send(payload)
        ack = client.recv(timeout_s=1.0)
    server_thread.join(timeout=2.0)
    assert ack == b"ack\n"
    assert received_lines == ["line1", "line2", "line3"]


# ---- protocol integration ----


def test_server_protocol_roundtrip() -> None:
    """Wire a Req through the pipe and back as a Res via protocol codec."""
    name = _unique_pipe_name()

    def handle(chunk: bytes) -> bytes:
        # Server side: decode the req, build a Res, encode back
        assert chunk.endswith(b"\n")
        import json

        req = json.loads(chunk[:-1])
        assert req["op"] == "ping"
        res = Res(id=req["id"], ok=True, data={"roundtrip_ms": 1.5})
        return encode_frame(res)

    server_thread = threading.Thread(
        target=lambda: PipeServer(name).serve_one(handle, timeout_s=2.0), daemon=True
    )
    server_thread.start()

    with PipeClient(name, timeout_ms=2000) as client:
        req = Req(id="test-id", op="ping")
        client.send(encode_frame(req))
        reply_bytes = client.recv(timeout_s=1.0)
    server_thread.join(timeout=2.0)
    res = decode_frame(reply_bytes)
    assert res.id == "test-id"
    assert res.ok is True
    assert res.data == {"roundtrip_ms": 1.5}


# ---- server exits cleanly on stop() ----


def test_server_serve_loop_terminates_on_stop() -> None:
    """Calling stop() unblocks the accept loop within a short window."""
    name = _unique_pipe_name()
    server = PipeServer(name)

    def handle(_chunk: bytes) -> bytes:
        return b"x\n"

    t = threading.Thread(target=server.serve_loop, kwargs={"handler": handle}, daemon=True)
    t.start()
    # Give the loop time to enter ConnectNamedPipe (blocking wait for client)
    time.sleep(0.1)
    server.stop()
    t.join(timeout=2.0)
    assert not t.is_alive(), "serve_loop did not exit within 2s after stop()"


# ---- framing utilities ----


def test_framing_split() -> None:
    f = Framing()
    out = f.feed(b"partial")
    assert out == []
    out = f.feed(b" line\nrest")
    assert out == [b"partial line\n"]
    assert f.tail == b"rest"