"""Tests for RingBuffer."""

from __future__ import annotations

import time

import pytest

from jrtt.daemon.ring_buffer import RingBuffer, RingEntry


def make_entry(s: str, ts: float | None = None) -> RingEntry:
    return RingEntry(ts=ts if ts is not None else time.time(), channel=0, data=s.encode())


def test_empty_buffer() -> None:
    rb = RingBuffer(capacity=4)
    assert rb.snapshot() == []
    assert len(rb) == 0


def test_append_and_snapshot() -> None:
    rb = RingBuffer(capacity=4)
    rb.append(make_entry("a"))
    rb.append(make_entry("b"))
    snap = rb.snapshot()
    assert [e.data.decode() for e in snap] == ["a", "b"]
    assert len(rb) == 2


def test_eviction_at_capacity() -> None:
    rb = RingBuffer(capacity=3)
    for c in ["a", "b", "c", "d", "e"]:
        rb.append(make_entry(c))
    snap = rb.snapshot()
    assert [e.data.decode() for e in snap] == ["c", "d", "e"]
    assert len(rb) == 3


def test_snapshot_returns_copy() -> None:
    """Mutating the returned list must not affect the buffer."""
    rb = RingBuffer(capacity=4)
    rb.append(make_entry("a"))
    s = rb.snapshot()
    s.clear()
    assert len(rb) == 1


def test_last_n() -> None:
    rb = RingBuffer(capacity=100)
    for i in range(20):
        rb.append(make_entry(f"line{i}"))
    snap = rb.last_n(5)
    assert [e.data.decode() for e in snap] == ["line15", "line16", "line17", "line18", "line19"]


def test_since() -> None:
    """since(t) returns entries with ts >= t, oldest first."""
    rb = RingBuffer(capacity=10)
    rb.append(make_entry("old", ts=100.0))
    rb.append(make_entry("mid", ts=200.0))
    rb.append(make_entry("new", ts=300.0))
    snap = rb.since(150.0)
    assert [e.data.decode() for e in snap] == ["mid", "new"]


def test_since_with_evicted_entries() -> None:
    """since() respects what is actually still in the buffer."""
    rb = RingBuffer(capacity=2)
    rb.append(make_entry("a", ts=1.0))
    rb.append(make_entry("b", ts=2.0))
    rb.append(make_entry("c", ts=3.0))  # evicts a
    snap = rb.since(0.0)
    assert [e.data.decode() for e in snap] == ["b", "c"]


def test_clear() -> None:
    rb = RingBuffer(capacity=4)
    for c in ["a", "b", "c"]:
        rb.append(make_entry(c))
    rb.clear()
    assert len(rb) == 0
    assert rb.snapshot() == []


def test_unicode_data() -> None:
    rb = RingBuffer(capacity=2)
    rb.append(RingEntry(ts=0.0, channel=0, data="你好世界\n".encode("utf-8")))
    snap = rb.snapshot()
    assert snap[0].data.decode("utf-8") == "你好世界\n"


def test_channel_filtering_via_constructor() -> None:
    """RingBuffer doesn't filter; caller filters. Verify the data is preserved."""
    rb = RingBuffer(capacity=10)
    rb.append(RingEntry(ts=1.0, channel=0, data=b"ch0"))
    rb.append(RingEntry(ts=2.0, channel=1, data=b"ch1"))
    rb.append(RingEntry(ts=3.0, channel=0, data=b"ch0-2"))
    ch0 = [e.data for e in rb.snapshot() if e.channel == 0]
    assert ch0 == [b"ch0", b"ch0-2"]