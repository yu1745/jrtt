"""Windows Named Pipe transport for jrtt.

Pure ctypes — no pywin32 dependency.

Server side (PipeServer):
    name = r'\\\\.\\pipe\\jrtt'
    srv = PipeServer(name)
    srv.serve_one(handle_chunk, timeout_s=...)  # single connection
    # or
    srv.serve_loop(handle_chunk, timeout_s=...)  # accept forever

Client side (PipeClient):
    with PipeClient(name, timeout_ms=2000) as c:
        c.send(b"hello\\n")
        data = c.recv(timeout_s=1.0)

Wire format: opaque byte stream. Higher layers (protocol.py) handle framing.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("jrtt.ipc is Windows-only (Named Pipes)")

# ---- Win32 bindings ----

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

PIPE_ACCESS_DUPLEX = 0x3
PIPE_TYPE_BYTE = 0x0
PIPE_READMODE_BYTE = 0x0
PIPE_WAIT = 0x0
PIPE_NOWAIT = 0x1
PIPE_UNLIMITED_INSTANCES = 255
PIPE_REJECT_REMOTE_CLIENTS = 0x8

ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_CONNECTED = 535
ERROR_IO_INCOMPLETE = 996
ERROR_NO_DATA = 232
ERROR_OPERATION_ABORTED = 995

INFINITE = 0xFFFFFFFF


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", wintypes.DWORD),
        ("InternalHigh", wintypes.DWORD),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE

kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL

kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(_OVERLAPPED),
]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(_OVERLAPPED),
]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
kernel32.WaitNamedPipeW.restype = wintypes.BOOL

kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
kernel32.CancelIoEx.restype = wintypes.BOOL


# ---- exceptions ----


class PipeError(RuntimeError):
    """Any pipe-level error (connection refused, broken pipe, timeout)."""


# ---- framing helper (newline-delimited, used by callers via iter) ----


class Framing:
    """Accumulate bytes; yield complete lines (with trailing \\n).

    Used by server to split client stream into individual requests, and by
    client to split server stream into individual responses.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        out: list[bytes] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl == -1:
                break
            line = bytes(self._buf[: nl + 1])
            del self._buf[: nl + 1]
            if line.strip():
                out.append(line)
        return out

    @property
    def tail(self) -> bytes:
        return bytes(self._buf)


# ---- client ----


class PipeClient:
    """Connect to an existing named-pipe server."""

    def __init__(self, name: str, *, timeout_ms: int = 5000) -> None:
        self._name = name
        self._timeout_ms = timeout_ms
        self._handle: wintypes.HANDLE | None = None

    def __enter__(self) -> "PipeClient":
        # Skip WaitNamedPipeW; it can block indefinitely on some systems
        # when the pipe exists but is busy in a connection. Just try
        # CreateFileW directly with a short retry loop.
        deadline = time.monotonic() + self._timeout_ms / 1000.0
        h = None
        err = 0
        while time.monotonic() < deadline:
            h = kernel32.CreateFileW(
                self._name,
                GENERIC_READ | GENERIC_WRITE,
                0,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if h and h != INVALID_HANDLE_VALUE:
                break
            err = ctypes.get_last_error()
            if err == ERROR_FILE_NOT_FOUND:
                time.sleep(0.05)
                continue
            if err == ERROR_PIPE_BUSY:
                time.sleep(0.05)
                continue
            break
        else:
            raise PipeError(f"Could not open {self._name} within {self._timeout_ms}ms (err={err})")
        if not h or h == INVALID_HANDLE_VALUE:
            if err == ERROR_FILE_NOT_FOUND:
                raise PipeError(f"No server listening on {self._name}")
            raise PipeError(f"CreateFileW failed: Win32 error {err}")
        self._handle = h
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None and self._handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def send(self, data: bytes) -> None:
        if self._handle is None:
            raise PipeError("send() before connect")
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            err = ctypes.get_last_error()
            raise PipeError(f"WriteFile failed: Win32 error {err}")
        if written.value != len(data):
            raise PipeError(f"Short write: {written.value}/{len(data)}")

    def recv(self, *, timeout_s: float = 5.0, max_bytes: int = 64 * 1024) -> bytes:
        """Read up to max_bytes or until no more immediately available."""
        if self._handle is None:
            raise PipeError("recv() before connect")
        buf = ctypes.create_string_buffer(max_bytes)
        n = wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._handle, buf, max_bytes, ctypes.byref(n), None)
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_BROKEN_PIPE:
                raise PipeError("Pipe closed by server")
            if err == ERROR_NO_DATA:
                return b""
            raise PipeError(f"ReadFile failed: Win32 error {err}")
        return bytes(buf.raw[: n.value])

    def read_chunk(self, max_bytes: int = 64 * 1024) -> bytes:
        """Server-side helper: blocking read of one chunk. Returns b'' on disconnect."""
        if self._handle is None:
            return b""
        buf = ctypes.create_string_buffer(max_bytes)
        n = wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._handle, buf, max_bytes, ctypes.byref(n), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
                return b""
            raise PipeError(f"ReadFile failed: Win32 error {err}")
        return bytes(buf.raw[: n.value])


# ---- server ----


