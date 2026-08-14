"""Unix-socket JSON protocol between the CLI client and the playback daemon.

One connection per request: the client connects, sends one JSON object
terminated by a newline, and reads back one newline-terminated JSON object.

Request:  ``{"cmd": "<name>", ...args}``
Response: ``{"ok": true, "data": ...}`` or ``{"ok": false, "error": "..."}``
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .core.errors import PlayerError
from .core.paths import config_dir

COMMANDS = (
    "play",
    "pause",
    "resume",
    "toggle",
    "next",
    "seek",
    "volume",
    "mute",
    "loop",
    "auto_next",
    "status",
    "queue",
    "stop",
)


def socket_path() -> Path:
    """The daemon's control socket (``~/.config/music-cli/control.sock``)."""
    return config_dir() / "control.sock"


def send_request(request: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """Send one request to the daemon and return its response dict.

    Raises ``PlayerError`` when the daemon is not running or the response
    is malformed; the response's own ``ok`` flag reports command failure.
    """
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(socket_path()))
    except OSError as error:
        conn.close()
        raise PlayerError(
            "the daemon is not running — start it with 'music-cli play ...'"
        ) from error
    with conn:
        send_message(conn, request)
        return recv_message(conn)


def send_message(conn: socket.socket, message: dict[str, Any]) -> None:
    conn.sendall(json.dumps(message).encode() + b"\n")


def recv_message(conn: socket.socket) -> dict[str, Any]:
    """Read one newline-terminated JSON object from ``conn``."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    if not data:
        raise PlayerError("the daemon closed the connection without a response")
    try:
        message = json.loads(data)
    except ValueError as error:
        raise PlayerError(f"malformed response from the daemon: {data!r}") from error
    if not isinstance(message, dict):
        raise PlayerError(f"malformed response from the daemon: {data!r}")
    return message
