"""In-memory FakeJLinkSession with the same surface as JLinkSession.

Used by unit tests for business logic (line splitting, ring buffer,
filters) without any hardware.
"""

from __future__ import annotations

from typing import Optional

from .constants import TIF
from .session import JLinkSession  # for isinstance


class FakeJLinkSession:
    """Drop-in replacement for JLinkSession for tests."""

    def __init__(self) -> None:
        self.opened = False
        self.rtt_running = False
        self.chip = ""
        self.tif = TIF.SWD
        self.speed = 0
        self.sn: Optional[int] = None
        # per-buffer FIFO of byte chunks (mirrors FakeJLinkDll semantics)
        self._buffers: dict[int, list[bytes]] = {0: [], 1: [], 2: []}

    # ---- test API ----

    def inject(self, buf_index: int, data: bytes) -> None:
        self._buffers.setdefault(buf_index, []).append(bytes(data))

    def pending_bytes(self, buf_index: int) -> int:
        return sum(len(c) for c in self._buffers.get(buf_index, ()))

    # ---- JLinkSession-compatible API ----

    def open(self, *, sn=None, chip: str, tif: TIF = TIF.SWD, speed_khz: int = 4000) -> None:
        if self.opened:
            raise RuntimeError("Fake: already opened")
        self.opened = True
        self.chip = chip
        self.tif = tif
        self.speed = speed_khz
        self.sn = sn

    def start_rtt(self) -> None:
        if not self.opened:
            raise RuntimeError("Fake: start_rtt() before open()")
        self.rtt_running = True

    def stop_rtt(self) -> None:
        self.rtt_running = False

    def read_bytes(self, buf_index: int = 0, max_bytes: int = 4096) -> bytes:
        if not self.rtt_running:
            return b""
        chunks = self._buffers.get(buf_index)
        if not chunks:
            return b""
        out = bytearray()
        while chunks and len(out) < max_bytes:
            head = chunks[0]
            take = min(len(head), max_bytes - len(out))
            out.extend(head[:take])
            if take == len(head):
                chunks.pop(0)
            else:
                chunks[0] = head[take:]
        return bytes(out)

    def num_up_buffers(self) -> int:
        # Pretend any touched buffer exists
        return max([k for k, v in self._buffers.items() if v], default=0) + 1

    def close(self) -> None:
        self.opened = False
        self.rtt_running = False

    def __enter__(self) -> "FakeJLinkSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()