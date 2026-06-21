"""Abstract JLinkDll interface.

All Real/Fake implementations must satisfy this protocol. Methods document
the SEGGER semantics; RealJLinkDll is a thin ctypes wrapper, FakeJLinkDll
simulates the same behaviour in pure Python.
"""

from __future__ import annotations

from typing import Protocol

from .constants import RTTCommand, TIF
from .structs import EmuConnectInfo


class JLinkDllError(RuntimeError):
    """Raised on any DLL-level error (open failure, connect failure, etc.)."""


class JLinkDll(Protocol):
    """Subset of the SEGGER J-Link SDK needed for jrtt v1.

    Every method here maps 1:1 to a JLINK_* export in JLink_x64.dll.
    """

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """JLINK_Open(). Returns None on success.

        Real impl: returns const char*; NULL = ok, otherwise error string.
        Fake impl: sets internal state.
        """

    def close(self) -> None:
        """JLINK_Close(). Idempotent."""

    def connect(self) -> None:
        """JLINK_Connect(). Raises JLinkDllError on failure (return < 0)."""

    def is_connected(self) -> bool:
        """JLINK_IsConnected(). Return value >= 1 means connected."""

    def get_dll_version(self) -> int:
        """JLINK_GetDLLVersion(). E.g. 0x80000 = 'V8.00' format."""

    def get_sn(self) -> int:
        """JLINK_GetSN(). Serial number of selected probe; < 0 on error."""

    # -- probe selection ---------------------------------------------------

    def emu_get_list(self, host: int) -> list[EmuConnectInfo]:
        """JLINK_EMU_GetList(HostIFs, NULL, 0) -> count, then JLINK_EMU_GetList(buf, count).

        Two-call pattern: first with NULL buffer to get required count, then
        with a buffer of that size to get the actual data.
        """

    def emu_select_by_usb_sn(self, serial: int) -> None:
        """JLINK_EMU_SelectByUSBSN(). Raises if not found."""

    # -- target interface / speed -----------------------------------------

    def tif_select(self, interface: TIF) -> None:
        """JLINK_TIF_Select(Interface). Raises if value != 0."""

    def set_speed(self, khz: int) -> None:
        """JLINK_SetSpeed(). 0 = adaptive; else fixed in kHz."""

    # -- RTT ----------------------------------------------------------------
    # NOTE: SEGGER's high-level RTT API is JLINK_RTTERMINAL_*. It manages
    # RTT control-block discovery internally — application does not provide
    # a buffer address. This is documented in SEGGER SDK as the "RTT Terminal"
    # interface and is what JLinkRTTViewer.exe uses under the hood.

    def rtt_control(self, cmd: RTTCommand, p: int = 0) -> int:
        """JLINK_RTTERMINAL_Control(Cmd, p). Returns 0 on success."""

    def rtt_read(self, buf_index: int, size: int) -> bytes:
        """JLINK_RTTERMINAL_Read(BufferIndex, sBuffer, BufferSize).

        Returns up to ``size`` bytes from the up-buffer. Empty bytes on no
        data. The full buffer is allocated by us and filled by the DLL.
        """

    def rtt_write(self, buf_index: int, data: bytes) -> int:
        """JLINK_RTTERMINAL_Write(BufferIndex, sBuffer, BufferSize).

        Returns number of bytes actually written (< size means buffer full).
        """