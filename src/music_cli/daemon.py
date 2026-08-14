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
import select
import signal
import socket
import sys
import threading
from typing import TYPE_CHECKING

from . import ipc
from .core.errors import PlayerError

if TYPE_CHECKING:
    from .session import PlaybackSession

_STATE_VALUES = ("on", "off", "toggle")
_PLAY_TARGETS = ("query", "video_id", "playlist_id")


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
    elif cmd in ("stop", "quit"):
        return {"ok": True, "data": None}
    elif cmd != "status":
        return {"ok": False, "error": f"unknown command: {cmd!r}"}
    # "status" falls through, as do the transport commands above.
    return {"ok": True, "data": session.status()}


def _play(session: PlaybackSession, request: dict) -> dict:
    targets = [key for key in _PLAY_TARGETS if request.get(key)]
    if len(targets) != 1:
        return {
            "ok": False,
            "error": "play requires exactly one of: query, video_id, playlist_id",
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
        session.play_video(request["video_id"])
    else:
        session.play_playlist(request["playlist_id"])
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
    # Track-end fires from an AVFoundation notification; the flag lets the
    # serve loop run auto-next on the main thread, where pump() runs.
    eof = threading.Event()
    session.client.on_track_end = eof.set
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: stop.set())
    try:
        while not stop.is_set():
            readable, _, _ = select.select([server], [], [], 0.02)
            if readable:
                conn, _ = server.accept()
                with conn:
                    try:
                        request = ipc.recv_message(conn)
                    except PlayerError:
                        continue  # malformed request; keep serving
                    response = handle_request(session, request)
                    ipc.send_message(conn, response)
                if request.get("cmd") in ("stop", "quit"):
                    stop.set()
            if eof.is_set():
                eof.clear()
                try:
                    session.on_track_end()
                except PlayerError:
                    pass  # a failed auto-next must not kill the daemon
            session.client.player.pump()
    finally:
        server.close()
        path.unlink(missing_ok=True)
        session.close()


if __name__ == "__main__":
    main()
