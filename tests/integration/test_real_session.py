"""Integration test against a real J-Link + N32G430C8 target.

Run with:
    pytest tests/integration -m requires_hardware

Requires:
  - A J-Link probe plugged in via USB
  - No other process holding the probe (close JLinkRTTViewer, JLink.exe, etc.)
  - N32G430C8 target running RTT on up-buffer 0

Uses jrtt's JLinkSession facade (which wraps pylink-square).
"""

from __future__ import annotations

import time

import pytest

from jrtt.jlink import JLinkSession, TIF


pytestmark = pytest.mark.requires_hardware

# Hardcoded for this fixture; override via env vars or pytest config if needed.
CHIP = "N32G430C8"
DLL_PATH = r"C:\Users\wangyu\.eide\tools\jlink\JLink_x64.dll"


@pytest.fixture(scope="module")
def session() -> JLinkSession:
    s = JLinkSession(dll_path=DLL_PATH)
    s.open(chip=CHIP, tif=TIF.SWD, speed_khz=4000)
    s.start_rtt()
    yield s
    s.stop_rtt()
    s.close()


def test_session_connected(session: JLinkSession) -> None:
    """Session reports itself open after .open() returned."""
    assert session is not None


def test_rtt_reads_real_bytes(session: JLinkSession) -> None:
    """The N32G430C8 is emitting FOC control logs; verify bytes flow."""
    deadline = time.monotonic() + 2.0
    collected = bytearray()
    while time.monotonic() < deadline:
        chunk = session.read_bytes(0, 4096)
        if chunk:
            collected.extend(chunk)
        else:
            time.sleep(0.05)
    print(f"\n  Read {len(collected)} bytes from up-buffer 0")
    print(f"  First 200 bytes: {bytes(collected[:200])!r}")
    assert len(collected) > 0, "RTT up-buffer 0 produced no bytes; is target printing?"


def test_rtt_reader_splits_lines_on_real_data(session: JLinkSession) -> None:
    """End-to-end: RttReader over the real JLinkSession yields complete lines."""
    from jrtt.rtt_reader import RttReader

    reader = RttReader(session)
    deadline = time.monotonic() + 2.0
    lines = []
    while time.monotonic() < deadline and len(lines) < 5:
        batch = reader.poll_once()
        if batch:
            lines.extend(batch)
        else:
            time.sleep(0.05)
    print(f"\n  Got {len(lines)} complete lines")
    for ln in lines[:3]:
        print(f"    {ln!r}")
    assert len(lines) >= 1
    for ln in lines:
        assert ln.data.endswith(b"\n"), f"Line not terminated: {ln.data!r}"