"""Constants from JLinkARM_Const.h / pylink enums.py.

Verified against:
  - pylink enums.py (square/pylink @ a2172aadc)
  - LX050724/JLinkDll JLink_RTT.c
  - MCUSec/IPEA JLinkARMDLL.h

Sources are independent, values match.
"""

from enum import IntEnum


class RTTCommand(IntEnum):
    """Cmd values for JLINK_RTTERMINAL_Control."""

    START = 0
    STOP = 1
    GETDESC = 2
    GETNUMBUF = 3
    GETSTAT = 4


class TIF(IntEnum):
    """Interface values for JLINK_TIF_Select."""

    JTAG = 0
    SWD = 1
    FINE = 3
    ICSP = 4
    SPI = 5
    C2 = 6


class HostIF(IntEnum):
    """Host interface for JLINK_EMU_GetList (HostIFs parameter)."""

    USB = 0
    IP = 1


# Common RTT buffer count we will request via GETNUMBUF. SEGGER typically
# supports up to 3 up-buffers + 3 down-buffers. We read only up-buffers for v1.