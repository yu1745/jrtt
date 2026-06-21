"""RttReader — turn a stream of RTT bytes into a stream of lines.

The MCU emits bytes at high rate over up-buffer 0. We poll the J-Link
session, accumulate partial lines, and emit complete lines (with trailing
\\n preserved) to the consumer.

Line splitter:
    * Splits on \\n.
    * Preserves the \\n in the output (so callers see the exact bytes).
    * A partial line at end-of-stream stays in the buffer for the next poll.
    * \\r\\n is normalised to \\n at the boundary (so a line ending in \\r\\n
      reads as one line, not two empty ones).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Protocol


class RttSource(Protocol):
    """Anything that can produce bytes from a J-Link RTT buffer."""

    def read_bytes(self, buf_index: int = 0, max_bytes: int = 4096) -> bytes: ...


@dataclass
class RttLine:
    """One logical line of RTT output, with provenance."""

    channel: int
    data: bytes
    received_at: float = field(default_factory=time.time)

    def decode(self, errors: str = "replace") -> str:
        return self.data.decode("utf-8", errors=errors)

    def __repr__(self) -> str:
        body = self.decode().rstrip("\n").rstrip("\r")
        return f"RttLine(ch={self.channel}, len={len(self.data)}, body={body!r})"


class RttReader:
    """Stateful line splitter over an RttSource.

    Usage:
        reader = RttReader(session)
        for line in reader.lines():        # blocking iterator
            print(line.decode(), end='')
    """

    def __init__(self, source: RttSource, channel: int = 0, poll_idle_ms: int = 50):
        self._source = source
        self._channel = channel
        self._poll_idle = poll_idle_ms / 1000.0
        # partial buffer per channel (we only support 1 channel for v1)
        self._partial = bytearray()

    def poll_once(self) -> list[RttLine]:
        """Pull available bytes and return any complete lines."""
        chunk = self._source.read_bytes(self._channel, 4096)
        if not chunk:
            return []
        self._partial.extend(chunk)

        lines: list[RttLine] = []
        while True:
            # Find first \n in partial
            nl = self._partial.find(b"\n")
            if nl == -1:
                break
            line_bytes = bytes(self._partial[: nl + 1])
            del self._partial[: nl + 1]
            # Normalise trailing \r\n -> \n (strip the \r, keep the \n)
            if line_bytes.endswith(b"\r\n"):
                line_bytes = line_bytes[:-2] + b"\n"
            lines.append(RttLine(channel=self._channel, data=line_bytes))
        return lines

    def lines(self, *, timeout_s: float | None = None) -> Iterator[RttLine]:
        """Blocking iterator yielding RttLine as they become available.

        timeout_s=None means infinite. Otherwise raises TimeoutError if no
        line arrives within the timeout window.
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            batch = self.poll_once()
            if batch:
                for line in batch:
                    yield line
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("RttReader: no data within timeout")
            time.sleep(self._poll_idle)

    def drain_partial(self) -> bytes:
        """Return any partial line data the MCU sent without a trailing \\n.

        Useful for shutdown — caller can log it as a 'truncated' line.
        """
        out = bytes(self._partial)
        self._partial.clear()
        return out