"""ctypes Structure definitions matching SEGGER headers.

JLINKARM_EMU_CONNECT_INFO is the struct passed to JLINK_EMU_GetList.
Field layout verified against pylink JLinkConnectInfo (square/pylink @ a2172aadc).
"""

import ctypes


class EmuConnectInfo(ctypes.Structure):
    """Connection info for one J-Link probe.

    Layout must match SEGGER's JLINKARM_EMU_CONNECT_INFO exactly; ctypes
    copies raw bytes, so any size / offset mismatch silently corrupts data.
    """

    _fields_ = [
        ("SerialNumber", ctypes.c_uint32),
        ("Connection", ctypes.c_ubyte),
        ("USBAddr", ctypes.c_uint32),
        ("aIPAddr", ctypes.c_uint8 * 16),
        ("Time", ctypes.c_int),
        ("Time_us", ctypes.c_uint64),
        ("HWVersion", ctypes.c_uint32),
        ("abMACAddr", ctypes.c_uint8 * 6),
        ("acProduct", ctypes.c_char * 32),
        ("acNickname", ctypes.c_char * 32),
        ("acFWString", ctypes.c_char * 112),
        ("IsDHCPAssignedIP", ctypes.c_char),
        ("IsDHCPAssignedIPIsValid", ctypes.c_char),
        ("NumIPConnections", ctypes.c_char),
        ("NumIPConnectionsIsValid", ctypes.c_char),
        ("aPadding", ctypes.c_uint8 * 34),
    ]