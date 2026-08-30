"""The wire contract.

The point of these is that the two structural guarantees cannot be worked around: a
response cannot claim success while carrying a failure, and nothing unrecognised gets
through.
"""

from __future__ import annotations

import json

import pytest

from localplane.protocol.wire import (
    MAX_MESSAGE_BYTES,
    METHODS,
    PROTOCOL_VERSION,
    ErrorCode,
    Method,
    ProtocolError,
    decode_request,
    decode_response,
    encode_error_response,
    encode_ok_response,
    encode_request,
    read_message,
)


def test_request_round_trip():
    frame = encode_request("r1", Method.NETWORK_OBSERVE_INTERFACES, {"names": ["eth0"]})
    request_id, method, params = decode_request(frame.rstrip(b"\n"))
    assert (request_id, method, params) == ("r1", "network.observe_interfaces", {"names": ["eth0"]})


def test_ok_response_round_trip():
    _, ok, result, error = decode_response(encode_ok_response("r1", {"a": 1}).rstrip(b"\n"))
    assert (ok, result, error) == (True, {"a": 1}, None)


def test_error_response_round_trip():
    frame = encode_error_response("r1", ErrorCode.PROVIDER_ERROR, "boom", {"x": 1})
    _, ok, result, error = decode_response(frame.rstrip(b"\n"))
    assert ok is False and result is None
    assert error == {"code": "provider_error", "message": "boom", "detail": {"x": 1}}


def test_every_method_has_a_name_in_the_closed_set():
    assert METHODS == {m.value for m in Method}


@pytest.mark.parametrize("method", ["shell.exec", "run", "network.apply", ""])
def test_unsupported_methods_are_refused(method):
    payload = {
        "localplane_protocol": PROTOCOL_VERSION,
        "request_id": "r1",
        "method": method,
        "params": {},
    }
    with pytest.raises(ProtocolError) as caught:
        decode_request(json.dumps(payload).encode())
    assert caught.value.code is ErrorCode.UNSUPPORTED_METHOD


def test_encoding_an_unsupported_method_is_refused_at_the_source():
    with pytest.raises(ProtocolError) as caught:
        encode_request("r1", "exec.command", {"argv": ["rm", "-rf", "/"]})
    assert caught.value.code is ErrorCode.UNSUPPORTED_METHOD


def test_unknown_envelope_fields_are_refused():
    payload = {
        "localplane_protocol": PROTOCOL_VERSION,
        "request_id": "r1",
        "method": str(Method.AGENT_HELLO),
        "params": {},
        "command": "/bin/sh",
    }
    with pytest.raises(ProtocolError) as caught:
        decode_request(json.dumps(payload).encode())
    assert caught.value.code is ErrorCode.UNKNOWN_FIELD
    assert caught.value.detail["unknown"] == ["command"]


def test_protocol_version_mismatch_is_refused():
    payload = {
        "localplane_protocol": "99",
        "request_id": "r1",
        "method": str(Method.AGENT_HELLO),
        "params": {},
    }
    with pytest.raises(ProtocolError) as caught:
        decode_request(json.dumps(payload).encode())
    assert caught.value.code is ErrorCode.PROTOCOL_VERSION_UNSUPPORTED


def test_a_response_claiming_ok_over_an_error_is_refused():
    """The truthfulness guarantee: top-level ok is recomputed, never believed."""
    payload = {
        "localplane_protocol": PROTOCOL_VERSION,
        "request_id": "r1",
        "ok": True,
        "error": {"code": "provider_error", "message": "it failed"},
    }
    with pytest.raises(ProtocolError) as caught:
        decode_response(json.dumps(payload).encode())
    assert caught.value.code is ErrorCode.MALFORMED_MESSAGE


def test_a_response_carrying_both_result_and_error_is_refused():
    payload = {
        "localplane_protocol": PROTOCOL_VERSION,
        "request_id": "r1",
        "ok": True,
        "result": {},
        "error": {"code": "x", "message": "y"},
    }
    with pytest.raises(ProtocolError) as caught:
        decode_response(json.dumps(payload).encode())
    assert "exactly one" in caught.value.message


def test_a_response_carrying_neither_is_refused():
    payload = {"localplane_protocol": PROTOCOL_VERSION, "request_id": "r1", "ok": True}
    with pytest.raises(ProtocolError):
        decode_response(json.dumps(payload).encode())


def test_malformed_json_is_refused():
    with pytest.raises(ProtocolError) as caught:
        decode_request(b"{not json")
    assert caught.value.code is ErrorCode.MALFORMED_MESSAGE


def test_oversized_frames_are_refused_without_buffering():
    import io

    stream = io.BytesIO(b"x" * 128)
    with pytest.raises(ProtocolError) as caught:
        read_message(stream, limit=32)
    assert caught.value.code is ErrorCode.MESSAGE_TOO_LARGE


def test_unterminated_frame_is_refused():
    import io

    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(b'{"a":1}'))


def test_clean_close_reads_as_none():
    import io

    assert read_message(io.BytesIO(b"")) is None


def test_message_ceiling_is_bounded():
    assert 0 < MAX_MESSAGE_BYTES <= 64 * 1024 * 1024
