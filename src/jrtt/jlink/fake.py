"""In-memory FakeJLinkDll for unit tests.

Behaviour matches SEGGER J-Link RTT Terminal semantics at the API boundary,
so RttReader / ring-buffer / filter logic can be exercised without hardware.

Test convenience API (not part of the abstract interface):
    inject(buf_index, data)   — push bytes into an up-buffer
    pending_bytes(buf_index)  — how many bytes are queued
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from .constants import RTTCommand, TIF
from .dll import JLinkDll, JLinkDllError
from .structs import EmuConnectInfo


class FakeJLinkDll(JLinkDll):
    def __init__(self) -> None:
        self._open = False
        self._connected = False
        self._rtt_running = False
        self._rtt_buffers: dict[int, deque[bytes]] = {}     # up-buffers: idx -> byte chunks
        self._rtt_down: dict[int, deque[bytes]] = {}        # down-buffers (write side)
        self._tif: int | None = None
        self._speed_khz: int = 0
        self._selected_sn: int | None = None
        self._version: int = 0x80000  # V8.00
        self._sn: int = 0x12345678
        self._probes: list[EmuConnectInfo] = []
        self._product_name: str = "FakeJLink"

    # ---- test API --------------------------------------------------------

    def add_probe(self, info: EmuConnectInfo) -> None:
        self._probes.append(info)

    def inject(self, buf_index: int, data: bytes | Iterable[int]) -> None:
        """Queue bytes into up-buffer for next rtt_read."""
        if isinstance(data, (bytes, bytearray)):
            chunk = bytes(data)
        else:
            chunk = bytes(data)
        self._rtt_buffers.setdefault(buf_index, deque()).append(chunk)

    def pending_bytes(self, buf_index: int) -> int:
        return sum(len(c) for c in self._rtt_buffers.get(buf_index, ()))

    # ---- JLinkDll interface ---------------------------------------------

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self._connected = False
        self._rtt_running = False

    def connect(self) -> None:
        if not self._open:
            raise JLinkDllError("Fake: connect() before open()")
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def get_dll_version(self) -> int:
        return self._version

    def get_sn(self) -> int:
        if self._selected_sn is not None:
            return self._selected_sn
        return self._sn

    def emu_get_list(self, host: int) -> list[EmuConnectInfo]:
        # First call with NULL buffer in real API -> returns count.
        # Our typed signature returns the list directly; we trust the caller.
        return list(self._probes)

    def emu_select_by_usb_sn(self, serial: int) -> None:
        for p in self._probes:
            if p.SerialNumber == serial:
                self._selected_sn = serial
                return
        raise JLinkDllError(f"Fake: probe SN={serial} not found")

    def tif_select(self, interface: TIF) -> None:
        if not self._open:
            raise JLinkDllError("Fake: tif_select() before open()")
        self._tif = int(interface)

    def set_speed(self, khz: int) -> None:
        self._speed_khz = khz

    def rtt_control(self, cmd: RTTCommand, p: int = 0) -> int:
        if cmd == RTTCommand.START:
            self._rtt_running = True
            return 0
        if cmd == RTTCommand.STOP:
            self._rtt_running = False
            return 0
        if cmd == RTTCommand.GETNUMBUF:
            # Buffer count is max(up-buffer index) + 1, or 1 if none have been touched.
            return max(self._rtt_buffers.keys(), default=-1) + 1 if self._rtt_buffers else 1
        if cmd == RTTCommand.GETSTAT:
            return 0 if self._rtt_running else -1
        if cmd == RTTCommand.GETDESC:
            return 0
        return -1  # unknown cmd

    def rtt_read(self, buf_index: int, size: int) -> bytes:
        if not self._rtt_running:
            return b""
        chunks = self._rtt_buffers.get(buf_index)
        if not chunks:
            return b""
        out = bytearray()
        while chunks and len(out) < size:
            head = chunks[0]
            take = min(len(head), size - len(out))
            out.extend(head[:take])
            if take == len(head):
                chunks.popleft()
            else:
                chunks[0] = head[take:]
        return bytes(out)

    def rtt_write(self, buf_index: int, data: bytes) -> int:
        if not self._rtt_running:
            return 0
        self._rtt_down.setdefault(buf_index, deque()).append(data)
        return len(data)

    # ---- extras ----------------------------------------------------------

    def get_product_name(self) -> str:
        return self._product_name