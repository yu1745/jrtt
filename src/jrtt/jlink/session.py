"""High-level JLinkSession wrapping pylink-square.

Verified flow against N32G430C8 + J-Link probe (real hardware, June 2026):
    Library(dllpath=...)        # explicit; auto-detect fails on this machine
    JLink(lib=lib).open()       # calls JLINKARM_SelectUSB + JLINKARM_OpenEx + JLock
    .connected_emulators()      # find probe
    .connect(chip_name=...)     # MANDATORY; pylink 2.x requires device name
    .set_tif(1)                 # 1 = SWD
    .set_speed(khz)             # fixed or auto
    .rtt_start()                # no params needed
    .rtt_read(0, 4096)          # poll loop
    .rtt_stop() / .close()

We expose a friendlier jrtt-native API on top of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .constants import TIF


@dataclass
class JLinkSession:
    """Real J-Link session backed by pylink.

    Lifecycle:
        sess = JLinkSession()
        sess.open(sn=..., chip="N32G430C8", tif=TIF.SWD, speed_khz=4000)
        sess.start_rtt()
        for line in sess.read_lines():
            ...
        sess.stop()
        sess.close()
    """

    _lib = None  # pylink.library.Library
    _jlink = None  # pylink.JLink

    def __init__(self, dll_path: Optional[str] = None) -> None:
        # Imported lazily so unit tests don't need pylink installed
        from pylink import JLink, Library  # type: ignore
        self._pylink_JLink = JLink
        self._pylink_Library = Library
        # Default to the well-known SEGGER install paths; auto-detect does
        # NOT work with pylink 2.x on Windows (Library() with no path raises
        # "Expected to be given a valid DLL" — see verified-facts in spec §0).
        if dll_path is None:
            from pathlib import Path
            for p in [
                Path.home() / ".eide" / "tools" / "jlink" / "JLink_x64.dll",
                Path("C:/Program Files/SEGGER/JLink/JLink_x64.dll"),
                Path("C:/Program Files (x86)/SEGGER/JLink/JLink_x64.dll"),
            ]:
                if p.is_file():
                    dll_path = str(p)
                    break
        self._dll_path = dll_path

    def open(
        self,
        *,
        sn: Optional[int] = None,
        chip: str,
        tif: TIF = TIF.SWD,
        speed_khz: int = 4000,
    ) -> None:
        """Acquire the probe + connect to target + start RTT plumbing.

        chip:    target device name as known to SEGGER (e.g. "N32G430C8",
                 "STM32F407VG"). Mandatory.
        tif:     SWD or JTAG. SWD is the typical modern default.
        speed_khz: JTAG/SWD clock in kHz. 0 = adaptive (slower but robust).
        """
        self._lib = self._pylink_Library(dllpath=self._dll_path) if self._dll_path else self._pylink_Library()
        self._jlink = self._pylink_JLink(lib=self._lib)
        # pylink.open() handles SelectUSB + OpenEx + JLock internally
        if sn is not None:
            self._jlink.open(serial_no=sn)
        else:
            self._jlink.open()
        # Target connect — pylink 2.x requires chip_name
        self._jlink.connect(chip_name=chip, speed="auto" if speed_khz == 0 else speed_khz)
        # TIF must be set BEFORE speed for SWD-only probes
        self._jlink.set_tif(int(tif))
        if speed_khz > 0:
            self._jlink.set_speed(speed_khz)

    def start_rtt(self) -> None:
        """Begin RTT transfer. SEGGER's high-level API locates the RTT
        control block on the target automatically — we don't pass an address.
        """
        self._jlink.rtt_start()

    def stop_rtt(self) -> None:
        self._jlink.rtt_stop()

    def read_bytes(self, buf_index: int = 0, max_bytes: int = 4096) -> bytes:
        """Pull whatever bytes are available from the RTT up-buffer.

        pylink returns a list[int] (annoying). We convert to bytes.
        Empty list → empty bytes (no data right now; caller should poll).
        """
        data = self._jlink.rtt_read(buf_index, max_bytes)
        if not data:
            return b""
        return bytes(data)

    def num_up_buffers(self) -> int:
        return self._jlink.rtt_get_num_up_buffers()

    def close(self) -> None:
        try:
            self._jlink.close()
        except Exception:
            pass
        self._jlink = None
        self._lib = None

    # ---- context manager sugar ----

    def __enter__(self) -> "JLinkSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()