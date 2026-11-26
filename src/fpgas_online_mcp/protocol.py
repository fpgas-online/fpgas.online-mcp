"""Newline-delimited JSON protocol between the broker and its clients.

Request:  {"id": int, "op": str, "args": dict, "version": str, "holder": str}
Response: {"id": int, "ok": true, "result": ...}
          {"id": int|null, "ok": false, "error": {"code": str, "message": str}}
`ping` answers {"version": str, "protocol": int, "pid": int, "started": float}; a broker without
"protocol" speaks protocol 1.
"""

import json

MAX_LINE = 1024 * 1024
# Bumped only when the wire format or an op's meaning changes incompatibly: clients of any version
# use a broker of the same protocol (a client newer than the broker still asks it to restart when idle).
PROTOCOL = 1


class BrokerError(Exception):
    """An error with a machine-readable code, carried over the wire as ok:false."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def encode(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict:
    try:
        obj = json.loads(line)
    except ValueError as e:
        raise BrokerError("bad_request", f"invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise BrokerError("bad_request", "message must be a JSON object")
    return obj


def error_response(req_id: int | None, code: str, message: str) -> dict:
    return {"id": req_id, "ok": False, "error": {"code": code, "message": message}}
