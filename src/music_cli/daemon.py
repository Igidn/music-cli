"""Playback daemon: owns the session behind the IPC Unix socket.

The daemon is the only process that plays audio in CLI mode. The CLI spawns
it detached (``python -m music_cli.daemon``) and talks to it over the
protocol in :mod:`music_cli.ipc`.

AVFoundation's media pipeline is serviced by the main ``NSRunLoop``, so the
serve loop is single-threaded: it polls the socket with a ~20ms timeout and
pumps the run loop between connections, mirroring the TUI's pump_platform.
"""

from __future__ import annotations

import argparse
import json
import select
import signal
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import ipc
from .core.errors import PlayerError

if TYPE_CHECKING:
    from .session import PlaybackSession

_STATE_VALUES = ("on", "off", "toggle")
_PLAY_TARGETS = ("query", "video_id", "playlist_id", "album_id", "queue_index")
#: Seconds a stopped, un-subscribed daemon lingers before exiting itself.
IDLE_TIMEOUT = 30.0
#: Cap per-subscriber bytes buffered awaiting a writable socket.
_MAX_SUBSCRIBER_BYTES = 1 << 20
#: Heartbeat interval at which the current status is re-pushed.
_HEARTBEAT = 0.5


def _push_download_progress(
    subscribers: dict[socket.socket, deque[bytes]], status: dict
) -> None:
    """Enqueue and immediately flush a download-progress event.

    Runs inside a play request, while the serve loop is blocked in the
    download, so buffering alone would never flush until the download ends.
    Each hook call pushes straight onto the wire instead.
    """
    payload = json.dumps(status).encode() + b"\n"
    for _conn, buffer in list(subscribers.items()):
        buffer.append(payload)
    for conn in list(subscribers.keys()):
        _flush_subscriber(conn, subscribers)


def handle_request(session: PlaybackSession, request: dict) -> dict:
    """Pure dispatch: one request dict -> one response dict. No socket I/O.

    Every failure is reported in the response, never raised: the daemon must
    survive bad requests and buggy commands alike.
    """
    try:
        return _dispatch(session, request)
    except PlayerError as error:
        return {"ok": False, "error": str(error)}
    except Exception as error:  # noqa: BLE001 — daemon boundary: a bug in one
        # command must not take down playback for every later request.
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def _dispatch(session: PlaybackSession, request: dict) -> dict:
    cmd = request.get("cmd")
    if cmd == "play":
        return _play(session, request)
    if cmd == "pause":
        session.pause()
    elif cmd == "resume":
        resumed = _resume(session)
        if resumed is not None:
            return resumed
    elif cmd == "toggle":
        session.toggle()
    elif cmd == "next":
        session.next_track()  # None just means the queue ran out
    elif cmd == "seek":
        return _seek(session, request)
    elif cmd == "volume":
        return _volume(session, request)
    elif cmd in ("mute", "loop", "auto_next"):
        return _toggle_state(session, cmd, request)
    elif cmd == "queue":
        return {"ok": True, "data": session.queue()}
    elif cmd == "stop":
        session.stop()
        return {"ok": True, "data": session.status()}
    elif cmd == "quit":
        return {"ok": True, "data": None}
    elif cmd != "status":
        return {"ok": False, "error": f"unknown command: {cmd!r}"}
    # "status" falls through, as do the transport commands above.
    return {"ok": True, "data": session.status()}


def _play(session: PlaybackSession, request: dict) -> dict:
    targets = [key for key in _PLAY_TARGETS if key in request]
    if len(targets) != 1:
        return {
            "ok": False,
            "error": (
                "play requires exactly one of: query, video_id, playlist_id,"
                " album_id, queue_index"
            ),
        }
    # Launch flags first: they must shape playback before the track starts.
    if "volume" in request:
        session.set_volume(volume=request["volume"])
    if "loop" in request:
        session.set_loop("on" if request["loop"] else "off")
    if "auto_next" in request:
        session.set_auto_next("on" if request["auto_next"] else "off")
    target = targets[0]
    if target == "query":
        session.play_query(request["query"])
    elif target == "video_id":
        session.play_video(request["video_id"], request.get("title", ""))
    elif target == "playlist_id":
        session.play_playlist(request["playlist_id"], request.get("playlist_index", 0))
    elif target == "album_id":
        session.play_album(request["album_id"], request.get("album_index", 0))
    else:
        session.play_queue_track(request["queue_index"])
    return {"ok": True, "data": session.status()}


