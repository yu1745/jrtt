"""Unit tests for RttReader — pure logic, no hardware."""

from __future__ import annotations

import time

import pytest

from jrtt.jlink import FakeJLinkSession
from jrtt.rtt_reader import RttReader, RttLine


@pytest.fixture
def session() -> FakeJLinkSession:
    s = FakeJLinkSession()
    s.open(chip="N32G430C8")
    s.start_rtt()
    return s


def test_empty_when_no_data(session: FakeJLinkSession) -> None:
    r = RttReader(session)
    assert r.poll_once() == []
    assert r.drain_partial() == b""


def test_single_complete_line(session: FakeJLinkSession) -> None:
    session.inject(0, b"hello world\n")
    r = RttReader(session)
    lines = r.poll_once()
    assert len(lines) == 1
    assert lines[0].data == b"hello world\n"
    assert lines[0].channel == 0


def test_multiple_lines_in_one_chunk(session: FakeJLinkSession) -> None:
    session.inject(0, b"line1\nline2\nline3\n")
    r = RttReader(session)
    lines = r.poll_once()
    assert [ln.data for ln in lines] == [b"line1\n", b"line2\n", b"line3\n"]


def test_partial_line_held_over(session: FakeJLinkSession) -> None:
    """Bytes without trailing \\n must stay buffered until next chunk."""
    session.inject(0, b"hello ")  # no newline
    r = RttReader(session)
    assert r.poll_once() == []  # partial held, no complete line

    # Now add the rest (NOT calling drain_partial, because that would empty the buffer)
    session.inject(0, b"world\n")
    lines = r.poll_once()
    assert len(lines) == 1
    assert lines[0].data == b"hello world\n"


def test_split_line_across_two_chunks(session: FakeJLinkSession) -> None:
    """A line split mid-stream across two reads must reassemble."""
    session.inject(0, b"hello wo")
    r = RttReader(session)
    assert r.poll_once() == []  # partial held
    session.inject(0, b"rld\n")
    lines = r.poll_once()
    assert len(lines) == 1
    assert lines[0].data == b"hello world\n"


def test_crlf_normalised(session: FakeJLinkSession) -> None:
    """\\r\\n should yield one line (not two)."""
    session.inject(0, b"line1\r\nline2\r\n")
    r = RttReader(session)
    lines = r.poll_once()
    assert len(lines) == 2
    assert lines[0].data == b"line1\n"
    assert lines[1].data == b"line2\n"


def test_real_foc_style_log(session: FakeJLinkSession) -> None:
    """Sanity-check against a realistic FOC log line."""
    line = (
        b"foc[105968999] cnt=105969007 us=37 mode=0 rpm=0 vref=0 "
        b"pos=2147483647mdeg theta=5786mrad id=0mA iq=0mA vd=0mV vq=0mV "
        b"va=0mV vb=0mV ov=7\r\n"
    )
    session.inject(0, line)
    r = RttReader(session)
    lines = r.poll_once()
    assert len(lines) == 1
    assert b"foc[105968999]" in lines[0].data
    assert lines[0].decode().endswith("\n")


def test_binary_garbage_does_not_crash(session: FakeJLinkSession) -> None:
    """Random binary should still split on \\n without UTF-8 errors."""
    session.inject(0, b"\x00\x01\x02\n\xff\xfe\n")
    r = RttReader(session)
    lines = r.poll_once()
    assert len(lines) == 2
    # decode() uses 'replace' by default, must not raise
    for ln in lines:
        s = ln.decode()
        assert isinstance(s, str)


def test_lines_iterator_blocks_until_data(
    session: FakeJLinkSession,
) -> None:
    """The blocking iterator must raise TimeoutError when nothing arrives."""
    r = RttReader(session, poll_idle_ms=5)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        next(r.lines(timeout_s=0.05))
    elapsed = time.monotonic() - start
    assert 0.04 < elapsed < 1.0  # honoured timeout


def test_lines_iterator_yields_in_order(
    session: FakeJLinkSession,
) -> None:
    """Data arriving across multiple polls should emit in order."""
    r = RttReader(session, poll_idle_ms=2)
    session.inject(0, b"a\nb\n")
    seen: list[bytes] = []
    gen = r.lines(timeout_s=0.5)
    # Take first 2 lines
    for _ in range(2):
        try:
            seen.append(next(gen).data)
        except StopIteration:
            break
    # Generator runs forever; we manually break by closing the session
    session.close()
    assert seen == [b"a\n", b"b\n"]


def test_drain_partial_on_shutdown(session: FakeJLinkSession) -> None:
    """drain_partial returns whatever was buffered without a newline."""
    session.inject(0, b"partial-no-newline")
    r = RttReader(session)
    assert r.poll_once() == []
    drained = r.drain_partial()
    assert drained == b"partial-no-newline"
    # After drain, internal buffer should be empty
    assert r.drain_partial() == b""