"""Fixed-capacity FIFO ring buffer for RTT lines.

Thread-safety: NOT thread-safe. Callers must serialise access (we'll do
that from a single RTT reader thread + GIL on dict/list ops).

Eviction: when capacity is exceeded, the OLDEST entry is dropped.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class RingEntry:
    ts: float          # daemon-local Unix timestamp (seconds, float)
    channel: int       # RTT up-buffer index
    data: bytes        # raw bytes (one logical line, includes trailing \n)


class RingBuffer:
    def __init__(self, capacity: int = 4096) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        self._buf: deque[RingEntry] = deque(maxlen=capacity)

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, entry: RingEntry) -> None:
        # deque(maxlen=...) evicts from the left when full
        self._buf.append(entry)

    def clear(self) -> None:
        self._buf.clear()

    def snapshot(self) -> list[RingEntry]:
        """Return a list copy of current contents, oldest first."""
        return list(self._buf)

    def last_n(self, n: int) -> list[RingEntry]:
        if n <= 0:
            return []
        if n >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-n:]

    def since(self, t: float) -> list[RingEntry]:
        """Return entries with ts >= t, oldest first.

        O(n) linear scan. n is bounded by capacity (default 4096) so this is
        cheap.
        """
        out: list[RingEntry] = []
        for e in self._buf:
            if e.ts >= t:
                out.append(e)
        return out