def _resume(session: PlaybackSession) -> dict | None:
    """Resume playback, falling back to the last history track when idle.

    Returns an error response when there is nothing to resume, else None.
    """
    if session.client.current is None:
        if session.resume_last() is None:
            return {
                "ok": False,
                "error": "nothing to resume — play history is empty",
            }
    else:
        session.resume()
    return None


def _seek(session: PlaybackSession, request: dict) -> dict:
    if "offset" in request:
        session.seek(offset=request["offset"])
    elif "position" in request:
        session.seek(position=request["position"])
    else:
        return {"ok": False, "error": "seek requires offset or position"}
    return {"ok": True, "data": session.status()}


def _volume(session: PlaybackSession, request: dict) -> dict:
    if "level" in request:
        session.set_volume(volume=request["level"])
    elif "delta" in request:
        session.set_volume(delta=request["delta"])
    else:
        return {"ok": False, "error": "volume requires level or delta"}
    return {"ok": True, "data": {"volume": session.client.player.volume}}


def _toggle_state(session: PlaybackSession, cmd: str, request: dict) -> dict:
    state = request.get("state")
    if state not in _STATE_VALUES:
        return {"ok": False, "error": f"{cmd} requires state on|off|toggle"}
    setters = {"mute": session.set_muted, "loop": session.set_loop}
    setter = setters.get(cmd, session.set_auto_next)
    return {"ok": True, "data": {cmd: setter(state)}}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="music-cli-daemon",
        description="music-cli playback daemon (spawned by the CLI).",
    )
    parser.add_argument(
        "--cookies", metavar="FILE", default=None, help="Netscape cookie file"
    )
    parser.add_argument("--volume", type=int, default=80, help="initial volume")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=IDLE_TIMEOUT,
        help="seconds idle before the daemon exits itself",
    )
    return parser.parse_args(argv)


def _claim_socket(path) -> socket.socket:
    """Bind the control socket, refusing to start over a live daemon.

    A stale socket file (previous daemon killed without cleanup) refuses
    connections and is simply unlinked.
    """
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()
        else:
            print("music-cli: daemon already running", file=sys.stderr)
            sys.exit(1)
        finally:
            probe.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(5)
    return server


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    from .client import MusicClient
    from .session import PlaybackSession  # lazy: absent at module-import time
    from .storage.cache import AudioCache
    from .yt.cookies import Cookies

    cookies = Cookies.from_file(args.cookies) if args.cookies else None
    client = MusicClient(cookies=cookies, volume=args.volume, cache=AudioCache())
    session = PlaybackSession(client)

    path = ipc.socket_path()
    server = _claim_socket(path)
    events_path = ipc.events_socket_path()
    event_server = _claim_socket(events_path)
    # Track-end fires from an AVFoundation notification; the flag lets the
    # serve loop run auto-next on the main thread, where pump() runs.
    eof = threading.Event()
    session.client.on_track_end = eof.set
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: stop.set())
    try:
        _serve(session, server, event_server, eof, stop, args.idle_timeout)
    finally:
        server.close()
        event_server.close()
        path.unlink(missing_ok=True)
        events_path.unlink(missing_ok=True)
        session.close()


def _download_hook(
    subscribers: dict[socket.socket, deque[bytes]]
) -> Callable[[dict[str, Any]], None]:
    """A yt-dlp progress hook that pushes the download percent to subscribers.

    ``percent`` is None when the total size is unknown (yt-dlp only gives an
    estimate / no total), so the TUI can fall back to an indeterminate hint.
    """

    def hook(progress: dict[str, Any]) -> None:
        if progress.get("status") != "downloading":
            return
        total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
        downloaded = progress.get("downloaded_bytes") or 0
        percent = round(downloaded / total * 100) if total else None
        _push_download_progress(
            subscribers, {"event": "download", "percent": percent}
        )

    return hook


