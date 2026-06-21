"""Daemon-side RTT reader loop.

Single thread that polls the JLinkSession's RTT buffer, splits bytes into
lines (via RttReader), and appends them to a RingBuffer.

Lifecycle:
    reader = RttDaemonReader(session, ring)
    reader.start()           # spawns background thread
    reader.poll_once()       # one synchronous poll (used by tests)
    reader.stop()            # joins the thread
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from jrtt.daemon.ring_buffer import RingBuffer, RingEntry
from jrtt.jlink.session import JLinkSession  # for type
from jrtt.rtt_reader import RttReader


@dataclass
class RttDaemonReader:
    session: JLinkSession
    ring: RingBuffer
    channel: int = 0
    poll_ms: int = 5

    # Internal state populated by start()
    _thread: threading.Thread | None = None
    _stop_evt: threading.Event | None = None
    _reader: RttReader | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("reader already started")
        self._stop_evt = threading.Event()
        self._reader = RttReader(self.session, channel=self.channel, poll_idle_ms=self.poll_ms)
        self._thread = threading.Thread(target=self._loop, name="jrtt-rtt-reader", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_s: float = 2.0) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> int:
        """Pull bytes, split into lines, append to ring buffer.

        Returns number of lines appended.
        """
        if self._reader is None:
            self._reader = RttReader(self.session, channel=self.channel, poll_idle_ms=self.poll_ms)
        lines = self._reader.poll_once()
        for ln in lines:
            self.ring.append(RingEntry(ts=ln.received_at, channel=ln.channel, data=ln.data))
        return len(lines)

    # ---- internals ----

    def _loop(self) -> None:
        assert self._stop_evt is not None
        poll_s = self.poll_ms / 1000.0
        while not self._stop_evt.is_set():
            try:
                self.poll_once()
            except Exception:
                # Swallow & back off — never let the reader thread die silently
                self._stop_evt.wait(poll_s * 10)
                continue
            self._stop_evt.wait(poll_s)