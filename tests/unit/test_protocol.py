"""Tests for the NDJSON wire protocol codec."""

from __future__ import annotations

import pytest

from jrtt.protocol import (
    Req,
    Res,
    Evt,
    FrameTooLarge,
    FrameParser,
    ProtocolError,
    encode_frame,
    decode_frame,
    iter_frames,
)


# ---- Req ----


def test_req_roundtrip() -> None:
    r = Req(id="abc-123", op="tail", args={"channel": 0})
    wire = encode_frame(r)
    decoded = decode_frame(wire)
    assert decoded == r


def test_req_no_args() -> None:
    r = Req(id="x", op="ping")
    decoded = decode_frame(encode_frame(r))
    assert decoded.args == {}


def test_req_must_be_one_line() -> None:
    r = Req(id="x", op="tail")
    wire = encode_frame(r)
    # encode_frame returns a wire-format line ending in exactly one \n.
    # Inside that line, no other \n (no embedded raw newlines — those would
    # be JSON-escaped as \n).
    assert wire.count(b"\n") == 1
    assert wire.endswith(b"\n")


# ---- Res ----


def test_res_ok() -> None:
    r = Res(id="abc", ok=True, data={"roundtrip_ms": 2})
    decoded = decode_frame(encode_frame(r))
    assert decoded.ok is True
    assert decoded.data == {"roundtrip_ms": 2}


def test_res_err() -> None:
    r = Res(id="abc", ok=False, code="E_NO_JLINK", msg="probe missing")
    decoded = decode_frame(encode_frame(r))
    assert decoded.ok is False
    assert decoded.code == "E_NO_JLINK"
    assert decoded.msg == "probe missing"


# ---- Evt ----


def test_evt_roundtrip() -> None:
    e = Evt(id="abc", name="rtt.line", data={"ts": 1.0, "channel": 0, "data": "hi\n"})
    decoded = decode_frame(encode_frame(e))
    assert decoded.name == "rtt.line"
    assert decoded.data["channel"] == 0


# ---- wire format ----


def test_frame_is_single_line_json_with_trailing_newline() -> None:
    r = Req(id="x", op="ping")
    wire = encode_frame(r)
    assert wire.endswith(b"\n")
    assert wire.count(b"\n") == 1


def test_frame_uses_unicode_escape_for_control_chars() -> None:
    """Binary data in `data` field must NOT break NDJSON parsing.

    The wire format encodes the whole frame as JSON. Bytes inside strings
    get escaped; raw newlines inside string values would otherwise corrupt
    the frame boundary.
    """
    e = Evt(id="x", name="rtt.line", data={"data": "line1\nline2\n"})
    wire = encode_frame(e)
    # The wire line itself must be exactly ONE line
    assert wire.count(b"\n") == 1
    # But the JSON body has escaped newlines for the embedded string
    assert wire.count(b"\\n") >= 2


def test_decode_rejects_oversized_frame() -> None:
    """Frames above the limit must raise FrameTooLarge."""
    big = b'{"v":1,"t":"req","id":"x","op":"x","args":{"a":"' + b"A" * 70000 + b'"}}\n'
    with pytest.raises(FrameTooLarge):
        decode_frame(big)


def test_decode_rejects_malformed_json() -> None:
    with pytest.raises(Exception):  # JSONDecodeError subclass
        decode_frame(b"not-json\n")


def test_decode_rejects_missing_required_fields() -> None:
    with pytest.raises(Exception):
        decode_frame(b'{"v":1,"t":"req","id":"x"}\n')  # no op


def test_decode_rejects_unknown_type() -> None:
    with pytest.raises(Exception):
        decode_frame(b'{"v":1,"t":"banana","id":"x","op":"x"}\n')


def test_decode_rejects_wrong_version() -> None:
    with pytest.raises(Exception):
        decode_frame(b'{"v":99,"t":"req","id":"x","op":"x"}\n')


# ---- FrameParser: streaming across reads ----


def test_parser_extracts_complete_lines() -> None:
    p = FrameParser()
    buf = (
        b'{"v":1,"t":"req","id":"a","op":"x"}\n'
        b'{"v":1,"t":"req","id":"b","op":"x"}\n'
        b'{"v":1,"t":"req","id":"c","op":"x"}'  # trailing partial, no \n
    )
    frames = p.feed(buf)
    assert len(frames) == 2
    assert frames[0].id == "a"
    assert frames[1].id == "b"


def test_parser_handles_concatenated_input() -> None:
    """A frame split across two feeds must reassemble."""
    p = FrameParser()
    buf1 = b'{"v":1,"t":"req","id":"a","op":"x"}\n{"v":1,"t":"req","id":"b"'
    buf2 = b',"op":"x"}\n'
    out = p.feed(buf1) + p.feed(buf2)
    assert len(out) == 2
    assert [f.id for f in out] == ["a", "b"]


def test_parser_flush_raises_on_partial() -> None:
    p = FrameParser()
    p.feed(b'{"v":1,"t":"req","id":"a"')  # no newline
    with pytest.raises(ProtocolError):
        p.flush()


def test_parser_flush_ok_when_empty() -> None:
    p = FrameParser()
    p.feed(b'{"v":1,"t":"req","id":"a","op":"x"}\n')
    assert p.flush() == []