def _serve(
    session: PlaybackSession,
    server: socket.socket,
    event_server: socket.socket,
    eof: threading.Event,
    stop: threading.Event,
    idle_timeout: float,
) -> None:
    """Serve control requests and push state events until quit/idle/signal.

    One loop pumps the AVFoundation run loop, handles control requests, and
    fans state events out to subscribers. Push happens when the discrete
    state signature changes or as a coarse heartbeat, so a connected TUI gets
    fresh ``position`` without polling.
    """
    subscribers: dict[socket.socket, deque[bytes]] = {}
    last_signature: tuple | None = None
    last_heartbeat = time.monotonic()
    last_activity = time.monotonic()
    session.client.download_progress = _download_hook(subscribers)
    while not stop.is_set():
        # Only poll a subscriber for writability when it has pending bytes:
        # a connected socket is always writable, so selecting on it turns the
        # 20ms poll into a busy loop (100% CPU) for as long as a TUI is attached.
        readable, writable, _ = select.select(
            [server, event_server, *subscribers],
            [sock for sock, buffer in subscribers.items() if buffer],
            [],
            0.02,
        )
        for sock in readable:
            if sock is server:
                conn, _ = server.accept()
                with conn:
                    try:
                        request = ipc.recv_message(conn)
                    except PlayerError:
                        continue  # malformed request; keep serving
                    response = handle_request(session, request)
                    ipc.send_message(conn, response)
                    # A play request replaces the current track; drop any
                    # pending track-end from the track it superseded, or the
                    # loop below would auto-advance straight past what the
                    # user just asked to play (up-next flashes as current).
                    if request.get("cmd") == "play" and response.get("ok"):
                        eof.clear()
                last_activity = time.monotonic()
                if request.get("cmd") == "quit":
                    return
            elif sock is event_server:
                conn, _ = event_server.accept()
                conn.setblocking(False)
                subscribers[conn] = deque()
                last_activity = time.monotonic()
            elif sock in subscribers:
                # Non-control senders send one subscribe line then go quiet;
                # read to notice a closed (or badly-behaved) connection.
                try:
                    data = sock.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    _drop_subscriber(sock, subscribers)
        for sock in writable:
            if sock in subscribers:
                _flush_subscriber(sock, subscribers)
        if eof.is_set():
            eof.clear()
            try:
                session.on_track_end()
            except PlayerError:
                pass  # a failed auto-next must not kill the daemon
        status = session.status()
        signature = _status_signature(status)
        if signature != last_signature:
            last_signature = signature
            last_heartbeat = time.monotonic()
            _push(subscribers, status)
        elif time.monotonic() - last_heartbeat >= _HEARTBEAT:
            last_heartbeat = time.monotonic()
            _push(subscribers, status)
        if _idle(session, subscribers, last_activity, idle_timeout):
            return
        session.client.player.pump()


def _status_signature(status: dict) -> tuple:
    """The discrete state signature: anything except position and duration."""
    track = status["track"]
    return (
        status["state"],
        track["video_id"] if track else None,
        status["queue"],
        status["volume"],
        status["muted"],
        status["loop"],
        status["auto_next"],
    )


def _push(subscribers: dict[socket.socket, deque[bytes]], status: dict) -> None:
    """Enqueue the current status as an event for every subscriber."""
    payload = json.dumps({"event": "state", "status": status}).encode() + b"\n"
    for conn, buffer in list(subscribers.items()):
        buffer.append(payload)
        if sum(len(chunk) for chunk in buffer) > _MAX_SUBSCRIBER_BYTES:
            _drop_subscriber(conn, subscribers)


def _flush_subscriber(
    conn: socket.socket, subscribers: dict[socket.socket, deque[bytes]]
) -> None:
    """Non-blockingly drain a subscriber's pending events; drop it when stuck."""
    buffer = subscribers[conn]
    while buffer:
        chunk = buffer[0]
        try:
            sent = conn.send(chunk)
        except BlockingIOError, ConnectionError, OSError:
            _drop_subscriber(conn, subscribers)
            return
        if sent == 0:
            _drop_subscriber(conn, subscribers)
            return
        if sent < len(chunk):
            buffer[0] = chunk[sent:]
            return
        buffer.popleft()


def _drop_subscriber(conn: socket.socket, subscribers: dict) -> None:
    """Remove and close a dead, slow, or unreadable subscriber."""
    if subscribers.pop(conn, None) is not None:
        try:
            conn.close()
        except OSError:
            pass


def _idle(
    session: PlaybackSession,
    subscribers: dict,
    last_activity: float,
    idle_timeout: float,
) -> bool:
    """Whether to idle-exit: nothing playing, no subscribers, quiet for a while."""
    if session.client.current is not None:
        return False
    if subscribers:
        return False
    return time.monotonic() - last_activity > idle_timeout


if __name__ == "__main__":
    main()
