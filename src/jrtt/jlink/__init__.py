"""J-Link RTT access.

The real hardware path uses pylink-square (Apache 2.0) as our ctypes layer.
We do not reimplement what pylink already implements well (RTT API, EMU
enumeration, stdcall/cdecl plumbing, JLock). We only wrap it with a thinner
interface that suits jrtt's needs and add a Fake implementation for
business-logic tests.

Public API:
    JLinkSession      — high-level facade: open(sn, chip, tif, speed) → rtt_start → read lines
    FakeJLinkSession  — same interface, in-memory, no hardware
    JLinkDll          — abstract protocol (kept for type-checking)
    JLinkDllError     — protocol-level error
    RTTCommand, TIF   — enum-like constants
    EmuConnectInfo    — ctypes Structure for JLINK_EMU_GetList
"""

from .constants import RTTCommand, TIF
from .dll import JLinkDll, JLinkDllError
from .fake import FakeJLinkDll
from .session import JLinkSession
from .fake_session import FakeJLinkSession
from .structs import EmuConnectInfo

__all__ = [
    "JLinkDll",
    "JLinkDllError",
    "FakeJLinkDll",
    "JLinkSession",
    "FakeJLinkSession",
    "RTTCommand",
    "TIF",
    "EmuConnectInfo",
]