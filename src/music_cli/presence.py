"""Discord Rich Presence over Discord's local IPC socket.

The daemon owns playback for both frontends, so it is also the only process
that talks to Discord. A background thread keeps the socket off the daemon's
~20ms serve loop: ``update`` only stores the latest snapshot, and the worker
sends it when Discord's rate limit allows. If Discord is not running the
presence simply appears once it starts.

Only the small slice of the RPC protocol that Rich Presence needs is
implemented here (handshake + ``SET_ACTIVITY``); there is no dependency on
``pypresence``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import struct
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

#: Discord accepts five activity updates per twenty seconds.
MIN_UPDATE_INTERVAL = 4.0
#: Reconnect backoff while Discord is not running.
RETRY_MIN = 5.0
RETRY_MAX = 30.0
#: Re-send an unchanged activity now and then. Besides refreshing the
#: timestamps, this notices a Discord that was restarted while we idled.
REFRESH_INTERVAL = 60.0
#: A broken Discord must never pin the worker thread for long.
SOCKET_TIMEOUT = 3.0

#: Discord activity type "Listening to ...".
ACTIVITY_TYPE_LISTENING = 2
#: Show the track title (details) in the member list instead of the app name.
STATUS_DISPLAY_DETAILS = 2

_FIELD_LIMIT = 128
_BUTTON_LABEL = "Listen on YouTube Music"
_FALLBACK_STATE = "YouTube Music"


class PresenceError(Exception):
    """The Discord RPC socket failed or refused the activity."""


def socket_paths() -> list[Path]:
    """Every Discord IPC socket worth trying, in the documented order.

    On Linux, Discord usually runs inside Flatpak or Snap, which keeps the
    socket in an app-specific subdirectory of the runtime dir. Trying those
    after the plain paths costs nothing when the official client is running.
    """
    dirs: list[Path] = []
    for variable in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(variable)
        if value:
            path = Path(value)
            if path not in dirs:
                dirs.append(path)
    fallback = Path("/tmp")  # noqa: S108 — Discord's documented fallback
    if fallback not in dirs:
        dirs.append(fallback)

    subs = [Path()]
    if sys.platform.startswith("linux"):
        subs += [Path("snap.discord"), Path("snap.discord-canary")]
        for app in (
            "com.discordapp.Discord",
            "dev.vencord.Vesktop",
            "org.equicord.equibop",
            "io.github.equicord.equibop",
        ):
            subs += [Path("app") / app, Path(".flatpak") / app / "xdg-run"]

    return [
        directory / sub / f"discord-ipc-{index}"
        for directory in dirs
        for sub in subs
        for index in range(10)
    ]


def encode_frame(opcode: int, payload: dict[str, Any]) -> bytes:
    """Encode one Discord IPC frame: opcode + length + JSON, little-endian."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", opcode, len(body)) + body


def read_frame(conn: socket.socket) -> tuple[int, dict[str, Any]]:
    """Read one Discord IPC frame, tolerating short reads."""
    opcode, length = struct.unpack("<II", _recv_exact(conn, 8))
    body = _recv_exact(conn, length)
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise PresenceError("Discord sent malformed JSON") from error
    if not isinstance(payload, dict):
        raise PresenceError("Discord sent a malformed frame")
    return opcode, payload