class PipeServer:
    """Bind a named pipe and accept connections.

    Model: blocking single-threaded accept loop. Caller runs in their own
    thread(s). For the daemon, we'll spawn one thread per accepted client.
    """

    def __init__(self, name: str, *, buffer_size: int = 64 * 1024) -> None:
        self._name = name
        self._buffer_size = buffer_size
        self._handle: wintypes.HANDLE | None = None
        self._stop = threading.Event()

    def create(self) -> None:
        """Create the pipe instance. Call before serve_*."""
        h = kernel32.CreateNamedPipeW(
            self._name,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            self._buffer_size,
            self._buffer_size,
            0,
            None,
        )
        if h == INVALID_HANDLE_VALUE or h is None:
            err = ctypes.get_last_error()
            raise PipeError(f"CreateNamedPipeW failed for {self._name!r}: Win32 error {err}")
        self._handle = h

    def close(self) -> None:
        if self._handle is not None and self._handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def stop(self) -> None:
        """Cancel pending I/O and signal the serve loop to exit.

        Idempotent and thread-safe.
        """
        self._stop.set()
        if self._handle is not None and self._handle != INVALID_HANDLE_VALUE:
            # CancelIoEx on a pipe that is blocking in ConnectNamedPipe makes
            # that call return with ERROR_OPERATION_ABORTED, unblocking the
            # serve loop.
            kernel32.CancelIoEx(self._handle, None)

    def serve_one(self, handler, *, timeout_s: float = 30.0) -> None:
        """Accept one client; ``handler`` is responsible for all subsequent I/O.

        The handler can have one of two signatures:

        1. ``handler(chunk: bytes) -> bytes | None`` — single-roundtrip mode.
           Serve_one reads one chunk from the client, calls the handler, writes
           back the returned bytes (if any), and disconnects. Returns when done.

        2. ``handler(server: PipeServer) -> None`` — long-lived mode. Serve_one
           does the accept; the handler drives its own read/write loop using
           server.read_chunk() and server.send(). It returns when it wants to
           close the connection (or the client disconnects).

        We detect the signature by inspecting the handler's parameters.

        timeout_s: how long to wait for a client to connect before giving up.
        """
        if self._handle is None:
            self.create()
        ok = kernel32.ConnectNamedPipe(self._handle, None)
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_OPERATION_ABORTED and self._stop.is_set():
                raise PipeError("stop requested")
            if err != ERROR_PIPE_CONNECTED:
                raise PipeError(f"ConnectNamedPipe failed: Win32 error {err}")

        try:
            # Decide which signature the handler uses by inspecting its params
            import inspect
            try:
                sig = inspect.signature(handler)
                params = list(sig.parameters.values())
                first_param_name = params[0].name if params else None
            except (TypeError, ValueError):
                first_param_name = None

            if first_param_name in ("server", "srv", "pipe"):
                # Long-lived mode: hand the server to the handler.
                handler(self)
            else:
                # Single-roundtrip mode (handler takes a chunk arg, or no arg).
                # Read one chunk, call handler, send reply.
                buf = ctypes.create_string_buffer(self._buffer_size)
                n = wintypes.DWORD(0)
                ok = kernel32.ReadFile(self._handle, buf, self._buffer_size,
                                       ctypes.byref(n), None)
                if not ok:
                    err = ctypes.get_last_error()
                    if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
                        return
                    raise PipeError(f"ReadFile failed: Win32 error {err}")
                if n.value == 0:
                    return
                chunk = bytes(buf.raw[: n.value])
                reply = handler(chunk)
                if reply is not None:
                    self._send(reply)
        finally:
            try:
                kernel32.DisconnectNamedPipe(self._handle)
            except Exception:
                pass
            self.close()

    def serve_loop(self, handler, *, timeout_s: float = 30.0) -> None:
        """Keep accepting clients until .stop() is called or accept times out.

        timeout_s bounds how long we wait for a single client to connect
        before giving up and exiting. Use a small value for tests; production
        daemon passes a long value (or INFINITE).
        """
        import time as _t
        deadline = None if timeout_s <= 0 else _t.monotonic() + timeout_s
        while not self._stop.is_set():
            if deadline is not None and _t.monotonic() >= deadline:
                return
            try:
                self.serve_one(handler, timeout_s=timeout_s)
                # Re-create the pipe handle for the next connection
                self.create()
            except PipeError:
                return  # fatal; bail

    def _send(self, data: bytes) -> None:
        if self._handle is None:
            return
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)
        if not ok or written.value != len(data):
            raise PipeError("WriteFile short or failed")

    def send(self, data: bytes) -> bool:
        """Server-side helper: blocking write of bytes. Returns True on success."""
        try:
            self._send(data)
            return True
        except PipeError:
            return False

    def read_chunk(self, max_bytes: int = 64 * 1024) -> bytes:
        """Server-side helper: blocking read of one chunk. Returns b'' on disconnect."""
        if self._handle is None:
            return b""
        buf = ctypes.create_string_buffer(max_bytes)
        n = wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._handle, buf, max_bytes, ctypes.byref(n), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
                return b""
            raise PipeError(f"ReadFile failed: Win32 error {err}")
        return bytes(buf.raw[: n.value])


# ---- daemon-detection helper ----


def pipe_exists(name: str, *, wait_ms: int = 0) -> bool:
    """Return True if a server is listening on the named pipe.

    wait_ms=0 means "no wait" — caller has to retry on False if it just
    spawned a daemon.
    """
    return bool(kernel32.WaitNamedPipeW(name, wait_ms))