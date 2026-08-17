"""Unix-socket JSON protocol between the CLI/TUI and the playback daemon.

Two sockets, both in the config dir:

Control socket. One connection per request: the client connects, sends one
JSON object terminated by a newline, and reads back one newline-terminated
JSON object.
  Request:  ``{"cmd": "<name>", ...args}``
  Response: ``{"ok": true, "data": ...}`` or ``{"ok": false, "error": "..."}``

Events socket. A client connects, sends ``{"cmd": "subscribe"}\n`` once, and
the connection stays open; the daemon writes newline-terminated JSON events
to it. Event: ``{"event": "state", "status": <full status dict>}``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core.errors import PlayerError
from .core.paths import config_dir

# The daemon's control socket; "play", "pause", ... are mutually-exclusive
# targets for the "play" command (query | video_id | playlist_id | queue_index).
COMMANDS = (
    "play",
    "download",
    "remove_download",
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
    "quit",
)


def socket_path() -> Path:
    """The daemon's control socket (``~/.config/music-cli/control.sock``)."""
    return config_dir() / "control.sock"


def events_socket_path() -> Path:
    """The daemon's events socket, where subscribers receive pushed state."""
    return config_dir() / "events.sock"


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


def send_play_request(
    request: dict[str, Any],
    *,
    timeout: float = 1200.0,
    on_progress: Callable[[float | None], None] | None = None,
) -> dict[str, Any]:
    """Send a ``play`` request, streaming download progress back to the caller.

    The daemon runs the (slow) download asynchronously and writes one
    newline-terminated ``{"type": "progress", ...}`` line per progress tick
    on the *same* connection, ending with the real ``ok``/``error`` response.
    Each progress line keeps the socket alive, so a legitimately slow download
    never trips a premature client timeout; ``on_progress(percent)`` is called
    per tick (``None`` when the total size is unknown). Raises ``PlayerError``
    only on a genuine protocol failure or a dead connection.
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
        return _read_play_lines(conn, on_progress)


def _read_play_lines(conn: socket.socket, on_progress) -> dict[str, Any]:
    """Read streamed lines off ``conn`` until the final response.

    The daemon writes one newline-terminated JSON ``{"type": "progress"...}``
    line per progress tick, ending with the real ``ok``/``error`` response. Lines
    may arrive batched inside a single ``recv`` chunk, so the buffer is split on
    ``\n`` and one line is decoded at a time. Returns the final response dict.
    """
    buffer = b""
    while True:
        if b"\n" in buffer:
            raw, _, buffer = buffer.partition(b"\n")
            message = json.loads(raw)
            if message.get("type") == "progress":
                percent = message.get("percent")
                if on_progress is not None and isinstance(percent, (int, float)):
                    on_progress(percent)
                continue
            return message
        try:
            chunk = conn.recv(65536)
        except OSError as error:
            conn.close()
            raise PlayerError("timed out waiting for the daemon") from error
        if not chunk:
            conn.close()
            raise PlayerError("the daemon closed the connection without a response")
        buffer += chunk


def send_message(conn: socket.socket, message: dict[str, Any]) -> None:
    conn.sendall(json.dumps(message).encode() + b"\n")


def recv_message(conn: socket.socket) -> dict[str, Any]:
    """Read one newline-terminated JSON object from ``conn``."""
    data = b""
    while not data.endswith(b"\n"):
        try:
            chunk = conn.recv(65536)
        except TimeoutError as error:
            raise PlayerError("timed out waiting for the daemon") from error
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


def ensure_daemon(cookies: str | None = None, volume: int | None = None) -> None:
    """Start the playback daemon in the background unless it already answers.

    Probes the control socket with a short ``status``; when nothing answers a
    detached daemon is spawned and awaited (up to ~10s). Raises
    ``PlayerError`` if the daemon never comes up.
    """
    try:
        send_request({"cmd": "status"}, timeout=1.0)
        return
    except PlayerError:
        pass
    command = [sys.executable, "-m", "music_cli.daemon"]
    if cookies:
        command += ["--cookies", cookies]
    if volume is not None:
        command += ["--volume", str(volume)]
    subprocess.Popen(  # noqa: S603 — argv is fixed, no user input
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            send_request({"cmd": "status"}, timeout=1.0)
            return
        except PlayerError:
            time.sleep(0.1)
    raise PlayerError("the daemon did not start")