def _recv_exact(conn: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = conn.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Discord closed the connection")
        data.extend(chunk)
    return bytes(data)


def thumbnail_url(video_id: str) -> str:
    """The auto-generated YouTube thumbnail used as the large image."""
    return f"https://i.ytimg.com/vi/{quote(video_id, safe='')}/mqdefault.jpg"


def watch_url(video_id: str) -> str:
    """The track page behind the presence button."""
    return f"https://music.youtube.com/watch?v={quote(video_id, safe='')}"


def activity_for_status(
    status: dict[str, Any], *, now: float | None = None
) -> dict[str, Any] | None:
    """Build the Discord activity for one session status snapshot.

    Returns ``None`` when nothing is loaded, which clears the presence.
    Timestamps are Unix milliseconds; they are only sent while playing so a
    paused track does not keep counting up.
    """
    track = status.get("track")
    state = status.get("state")
    if not track or state not in ("playing", "paused"):
        return None

    title = _clip(track.get("title") or "Unknown track")
    artists = _artists(track.get("artists"))
    state_text = _clip(artists or _FALLBACK_STATE)
    if state == "paused":
        state_text = _clip(f"Paused — {state_text}")

    activity: dict[str, Any] = {
        "type": ACTIVITY_TYPE_LISTENING,
        "status_display_type": STATUS_DISPLAY_DETAILS,
        "details": title,
        "state": state_text,
    }

    video_id = str(track.get("video_id") or "").strip()
    if video_id:
        activity["assets"] = {
            "large_image": thumbnail_url(video_id),
            "large_text": title,
        }
        activity["buttons"] = [{"label": _BUTTON_LABEL, "url": watch_url(video_id)}]

    if state == "playing":
        position = _number(status.get("position"))
        duration = _number(status.get("duration"))
        if duration > 0:
            position = min(position, duration)
        position = max(position, 0.0)
        started = (time.time() if now is None else now) - position
        activity["timestamps"] = {"start": int(started * 1000)}
        if duration > 0:
            activity["timestamps"]["end"] = int((started + duration) * 1000)

    return activity


def _artists(value: Any) -> str:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = []
    return ", ".join(text for item in items if (text := " ".join(str(item).split())))


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _clip(value: Any, limit: int = _FIELD_LIMIT) -> str:
    """Normalise a presence field to Discord's 2-128 character constraint."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit]
    # Discord rejects fields shorter than two characters.
    return f"{text} " if len(text) < 2 else text


def _status_key(status: dict[str, Any]) -> tuple:
    """The activity fields that change what Discord displays."""
    track = status.get("track") or {}
    artists = track.get("artists") or ()
    return (
        status.get("state"),
        track.get("video_id"),
        track.get("title"),
        tuple(str(artist) for artist in artists),
        status.get("duration"),
    )


class RPCConnection:
    """One handshaken Discord IPC connection."""

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn

    @classmethod
    def connect(
        cls,
        client_id: str,
        *,
        paths: Sequence[Path] | None = None,
        timeout: float = SOCKET_TIMEOUT,
    ) -> RPCConnection:
        """Connect to the first working Discord socket and complete the handshake."""
        candidates = list(paths) if paths is not None else socket_paths()
        last_error: Exception | None = None
        handshake_error: Exception | None = None
        for path in candidates:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(timeout)
            try:
                conn.connect(str(path))
            except OSError as error:
                last_error = error
                conn.close()
                continue
            connection = cls(conn)
            try:
                connection._handshake(client_id)
            except (OSError, PresenceError, ValueError) as error:
                handshake_error = last_error = error
                connection.close()
                continue
            return connection
        if handshake_error is not None:
            # A socket existed and Discord answered, but rejected us (bad
            # application id, for example); "not running" would be a lie.
            raise PresenceError(str(handshake_error)) from handshake_error
        detail = f": {last_error}" if last_error is not None else ""
        raise PresenceError(f"Discord is not running{detail}")

    def set_activity(self, activity: dict[str, Any] | None) -> None:
        """Send ``SET_ACTIVITY`` and wait for Discord's reply."""
        nonce = uuid.uuid4().hex
        self._send(
            OP_FRAME,
            {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": nonce,
            },
        )
        while True:
            opcode, payload = self._recv()
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                raise PresenceError(_close_message(payload))
            if opcode != OP_FRAME or payload.get("nonce") != nonce:
                continue
            if payload.get("evt") == "ERROR":
                raise PresenceError(_error_message(payload))
            return

    def close(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass

    def _handshake(self, client_id: str) -> None:
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": client_id})
        while True:
            opcode, payload = self._recv()
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                raise PresenceError(_close_message(payload))
            if opcode != OP_FRAME:
                continue
            event = payload.get("evt")
            if event == "READY":
                return
            if event == "ERROR":
                raise PresenceError(_error_message(payload))

    def _send(self, opcode: int, payload: dict[str, Any]) -> None:
        try:
            self._conn.sendall(encode_frame(opcode, payload))
        except OSError as error:
            raise PresenceError(f"could not write to Discord: {error}") from error

    def _recv(self) -> tuple[int, dict[str, Any]]:
        try:
            return read_frame(self._conn)
        except OSError as error:
            raise PresenceError(f"could not read from Discord: {error}") from error


def _close_message(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if message:
        return f"Discord closed the connection: {message}"
    return "Discord closed the connection"


def _error_message(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return "Discord rejected the activity"


class PresenceManager:
    """Keep Discord's presence in sync with the daemon's status.

    ``update`` is called from the serve loop and never touches the socket;
    it stores the newest snapshot and wakes the worker. The worker keeps only
    the latest pending activity, connects lazily, reconnects with backoff,
    periodically re-sends an unchanged activity (which also notices a
    restarted Discord), and enforces Discord's update rate limit.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        client_id: str = "",
        paths: Sequence[Path] | None = None,
        min_interval: float = MIN_UPDATE_INTERVAL,
        refresh_interval: float = REFRESH_INTERVAL,
    ) -> None:
        self._paths = list(paths) if paths is not None else None
        self._min_interval = min_interval
        self._refresh_interval = refresh_interval
        self._cond = threading.Condition()
        self._enabled = bool(enabled and client_id)
        self._client_id = client_id
        self._desired: dict[str, Any] | None = None
        self._desired_key: tuple | None = None
        self._anchor: tuple[float, float, str] | None = None
        self._revision = 0
        self._sent_revision = -1
        self._connected = False
        self._connection_id: str | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="discord-rpc", daemon=True
        )
        self._thread.start()

    def configure(self, *, enabled: bool, client_id: str) -> None:
        """Apply new settings; a changed app id replaces the old connection."""
        client_id = (client_id or "").strip()
        enabled = bool(enabled and client_id)
        with self._cond:
            if self._closed:
                return
            if enabled == self._enabled and client_id == self._client_id:
                return
            self._enabled = enabled
            self._client_id = client_id
            self._desired = None
            self._desired_key = None
            self._anchor = None
            self._revision += 1
            self._cond.notify_all()

    def update(self, status: dict[str, Any]) -> None:
        """Store the latest status; changes and slow refreshes wake the worker."""
        now = time.monotonic()
        key = _status_key(status)
        with self._cond:
            if self._closed or not self._enabled:
                return
            stale = (
                self._desired is not None
                and self._anchor is not None
                and now - self._anchor[0] >= self._refresh_interval
            )
            if key == self._desired_key and not stale and not self._seeked(status, now):
                return
            self._desired = activity_for_status(status)
            self._desired_key = key
            self._anchor = (
                now,
                _number(status.get("position")),
                str(status.get("state") or ""),
            )
            self._revision += 1
            self._cond.notify()

    def close(self) -> None:
        """Stop the worker; Discord clears the presence when the socket closes."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=2.0)

    def _wait_retry(self, delay: float) -> None:
        """Wait out a reconnect backoff without letting updates bypass it."""
        deadline = time.monotonic() + delay
        with self._cond:
            while not self._closed and self._enabled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)

    def _seeked(self, status: dict[str, Any], now: float) -> bool:
        """Whether playback jumped away from where the sent activity expects."""
        if self._anchor is None:
            return True
        anchor_time, anchor_position, anchor_state = self._anchor
        position = _number(status.get("position"))
        state = str(status.get("state") or "")
        if state != anchor_state:
            return True
        if state == "playing":
            expected = anchor_position + (now - anchor_time)
            return abs(position - expected) > 2.0
        return abs(position - anchor_position) > 0.5

    def _run(self) -> None:
        conn: RPCConnection | None = None
        retry = RETRY_MIN
        last_send = 0.0
        while True:
            with self._cond:
                while True:
                    if self._closed:
                        break
                    if not self._enabled:
                        if self._connected:
                            break
                    elif self._revision != self._sent_revision and (
                        self._desired is not None or self._connected
                    ):
                        break
                    self._cond.wait()
                if self._closed:
                    break
                enabled = self._enabled
                client_id = self._client_id
                revision = self._revision
                desired = self._desired
                # A new application id needs a new handshake; the old socket
                # keeps reporting under the previous app until it is closed.
                reconnect = (
                    enabled and conn is not None and self._connection_id != client_id
                )
                if enabled and not reconnect:
                    delay = last_send + self._min_interval - time.monotonic()
                    if delay > 0:
                        # Keep waiting out the rate limit; a newer activity
                        # replaces the one we just snapshotted.
                        self._cond.wait(delay)
                        continue

            if reconnect and conn is not None:
                conn.close()
                conn = None
                with self._cond:
                    self._connected = False
                    self._connection_id = None
                    self._sent_revision = -1
                continue

            if not enabled:
                if conn is not None:
                    try:
                        conn.set_activity(None)
                    except (OSError, PresenceError):
                        pass
                    conn.close()
                    conn = None
                with self._cond:
                    self._connected = False
                    self._connection_id = None
                    self._sent_revision = revision
                continue

            if conn is None:
                try:
                    conn = RPCConnection.connect(client_id, paths=self._paths)
                except PresenceError as error:
                    logger.debug("Discord RPC unavailable: %s", error)
                    with self._cond:
                        self._connected = False
                        self._sent_revision = -1
                    self._wait_retry(retry)
                    retry = min(retry * 2, RETRY_MAX)
                    continue
                retry = RETRY_MIN
                with self._cond:
                    self._connected = True
                    self._connection_id = client_id
                    # Force the current activity to be sent over the new socket.
                    self._sent_revision = -1
                continue

            try:
                conn.set_activity(desired)
            except (OSError, PresenceError) as error:
                logger.debug("Discord RPC update failed: %s", error)
                conn.close()
                conn = None
                with self._cond:
                    self._connected = False
                    self._connection_id = None
                    self._sent_revision = -1
                self._wait_retry(retry)
                retry = min(retry * 2, RETRY_MAX)
                continue

            last_send = time.monotonic()
            retry = RETRY_MIN
            with self._cond:
                self._sent_revision = revision

        if conn is not None:
            conn.close()
