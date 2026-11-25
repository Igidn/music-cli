from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from music_cli.presence import (
    OP_CLOSE,
    OP_FRAME,
    OP_HANDSHAKE,
    OP_PING,
    OP_PONG,
    PresenceError,
    PresenceManager,
    RPCConnection,
    activity_for_status,
    encode_frame,
    read_frame,
    socket_paths,
)

PLAYING = {
    "state": "playing",
    "track": {
        "video_id": "dQw4w9WgXcQ",
        "title": "Never Gonna Give You Up",
        "artists": ["Rick Astley"],
        "duration": "3:33",
    },
    "position": 30.0,
    "duration": 213.0,
    "volume": 80,
    "muted": False,
    "loop": False,
    "auto_next": True,
    "queue": [],
}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestActivity:
    def test_playing(self):
        activity = activity_for_status(PLAYING, now=1_700_000_000.0)

        assert activity["type"] == 2
        assert activity["status_display_type"] == 2
        assert activity["details"] == "Never Gonna Give You Up"
        assert activity["state"] == "Rick Astley"
        assert activity["assets"] == {
            "large_image": "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg",
            "large_text": "Never Gonna Give You Up",
        }
        assert activity["buttons"] == [
            {
                "label": "Listen on YouTube Music",
                "url": "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            }
        ]
        started = 1_700_000_000.0 - 30.0
        assert activity["timestamps"] == {
            "start": int(started * 1000),
            "end": int((started + 213.0) * 1000),
        }

    def test_paused_has_no_timestamps(self):
        activity = activity_for_status(dict(PLAYING, state="paused"))

        assert "timestamps" not in activity
        assert activity["state"] == "Paused — Rick Astley"

    def test_stopped_clears_the_presence(self):
        assert activity_for_status({"state": "stopped", "track": None}) is None
        assert activity_for_status(dict(PLAYING, track=None)) is None

    def test_missing_artists_falls_back(self):
        activity = activity_for_status(
            dict(PLAYING, track=dict(PLAYING["track"], artists=[]))
        )

        assert activity["state"] == "YouTube Music"

    def test_missing_video_id_drops_image_and_button(self):
        activity = activity_for_status(
            dict(PLAYING, track=dict(PLAYING["track"], video_id=""))
        )

        assert "assets" not in activity
        assert "buttons" not in activity

    def test_unknown_duration_only_sends_start(self):
        activity = activity_for_status(
            dict(PLAYING, duration=None), now=1_700_000_000.0
        )

        assert set(activity["timestamps"]) == {"start"}

    def test_non_finite_positions_do_not_break_timestamps(self):
        activity = activity_for_status(
            dict(PLAYING, position=float("nan"), duration=float("inf")),
            now=1_700_000_000.0,
        )

        assert activity["timestamps"] == {"start": 1_700_000_000_000}

    def test_fields_are_truncated_to_discord_limits(self):
        activity = activity_for_status(
            dict(
                PLAYING,
                track={
                    "video_id": "v",
                    "title": "T" * 200,
                    "artists": ["A" * 200],
                },
            )
        )

        assert len(activity["details"]) == 128
        assert len(activity["state"]) == 128
        assert len(activity["assets"]["large_text"]) == 128
        assert len(activity["buttons"][0]["label"]) <= 32

    def test_short_title_is_padded(self):
        activity = activity_for_status(
            dict(PLAYING, track={"video_id": "v", "title": "A", "artists": []})
        )

        assert len(activity["details"]) == 2


class ChunkedSocket:
    """A recv-only stand-in that returns one byte per call."""

    def __init__(self, data: bytes):
        self._data = data
        self.calls = 0

    def recv(self, size: int) -> bytes:
        self.calls += 1
        chunk, self._data = self._data[:1], self._data[1:]
        return chunk


class TestFrames:
    def test_encode_is_little_endian(self):
        frame = encode_frame(OP_FRAME, {"hello": "world"})

        assert frame[:4] == (1).to_bytes(4, "little")
        length = int.from_bytes(frame[4:8], "little")
        assert length == len(frame) - 8
        assert read_frame(ChunkedSocket(frame)) == (OP_FRAME, {"hello": "world"})

    def test_read_handles_split_reads(self):
        frame = encode_frame(OP_PING, {"nonce": "abc"})
        conn = ChunkedSocket(frame)

        assert read_frame(conn) == (OP_PING, {"nonce": "abc"})
        assert conn.calls > 1


class FakeDiscord:
    """A tiny Discord IPC server: handshake, one PING, then activity updates."""

    def __init__(
        self,
        path: Path,
        *,
        drop_after: int | None = None,
        reject: str | None = None,
    ) -> None:
        self.path = path
        self.drop_after = drop_after
        self.reject = reject
        self._dropped = False
        self.handshakes: list[dict] = []
        self.activities: list[dict | None] = []
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(2)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._session(conn)
            except ConnectionError:
                pass  # the client closing its socket is how a session ends
            except BaseException as error:  # noqa: BLE001 — reported by the test
                self.errors.append(error)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        opcode, handshake = read_frame(conn)
        assert opcode == OP_HANDSHAKE
        self.handshakes.append(handshake)

        if self.reject is not None:
            conn.sendall(encode_frame(OP_CLOSE, {"code": 4000, "message": self.reject}))
            return

        # The client must answer PING with PONG before the connection is ready.
        conn.sendall(encode_frame(OP_PING, {"nonce": "ping"}))
        assert read_frame(conn) == (OP_PONG, {"nonce": "ping"})
        conn.sendall(
            encode_frame(
                OP_FRAME,
                {"cmd": "DISPATCH", "evt": "READY", "data": {}},
            )
        )

        while True:
            opcode, payload = read_frame(conn)
            if opcode == OP_PING:
                conn.sendall(encode_frame(OP_PONG, payload))
                continue
            if opcode == OP_FRAME and payload.get("cmd") == "SET_ACTIVITY":
                self.activities.append(payload["args"]["activity"])
                conn.sendall(
                    encode_frame(
                        OP_FRAME,
                        {
                            "cmd": "SET_ACTIVITY",
                            "data": None,
                            "nonce": payload["nonce"],
                        },
                    )
                )
                if (
                    self.drop_after is not None
                    and len(self.activities) >= self.drop_after
                    and not self._dropped
                ):
                    self._dropped = True
                    return

    def close(self) -> None:
        self._stop.set()
        self._server.close()
        self._thread.join(2.0)


@pytest.fixture
def socket_dir():
    """A short path: macOS caps AF_UNIX socket paths at ~104 bytes."""
    directory = Path(tempfile.mkdtemp(prefix="music-cli-rpc-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_socket_paths_prefers_the_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socket_paths()[0] == tmp_path / "discord-ipc-0"


def test_connect_reports_a_rejected_client_id(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path, reject="Invalid Client ID")
    try:
        with pytest.raises(PresenceError, match="Invalid Client ID"):
            RPCConnection.connect("123456789012345678", paths=[path])
    finally:
        server.close()


def test_manager_connects_and_sends_activity(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(
        enabled=True,
        client_id="123456789012345678",
        paths=[path],
        min_interval=0.0,
    )
    try:
        manager.update(PLAYING)

        assert wait_for(lambda: server.activities)
        assert server.handshakes == [{"v": 1, "client_id": "123456789012345678"}]
        activity = server.activities[0]
        assert activity["details"] == "Never Gonna Give You Up"
        assert activity["state"] == "Rick Astley"
        assert activity["buttons"][0]["url"].endswith("dQw4w9WgXcQ")
        assert server.errors == []
    finally:
        manager.close()
        server.close()


def test_manager_sends_only_real_changes(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(
        enabled=True,
        client_id="123456789012345678",
        paths=[path],
        min_interval=0.0,
    )
    try:
        manager.update(PLAYING)
        assert wait_for(lambda: len(server.activities) == 1)

        manager.update(PLAYING)
        time.sleep(0.1)
        assert len(server.activities) == 1

        manager.update(dict(PLAYING, position=100.0))
        assert wait_for(lambda: len(server.activities) == 2)

        manager.update(dict(PLAYING, position=101.0))
        time.sleep(0.1)
        assert len(server.activities) == 2
    finally:
        manager.close()
        server.close()


def test_manager_connects_when_enabled_later(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(paths=[path], min_interval=0.0)
    try:
        manager.update(PLAYING)
        time.sleep(0.05)
        assert server.handshakes == []

        manager.configure(enabled=True, client_id="123456789012345678")
        manager.update(PLAYING)

        assert wait_for(lambda: len(server.activities) == 1)
        assert server.handshakes == [{"v": 1, "client_id": "123456789012345678"}]
    finally:
        manager.close()
        server.close()


def test_manager_clears_when_playback_stops(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(
        enabled=True,
        client_id="123456789012345678",
        paths=[path],
        min_interval=0.0,
    )
    try:
        manager.update(PLAYING)
        assert wait_for(lambda: len(server.activities) == 1)

        manager.update(dict(PLAYING, state="stopped", track=None))
        assert wait_for(lambda: len(server.activities) == 2)
        assert server.activities[1] is None
    finally:
        manager.close()
        server.close()


def test_manager_clears_and_disconnects_when_disabled(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(
        enabled=True,
        client_id="123456789012345678",
        paths=[path],
        min_interval=0.0,
    )
    try:
        manager.update(PLAYING)
        assert wait_for(lambda: len(server.activities) == 1)

        manager.configure(enabled=False, client_id="")
        assert wait_for(lambda: len(server.activities) == 2)
        assert server.activities[1] is None
    finally:
        manager.close()
        server.close()


def test_manager_reconnects_when_the_application_id_changes(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(
        enabled=True,
        client_id="111111111111111111",
        paths=[path],
        min_interval=0.0,
    )
    try:
        manager.update(PLAYING)
        assert wait_for(lambda: len(server.activities) == 1)

        manager.configure(enabled=True, client_id="222222222222222222")
        manager.update(PLAYING)

        assert wait_for(lambda: len(server.handshakes) == 2)
        assert server.handshakes[1] == {
            "v": 1,
            "client_id": "222222222222222222",
        }
        assert wait_for(lambda: len(server.activities) == 2)
    finally:
        manager.close()
        server.close()


def test_manager_notices_a_restarted_discord(socket_dir, monkeypatch):
    # Discord closes the socket; the periodic refresh fails, reconnects, and
    # sends the current activity to the new instance.
    monkeypatch.setattr("music_cli.presence.RETRY_MIN", 0.05)
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path, drop_after=1)
    manager = PresenceManager(
        enabled=True,
        client_id="123456789012345678",
        paths=[path],
        min_interval=0.0,
        refresh_interval=0.05,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(server.handshakes) < 2:
            manager.update(PLAYING)
            time.sleep(0.01)

        assert len(server.handshakes) == 2
        assert wait_for(lambda: len(server.activities) >= 2)
        assert server.activities[-1]["details"] == "Never Gonna Give You Up"
    finally:
        manager.close()
        server.close()


def test_disabled_manager_never_connects(socket_dir):
    path = socket_dir / "discord-ipc-0"
    server = FakeDiscord(path)
    manager = PresenceManager(paths=[path], min_interval=0.0)
    try:
        manager.update(PLAYING)
        time.sleep(0.1)
        assert server.handshakes == []
    finally:
        manager.close()
        server.close()
