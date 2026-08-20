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
    elif cmd == "remove_download":
        session.remove_download(request["video_id"])
        return {"ok": True, "data": None}
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
        if request.get("from_downloads"):
            session.play_download(request["video_id"], request.get("title", ""))
        else:
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
        if progress.get("status") == "finished":
            _push_download_progress(
                subscribers, {"event": "download", "finished": True}
            )
            return
        if progress.get("status") != "downloading":
            return
        total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
        downloaded = progress.get("downloaded_bytes") or 0
        percent = round(downloaded / total * 100) if total else None
        _push_download_progress(
            subscribers,
            {"event": "download", "percent": percent, "downloaded": downloaded},
        )

    return hook


class _AsyncPlay:
    """A single-track ``play`` running in the background.

    A worker thread does the network work (search, resolve, download into the
    cache) so the serve loop keeps answering other requests; the ready file is
    handed to the main thread, where AVFoundation playback actually starts.
    Download progress is streamed to the client so its socket stays alive.
    """

    def __init__(self, conn: socket.socket, request: dict[str, Any]) -> None:
        self.conn = conn
        self.request = request
        self.client: Any = None
        self.ready = threading.Event()
        self.stream: Any = None
        self.error: Exception | None = None

    def progress(self, info: dict[str, Any]) -> None:
        """yt-dlp progress hook: feed subscribers and stream to the client.

        The subscriber feed drives the TUI's live "Downloading… N%" status
        during an async play; the client connection carries the plain
        ``type: progress`` lines for headless callers.
        """
        if self.client is not None:
            events = self.client.download_progress
            if events is not None:
                events(info)
        if info.get("status") != "downloading":
            return
        total = info.get("total_bytes") or info.get("total_bytes_estimate")
        downloaded = info.get("downloaded_bytes") or 0
        percent = round(downloaded / total * 100) if total else None
        try:
            payload = json.dumps({"type": "progress", "percent": percent}).encode()
            self.conn.sendall(payload + b"\n")
        except OSError:
            pass  # client gave up; keep downloading so the track is still cached


_ASYNC_PLAY_TARGETS = ("video_id", "query")


def _is_async_play(request: dict[str, Any]) -> bool:
    """Whether ``request`` can run on the async single-track play path."""
    has_playlist = any(
        k in request for k in ("playlist_id", "album_id", "queue_index")
    )
    return ("video_id" in request or "query" in request) and not has_playlist


def _spawn_async_play(
    session: PlaybackSession, request: dict[str, Any], conn: socket.socket
) -> _AsyncPlay:
    """Apply launch flags and start the background resolve+download for a play."""
    slot = _AsyncPlay(conn, request)
    slot.client = session.client
    # Launch flags must shape playback before the track starts.
    if "volume" in request:
        session.set_volume(volume=request["volume"])
    if "loop" in request:
        session.set_loop("on" if request["loop"] else "off")
    if "auto_next" in request:
        session.set_auto_next("on" if request["auto_next"] else "off")
    target = "video_id" if "video_id" in request else "query"
    threading.Thread(
        target=_async_play_worker,
        args=(session, target, request, slot),
        daemon=True,
        name="play-prep",
    ).start()
    return slot


def _async_play_worker(
    session: PlaybackSession,
    target: str,
    request: dict[str, Any],
    slot: _AsyncPlay,
) -> None:
    """Thread body: network-only resolve+download; the serve loop starts playback."""
    try:
        slot.stream = session.prepare_play(target, request, progress=slot.progress)
    except Exception as error:  # noqa: BLE001 — daemon boundary; report to client
        slot.error = error
    finally:
        slot.ready.set()


def _resolve_pending_plays(session, pending_plays: dict) -> None:
    """Start playback for finished downloads and reply to their clients.

    Runs on the main thread so AVFoundation playback can pump the run loop.
    """
    for conn, slot in list(pending_plays.items()):
        if not slot.ready.is_set():
            continue
        if slot.error is not None:
            response = {"ok": False, "error": str(slot.error)}
        else:
            try:
                session.commit_play(slot.stream)
                response = {"ok": True, "data": session.status()}
            except PlayerError as error:
                response = {"ok": False, "error": str(error)}
        try:
            ipc.send_message(slot.conn, response)
        except OSError:
            pass  # client disconnected mid-download; the track is cached regardless
        try:
            conn.close()
        except OSError:
            pass
        del pending_plays[conn]


class _AsyncDownload:
    """A standalone ``download`` running in the background.

    Like :class:`_AsyncPlay`, the slow network+download work runs off the
    serve loop (which keeps pumping AVFoundation); when the file is committed
    and recorded, the loop answers the requesting client and notifies
    subscribers so the TUI refreshes its Downloads tree.
    """

    def __init__(self, conn: socket.socket, request: dict[str, Any]) -> None:
        self.conn = conn
        self.request = request
        self.client: Any = None
        self.ready = threading.Event()
        self.error: Exception | None = None

    def progress(self, info: dict[str, Any]) -> None:
        """Stream progress to subscribers and the requesting client.

        The events hook feeds the TUI's prioritized status line; the client
        connection carries plain ``type: progress`` lines so the headless CLI
        (``send_play_request``) can show the same download live.
        """
        if self.client is not None:
            events = self.client.download_progress
            if events is not None:
                events(info)
        if info.get("status") != "downloading":
            return
        total = info.get("total_bytes") or info.get("total_bytes_estimate")
        downloaded = info.get("downloaded_bytes") or 0
        percent = round(downloaded / total * 100) if total else None
        try:
            payload = json.dumps({"type": "progress", "percent": percent}).encode()
            self.conn.sendall(payload + b"\n")
        except OSError:
            pass  # client gave up; keep downloading so the track is still cached


