"""Client for the FreeLab listener that runs inside FontLab.

The listener writes its port and a per-session token to a state file; every
request carries that token. One request per TCP connection, newline-delimited
JSON in both directions.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

PROTOCOL = 1
DEFAULT_TIMEOUT_S = 10.0
_ids = itertools.count(1)


class ListenerError(RuntimeError):
    """The listener could not be reached or refused the request."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def state_dir() -> Path:
    override = os.environ.get("FREELAB_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "state")
    return Path(base) / "FreeLab"


def state_path() -> Path:
    return state_dir() / "listener.json"


START_HINT = (
    "Start it in FontLab: Scripts > FreeLab > Start Listener, or open "
    "freelab_listener.py in the Scripting panel and run it."
)


def read_state() -> dict[str, Any]:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ListenerError("not_running", "The FreeLab listener is not running (no state file). " + START_HINT)
    except (OSError, ValueError) as exc:
        raise ListenerError("bad_state", f"The listener state file could not be read: {exc}")
    if not isinstance(data, dict):
        raise ListenerError("bad_state", "The listener state file is not a JSON object.")
    if data.get("protocol") != PROTOCOL:
        raise ListenerError(
            "bad_protocol",
            f"The listener speaks protocol {data.get('protocol')!r}; this server speaks {PROTOCOL}. Update both from the same FreeLab checkout.",
        )
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536 or not isinstance(data.get("token"), str):
        raise ListenerError("bad_state", "The listener state file has no valid port or token.")
    return data


def call(op: str, args: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    """Send one request and return its result, or raise ListenerError.

    The request carries a deadline: if FontLab only gets to it after the client
    has given up, the listener refuses it instead of running it unseen.
    """
    state = read_state()
    request = {
        "protocol": PROTOCOL,
        "id": next(_ids),
        "token": state["token"],
        "op": op,
        "args": args or {},
        "deadline": time.time() + timeout,
    }
    payload = (json.dumps(request) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", state["port"]), timeout=timeout) as sock:
            sock.sendall(payload)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
    except ConnectionRefusedError:
        raise ListenerError(
            "not_running",
            "The FreeLab listener refused the connection: FontLab was closed, or it is too busy to accept "
            "connections. " + START_HINT,
        )
    except socket.timeout:
        raise ListenerError(
            "timeout",
            f"FontLab did not answer within {timeout:g} s; it may be busy with a long operation. A request "
            "FontLab had already started may still finish, so check the font's state before retrying a change.",
        )
    except OSError as exc:
        raise ListenerError("connection", f"Could not talk to the FreeLab listener: {exc}")
    raw = b"".join(chunks)
    if not raw:
        raise ListenerError("empty_reply", "The listener closed the connection without answering.")
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ListenerError("bad_reply", "The listener's reply is not valid JSON.")
    if not isinstance(reply, dict):
        raise ListenerError("bad_reply", "The listener's reply is not a JSON object.")
    if not reply.get("ok") and reply.get("id") is None and isinstance(reply.get("error"), dict):
        error = reply["error"]  # refused before authentication: no id is echoed
        raise ListenerError(error.get("code", "error"), error.get("message", "The listener refused the request."))
    if reply.get("id") != request["id"]:
        raise ListenerError("bad_reply", "The listener answered a different request.")
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise ListenerError(error.get("code", "error"), error.get("message", "The listener reported an error."))
    return reply.get("result")
