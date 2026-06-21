"""Tests for the daemon's RTT reader loop.

Uses FakeJLinkSession to feed bytes; verifies the loop fills the ring buffer
correctly, can be stopped cleanly, and respects poll cadence.
"""

from __future__ import annotations

import threading
import time

import pytest

from jrtt.daemon.reader import RttDaemonReader
from jrtt.daemon.ring_buffer import RingBuffer
from jrtt.jlink import FakeJLinkSession


def _make_session() -> FakeJLinkSession:
    s = FakeJLinkSession()
    s.open(chip="N32G430C8")
    s.start_rtt()
    return s


def test_reader_fills_buffer_with_lines() -> None:
    session = _make_session()
    rb = RingBuffer(capacity=100)
    reader = RttDaemonReader(session=session, ring=rb, poll_ms=5)
    session.inject(0, b"line1\nline2\nline3\n")
    # Run poll_once synchronously; should pull all 3 lines.
    n = reader.poll_once()
    assert n == 3
    snap = rb.snapshot()
    assert [e.data.decode() for e in snap] == ["line1\n", "line2\n", "line3\n"]


def test_reader_handles_partial_line() -> None:
    session = _make_session()
    rb = RingBuffer(capacity=100)
    reader = RttDaemonReader(session=session, ring=rb, poll_ms=5)
    session.inject(0, b"hello ")
    n = reader.poll_once()
    assert n == 0
    assert rb.snapshot() == []
    session.inject(0, b"world\n")
    n = reader.poll_once()
    assert n == 1
    assert rb.snapshot()[0].data == b"hello world\n"


def test_reader_thread_starts_and_stops() -> None:
    session = _make_session()
    rb = RingBuffer(capacity=100)
    reader = RttDaemonReader(session=session, ring=rb, poll_ms=5)
    reader.start()
    try:
        # Inject bytes; reader thread should pick them up
        time.sleep(0.05)
        session.inject(0, b"a\nb\nc\n")
        # Give the thread a few polls
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(rb) < 3:
            time.sleep(0.02)
        assert len(rb) >= 3
    finally:
        reader.stop()
    # Thread should exit cleanly within a short window
    assert not reader.is_alive()


def test_reader_thread_survives_empty_polls() -> None:
    """No data flowing must not cause errors or tight CPU loops."""
    session = _make_session()
    rb = RingBuffer(capacity=10)
    reader = RttDaemonReader(session=session, ring=rb, poll_ms=20)
    reader.start()
    time.sleep(0.15)  # many empty polls
    assert reader.is_alive()
    reader.stop()
    assert len(rb) == 0


def test_reader_records_timestamps() -> None:
    session = _make_session()
    rb = RingBuffer(capacity=10)
    reader = RttDaemonReader(session=session, ring=rb, poll_ms=5)
    session.inject(0, b"x\n")
    t0 = time.time()
    reader.poll_once()
    t1 = time.time()
    assert len(rb) == 1
    e = rb.snapshot()[0]
    assert t0 <= e.ts <= t1 + 0.01  # small slack


def test_reader_channel_field() -> None:
    session = _make_session()
    rb = RingBuffer(capacity=10)
    reader = RttDaemonReader(session=session, ring=rb, channel=1, poll_ms=5)
    session.inject(1, b"hi from ch1\n")
    reader.poll_once()
    assert rb.snapshot()[0].channel == 1