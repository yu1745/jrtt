"""NDJSON wire protocol for jrtt IPC.

Wire format (one frame per line, UTF-8, ≤64 KB):

  Request  (CLI → daemon):  {"v":1, "t":"req", "id":"<uuid>", "op":"<name>", "args":{...}}
  Response (daemon → CLI):  {"v":1, "t":"res", "id":"<uuid>", "ok":true,  "data":{...}}
                             {"v":1, "t":"res", "id":"<uuid>", "ok":false, "code":"...", "msg":"..."}
  Event    (daemon → CLI):  {"v":1, "t":"evt", "id":"<uuid>", "name":"<event>", "data":{...}}

Three rules:
  * Every frame is exactly one line ending in \\n.
  * Frame size cap: 64 KB. Larger → FrameTooLarge (callers should chunk).
  * Version mismatch → ProtocolError.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Iterator, Union


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024


# ---- exceptions ----


class ProtocolError(Exception):
    """Raised on any protocol-level violation (malformed, bad version, etc.)."""


class FrameTooLarge(ProtocolError):
    """A frame exceeded MAX_FRAME_BYTES."""


# ---- frame types ----


@dataclass
class Req:
    id: str
    op: str
    args: dict = field(default_factory=dict)
    t: str = field(default="req", init=False)
    v: int = field(default=PROTOCOL_VERSION, init=False)


@dataclass
class Res:
    id: str
    ok: bool
    data: dict | None = None
    code: str | None = None
    msg: str | None = None
    t: str = field(default="res", init=False)
    v: int = field(default=PROTOCOL_VERSION, init=False)


@dataclass
class Evt:
    id: str
    name: str
    data: dict
    t: str = field(default="evt", init=False)
    v: int = field(default=PROTOCOL_VERSION, init=False)


Frame = Union[Req, Res, Evt]


# ---- codec ----


_TYPE_TO_CLS: dict[str, type] = {"req": Req, "res": Res, "evt": Evt}
_REQUIRED_FIELDS: dict[type, set[str]] = {
    Req: {"id", "op"},
    Res: {"id", "ok"},
    Evt: {"id", "name", "data"},
}


def encode_frame(frame: Frame) -> bytes:
    """Serialise a frame to bytes ending in \\n.

    Embedded newlines inside string fields get JSON-escaped (\\n), so the
    output is guaranteed to be exactly one line.
    """
    payload = asdict(frame)
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _strip_init_false(payload: dict) -> dict:
    """asdict() includes our default-marker fields; remove those that shouldn't be re-emitted."""
    # They have the right values from defaults; we keep them but ensure types match.
    return payload


def decode_frame(data: bytes) -> Frame:
    """Parse one frame. Must contain exactly one trailing \\n.

    Raises ProtocolError / FrameTooLarge / json.JSONDecodeError.
    """
    if len(data) > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"Frame {len(data)} > {MAX_FRAME_BYTES}")
    if not data.endswith(b"\n"):
        raise ProtocolError("Frame missing trailing newline")
    try:
        obj = json.loads(data[:-1])
    except json.JSONDecodeError as e:
        raise ProtocolError(f"Malformed JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError("Frame must be a JSON object")

    v = obj.get("v")
    if v != PROTOCOL_VERSION:
        raise ProtocolError(f"Unsupported protocol version: {v}")

    t = obj.get("t")
    cls = _TYPE_TO_CLS.get(t or "")
    if cls is None:
        raise ProtocolError(f"Unknown frame type: {t!r}")

    required = _REQUIRED_FIELDS[cls]
    missing = required - set(obj.keys())
    if missing:
        raise ProtocolError(f"Frame missing required fields: {missing}")

    # Strip our internal sentinel fields (v, t) before passing to constructor
    kwargs = {k: v for k, v in obj.items() if k not in {"v", "t"}}
    return cls(**kwargs)


# ---- streaming ----


class FrameParser:
    """Stateful NDJSON frame parser. Accumulate bytes across reads.

    Usage:
        p = FrameParser()
        for frame in p.feed(chunk):
            handle(frame)
        # later:
        for frame in p.feed(more_chunk):
            handle(frame)
        # at EOF:
        for frame in p.flush():
            handle(frame)  # raises if a partial frame remains
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf.extend(chunk)
        out: list[Frame] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl == -1:
                break
            line = bytes(self._buf[: nl + 1])
            del self._buf[: nl + 1]
            if line.strip() == b"":
                continue
            out.append(decode_frame(line))
        return out

    def flush(self) -> list[Frame]:
        if self._buf:
            # leftover bytes — must be malformed input at EOF
            raise ProtocolError(f"Trailing partial frame at EOF: {bytes(self._buf)!r}")
        return []


def iter_frames(buf: bytes) -> Iterator[Frame]:
    """Convenience: one-shot parse of a complete buffer (no partials).

    For streaming use FrameParser instead.
    """
    parser = FrameParser()
    frames = parser.feed(buf)
    parser.flush()
    return iter(frames)