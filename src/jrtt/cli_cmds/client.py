"""Shared client-side helpers for subcommands."""

from __future__ import annotations

import socket
import sys
import uuid
from typing import Iterator

from jrtt.ipc import PipeClient
from jrtt.protocol import FrameParser, Req, Res, Evt, encode_frame


def send_request(pipe_name: str, op: str, args: dict | None = None, *, timeout_s: float = 5.0) -> Res:
    """Send one request, wait for response, return Res."""
    req = Req(id=uuid.uuid4().hex, op=op, args=args or {})
    with PipeClient(pipe_name, timeout_ms=int(timeout_s * 1000)) as client:
        client.send(encode_frame(req))
        buf = client.recv(timeout_s=timeout_s)
        parser = FrameParser()
        frames = parser.feed(buf)
        if not frames:
            raise RuntimeError("daemon closed connection without responding")
        res = frames[0]
        if not isinstance(res, Res):
            raise RuntimeError(f"unexpected frame: {res}")
        return res


def subscribe_events(pipe_name: str, op: str, args: dict | None = None, *, timeout_s: float = 5.0) -> Iterator[Evt]:
    """Send op (e.g. 'tail'), yield events until pipe closes or error."""
    req = Req(id=uuid.uuid4().hex, op=op, args=args or {})
    with PipeClient(pipe_name, timeout_ms=int(timeout_s * 1000)) as client:
        client.send(encode_frame(req))
        parser = FrameParser()
        while True:
            buf = client.recv(timeout_s=timeout_s)
            if not buf:
                return
            for frame in parser.feed(buf):
                if isinstance(frame, Evt):
                    yield frame
                elif isinstance(frame, Res):
                    if not frame.ok:
                        raise RuntimeError(f"server error: {frame.code} {frame.msg}")
                    # initial ack (ok=True) — drain and continue
                    continue