def _spawn_async_download(
    session: PlaybackSession, request: dict[str, Any], conn: socket.socket
) -> _AsyncDownload:
    """Start a background resolve+download; the loop replies when it finishes."""
    slot = _AsyncDownload(conn, request)
    slot.client = session.client
    threading.Thread(
        target=_download_worker,
        args=(session, request, slot),
        daemon=True,
        name="download",
    ).start()
    return slot


def _download_worker(
    session: PlaybackSession,
    request: dict[str, Any],
    slot: _AsyncDownload,
) -> None:
    """Thread body: resolve, download into the cache, pin and record the track."""
    video_id = request["video_id"]
    try:
        session.client.download(video_id, progress=slot.progress)
    except Exception as error:  # noqa: BLE001 — daemon boundary; report to client
        slot.error = error
    finally:
        slot.ready.set()


def _resolve_pending_downloads(
    session, pending_downloads: dict, subscribers: dict
) -> None:
    """Reply to finished background downloads and notify subscribers."""
    for conn, slot in list(pending_downloads.items()):
        if not slot.ready.is_set():
            continue
        if slot.error is not None:
            response = {"ok": False, "error": str(slot.error)}
        else:
            response = {"ok": True, "data": {"video_id": slot.request["video_id"]}}
            _push_event(subscribers, {"event": "downloads"})
        try:
            ipc.send_message(slot.conn, response)
        except OSError:
            pass  # client disconnected mid-download; the track is cached regardless
        try:
            conn.close()
        except OSError:
            pass
        del pending_downloads[conn]


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
    pending_plays: dict[socket.socket, _AsyncPlay] = {}
    pending_downloads: dict[socket.socket, _AsyncDownload] = {}
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
                # Bound the first read: a client that connects and sends
                # nothing must not pin the main thread in recv, which would
                # freeze the loop below (signals, other requests, playback).
                conn.settimeout(30)
                try:
                    request = ipc.recv_message(conn)
                except PlayerError:
                    conn.close()
                    continue  # malformed request; keep serving
                cmd = request.get("cmd")
                if cmd == "download" and request.get("video_id"):
                    # Slow offline download: run off the event loop and reply
                    # (with progress streamed) when the file is on disk.
                    pending_downloads[conn] = _spawn_async_download(
                        session, request, conn
                    )
                    last_activity = time.monotonic()
                    continue
                if cmd == "play" and _is_async_play(request):
                    # Run the slow resolve+download off the event loop; the
                    # response is streamed back when playback actually starts.
                    pending_plays[conn] = _spawn_async_play(session, request, conn)
                    eof.clear()  # supersede any pending track-end
                    last_activity = time.monotonic()
                    continue
                try:
                    response = handle_request(session, request)
                    try:
                        ipc.send_message(conn, response)
                    except OSError:
                        pass  # client disconnected before reading the answer
                finally:
                    conn.close()
                # An offline download was removed; tell subscribers to refresh.
                if cmd == "remove_download" and response.get("ok"):
                    _push_event(subscribers, {"event": "downloads"})
                # A play request replaces the current track; drop any pending
                # track-end from the track it superseded, or the loop below
                # would auto-advance straight past what the user just played.
                if cmd == "play" and response.get("ok"):
                    eof.clear()
                last_activity = time.monotonic()
                if cmd == "quit":
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
        # Start playback for any finished background downloads (main thread).
        _resolve_pending_plays(session, pending_plays)
        # Reply to finished offline downloads and tell subscribers to refresh.
        _resolve_pending_downloads(session, pending_downloads, subscribers)
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
        if _idle(
            session, subscribers, pending_plays, pending_downloads, last_activity, idle_timeout
        ):
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


def _push_event(
    subscribers: dict[socket.socket, deque[bytes]], event: dict[str, Any]
) -> None:
    """Fan one non-status event (e.g. ``downloads`` refresh) to subscribers."""
    payload = json.dumps(event).encode() + b"\n"
    for conn, buffer in list(subscribers.items()):
        buffer.append(payload)
        if sum(len(chunk) for chunk in buffer) > _MAX_SUBSCRIBER_BYTES:
            _drop_subscriber(conn, subscribers)


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
    pending_plays: dict,
    pending_downloads: dict,
    last_activity: float,
    idle_timeout: float,
) -> bool:
    """Whether to idle-exit: nothing playing, no subscribers, quiet for a while.

    Any background download in flight (play or offline) is still activity:
    let it finish and cache the track before the daemon exits itself.
    """
    if session.client.current is not None:
        return False
    if subscribers:
        return False
    if pending_plays:
        return False
    if pending_downloads:
        return False
    return time.monotonic() - last_activity > idle_timeout


if __name__ == "__main__":
    main()
