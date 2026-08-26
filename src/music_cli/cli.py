"""Command-line interface: transport control over IPC plus stateless queries.

Playback commands talk to the background daemon (see ``ipc.py``); ``play``
starts it on demand. Search, playlist management and history run directly
against the API or the local database and need no daemon.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from music_cli import build_client

from . import ipc
from .core.errors import PlayerError
from .storage.state import PlayHistoryStore
from .yt.search import format_duration

_console = Console()
_errors = Console(stderr=True)

STATES = ("on", "off", "toggle")
SEARCH_FILTERS = ("songs", "videos", "albums", "artists", "playlists")


def register_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Attach every CLI command to the given argparse subparsers."""
    play = subparsers.add_parser("play", help="play a query, video or playlist")
    target = play.add_mutually_exclusive_group(required=True)
    target.add_argument("query", nargs="?", help="search query to play")
    target.add_argument("--video-id", metavar="ID", help="play this video id")
    target.add_argument("--playlist", metavar="ID", help="play this playlist")
    play.add_argument("--loop", action="store_true", help="loop the track")
    play.add_argument(
        "--no-auto-next",
        dest="auto_next",
        action="store_false",
        default=None,
        help="stop after this track",
    )
    play.add_argument(
        "--volume",
        dest="play_volume",
        type=int,
        metavar="N",
        default=None,
        help="set the volume (0-100) for this playback",
    )

    for name, help_text in (
        ("pause", "pause playback"),
        ("resume", "resume a paused track, or replay the last track when idle"),
        ("toggle", "toggle play/pause"),
        ("next", "skip to the next queued track"),
        ("stop", "stop playback"),
    ):
        subparsers.add_parser(name, help=help_text)

    download = subparsers.add_parser(
        "download", help="download a track for offline listening"
    )
    download.add_argument(
        "video_id",
        nargs="?",
        metavar="TRACK_ID",
        help="the video id of the track to download (default: currently playing track)",
    )

    seek = subparsers.add_parser("seek", help="seek within the current track")
    seek.add_argument(
        "value",
        metavar="VALUE",
        type=_seek_value,
        help="+N seconds forward, -N back, N absolute",
    )

    volume = subparsers.add_parser("volume", help="set or adjust the volume")
    volume.add_argument(
        "value",
        metavar="VALUE",
        type=_volume_value,
        help="N absolute (0-100), +N/-N relative",
    )

    for name, help_text in (
        ("mute", "mute or unmute"),
        ("loop", "turn loop on or off"),
        ("auto-next", "turn auto-next on or off"),
    ):
        state_parser = subparsers.add_parser(name, help=help_text)
        state_parser.add_argument("state", choices=STATES)

    _add_json(subparsers.add_parser("status", help="show what is playing"))
    _add_json(subparsers.add_parser("queue", help="show the up-next queue"))

    search = subparsers.add_parser("search", help="search YouTube Music")
    search.add_argument("query", nargs="+", help="search terms")
    search.add_argument(
        "--limit", type=int, default=10, metavar="N", help="max results (default: 10)"
    )
    search.add_argument(
        "--filter",
        choices=SEARCH_FILTERS,
        default=None,
        help="restrict to one result type",
    )
    _add_json(search)

    playlists = subparsers.add_parser("playlists", help="manage your library playlists")
    commands = playlists.add_subparsers(dest="playlists_command", required=True)
    _add_json(commands.add_parser("list", help="list your playlists"))
    tracks = commands.add_parser("tracks", help="list a playlist's tracks")
    tracks.add_argument("id", metavar="ID", help="playlist id")
    _add_json(tracks)
    create = commands.add_parser("create", help="create a playlist")
    create.add_argument("name", metavar="NAME")
    rename = commands.add_parser("rename", help="rename a playlist")
    rename.add_argument("id", metavar="ID")
    rename.add_argument("name", metavar="NAME")
    add = commands.add_parser("add", help="add tracks to a playlist")
    add.add_argument("id", metavar="ID")
    add.add_argument("video_ids", nargs="+", metavar="VIDEO_ID")
    remove = commands.add_parser("remove", help="remove a track from a playlist")
    remove.add_argument("id", metavar="ID")
    remove.add_argument("video_id", metavar="VIDEO_ID")
    play = commands.add_parser("play", help="play a playlist, optionally from a track")
    play.add_argument("id", metavar="ID", help="playlist id")
    play.add_argument(
        "--track",
        type=int,
        default=1,
        metavar="N",
        help="start from this track, 1-based in list order (default: 1)",
    )
    downloaded = commands.add_parser(
        "downloaded", help="list tracks you downloaded for offline listening"
    )
    _add_json(downloaded)

    history = subparsers.add_parser("history", help="show recently played tracks")
    history.add_argument(
        "--limit", type=int, default=15, metavar="N", help="max entries (default: 15)"
    )
    _add_json(history)


def run(args: argparse.Namespace) -> int:
    """Dispatch the parsed command; returns the process exit code."""
    handler = _DISPATCH.get(args.command)
    if handler is None:
        _errors.print(f"music-cli: unknown command {args.command!r}", style="bold red")
        return 1
    try:
        return handler(args)
    except PlayerError as error:
        _errors.print(f"music-cli: {error}", style="bold red")
        return 1


def _ensure_daemon(args: argparse.Namespace) -> None:
    """Ensure the background daemon is up, spawning it if it idle-exited."""
    ipc.ensure_daemon(getattr(args, "cookies", None), getattr(args, "volume", None))


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )


def _seek_value(text: str) -> tuple[str, float]:
    """Parse a seek argument: +N/-N relative seconds, N absolute."""
    try:
        number = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid seek value: {text!r}") from error
    if text.startswith(("+", "-")):
        return ("offset", number)
    return ("position", number)


def _volume_value(text: str) -> tuple[str, int]:
    """Parse a volume argument: +N/-N relative, N absolute."""
    try:
        number = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid volume: {text!r}") from error
    if text.startswith(("+", "-")):
        return ("delta", number)
    return ("level", number)


def _send(
    args: argparse.Namespace, request: dict[str, Any], timeout: float = 30.0
) -> Any | None:
    """Send ``request`` to the daemon; print the error and return None on failure."""
    response = ipc.send_request(request, timeout=timeout)
    if not response.get("ok"):
        error = response.get("error", "unknown error")
        _errors.print(f"music-cli: {error}", style="bold red")
        return None
    return response.get("data")


def _print_json(value: Any) -> None:
    sys.stdout.buffer.write(
        json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
    )


def _onoff(value: Any) -> str:
    return "on" if value else "off"


def _track_line(data: dict[str, Any]) -> Text | str:
    """The one-line 'now playing' confirmation shared by transport commands."""
    track = data.get("track")
    if not track:
        return "Nothing is playing"
    word = "Playing" if data.get("state") == "playing" else "Paused"
    line = Text()
    line.append(word, style="bold green")
    line.append(f"  {track.get('title') or 'Unknown'}", style="bold")
    artists = ", ".join(track.get("artists") or [])
    if artists:
        line.append(f" — {artists}", style="dim")
    if "duration" in data:
        duration = format_duration(data.get("duration")) or "--:--"
        line.append(f" ({duration})", style="dim")
    return line


def _settings_line(data: dict[str, Any]) -> str:
    queue = data.get("queue")
    queued = len(queue) if isinstance(queue, list) else queue or 0
    return (
        f"volume {data.get('volume', '?')}"
        f" · muted {_onoff(data.get('muted'))}"
        f" · loop {_onoff(data.get('loop'))}"
        f" · auto-next {_onoff(data.get('auto_next'))}"
        f" · {queued} queued"
    )


def _cmd_play(args: argparse.Namespace) -> int:
    request: dict[str, Any] = {"cmd": "play"}
    if args.query is not None:
        request["query"] = args.query
    elif args.video_id is not None:
        request["video_id"] = args.video_id
    else:
        request["playlist_id"] = args.playlist
    if args.loop:
        request["loop"] = True
    if args.auto_next is not None:
        request["auto_next"] = args.auto_next
    if args.play_volume is not None:
        request["volume"] = args.play_volume
    _ensure_daemon(args)
    # Stream resolution and a possible download happen asynchronously in the
    # daemon; the IPC streams progress back so the client stays alive through
    # a slow download and reports it live instead of blocking on a silent socket.
    try:
        with _console.status("Resolving stream…", spinner="dots") as status:
            response = ipc.send_play_request(
                request,
                on_progress=lambda pct: status.update(
                    "Downloading…" if pct is None else f"Downloading… {pct:.0f}%"
                ),
            )
    except PlayerError as error:
        _errors.print(f"music-cli: {error}", style="bold red")
        return 1
    if not response.get("ok"):
        _errors.print(
            f"music-cli: {response.get('error', 'unknown error')}", style="bold red"
        )
        return 1
    _console.print(_track_line(response.get("data")))
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """resume a paused track, or start the daemon and replay the last track."""
    _ensure_daemon(args)
    # resume may fall back to replaying the last track, which re-resolves its stream.
    with _console.status("Resolving stream…", spinner="dots"):
        data = _send(args, {"cmd": "resume"})
    if data is None:
        return 1
    _console.print(_track_line(data))
    return 0


def _cmd_transport(args: argparse.Namespace) -> int:
    """pause/toggle: confirm with the resulting now-playing line."""
    _ensure_daemon(args)
    data = _send(args, {"cmd": args.command})
    if data is None:
        return 1
    _console.print(_track_line(data))
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    """next: skip to the next queued track, resolving its stream first."""
    _ensure_daemon(args)
    with _console.status("Resolving next track…", spinner="dots"):
        data = _send(args, {"cmd": "next"})
    if data is None:
        return 1
    _console.print(_track_line(data))
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    """stop: halt playback but leave the daemon alive to idle-exit."""
    _ensure_daemon(args)
    if _send(args, {"cmd": "stop"}) is None:
        return 1
    _console.print("Stopped")
    return 0


def _cmd_seek(args: argparse.Namespace) -> int:
    key, number = args.value
    _ensure_daemon(args)
    data = _send(args, {"cmd": "seek", key: number})
    if data is None:
        return 1
    position = format_duration((data or {}).get("position")) or "--:--"
    _console.print(f"Position {position}")
    return 0


def _cmd_volume(args: argparse.Namespace) -> int:
    key, number = args.value
    _ensure_daemon(args)
    data = _send(args, {"cmd": "volume", key: number})
    if data is None:
        return 1
    _console.print(f"Volume {(data or {}).get('volume', '?')}")
    return 0


_STATE_LABELS = {
    "mute": ("muted", "Muted", "Unmuted", "Mute"),
    "loop": ("loop", "Loop on", "Loop off", "Loop"),
    "auto-next": ("auto_next", "Auto-next on", "Auto-next off", "Auto-next"),
}


def _cmd_state(args: argparse.Namespace) -> int:
    key, on_label, off_label, name = _STATE_LABELS[args.command]
    cmd = "auto_next" if args.command == "auto-next" else args.command
    _ensure_daemon(args)
    data = _send(args, {"cmd": cmd, "state": args.state})
    if data is None:
        return 1
    on: bool | None
    if args.state != "toggle":
        on = args.state == "on"
    elif isinstance(data, dict) and isinstance(data.get(key), bool):
        on = data[key]
    elif isinstance(data, bool):
        on = data
    else:
        on = None
    _console.print(
        (on_label if on else off_label) if on is not None else f"{name} toggled"
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _ensure_daemon(args)
    data = _send(args, {"cmd": "status"})
    if data is None:
        return 1
    if args.json:
        _print_json(data)
        return 0
    if data.get("state") == "stopped" or not data.get("track"):
        _console.print("Nothing is playing")
    else:
        line = _track_line(data)
        position = format_duration(data.get("position")) or "--:--"
        duration = format_duration(data.get("duration")) or "--:--"
        line.append(f"  {position} / {duration}", style="dim")
        _console.print(line)
    _console.print(_settings_line(data), style="dim")
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    _ensure_daemon(args)
    tracks = _send(args, {"cmd": "queue"})
    if tracks is None:
        return 1
    if args.json:
        _print_json(tracks)
        return 0
    _console.print(f"Up next — {len(tracks)} tracks")
    for index, track in enumerate(tracks, 1):
        line = Text()
        line.append(f"{index:>2} ", style="dim")
        line.append(track.get("title") or "Unknown", style="bold")
        artists = ", ".join(track.get("artists") or [])
        duration = format_duration(track.get("duration")) or "--:--"
        line.append(f" — {artists} ({duration})", style="dim")
        _console.print(line)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    client = build_client(args)
    results = client.search(" ".join(args.query), limit=args.limit, filter=args.filter)
    if args.json:
        for result in results:
            _print_json(
                {
                    "video_id": result.video_id,
                    "title": result.title,
                    "artists": result.artists,
                    "album": result.album,
                    "duration": result.duration,
                    "type": result.result_type,
                }
            )
        return 0
    if not results:
        _console.print("No results")
        return 0
    table = Table()
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Artists")
    table.add_column("Album")
    table.add_column("Time")
    table.add_column("Type")
    table.add_column("Video ID")
    for index, result in enumerate(results, 1):
        table.add_row(
            str(index),
            escape(result.title),
            escape(", ".join(result.artists)),
            escape(result.album),
            result.duration or "--:--",
            result.type_label,
            result.video_id,
        )
    _console.print(table)
    return 0


def _cmd_playlists(args: argparse.Namespace) -> int:
    # `downloaded` is a local (non-synced) list and needs no sign-in/library.
    if args.playlists_command == "downloaded":
        return _playlists_downloaded(args)
    client = build_client(args)
    library = client.library
    if not library.authenticated:
        _errors.print(
            "music-cli: not signed in — sign in first with 'music-cli login'",
            style="bold red",
        )
        return 1
    action = args.playlists_command
    if action == "list":
        return _playlists_list(args, library)
    if action == "tracks":
        return _playlists_tracks(args, library)
    if action == "play":
        return _playlists_play(args)
    if action == "create":
        playlist_id = library.create_playlist(args.name)
        _console.print(f"Created playlist “{escape(args.name)}” ({playlist_id})")
    elif action == "rename":
        library.rename_playlist(args.id, args.name)
        _console.print(f"Renamed playlist to “{escape(args.name)}”")
    elif action == "add":
        library.add_tracks(args.id, args.video_ids)
        _console.print(f"Added {len(args.video_ids)} track(s) to playlist {args.id}")
    elif action == "remove":
        library.remove_track(args.id, args.video_id)
        _console.print(f"Removed {args.video_id} from playlist {args.id}")
    return 0


def _playlists_list(args: argparse.Namespace, library: Any) -> int:
    playlists = library.playlists()
    if args.json:
        for playlist in playlists:
            _print_json(
                {
                    "playlist_id": playlist.playlist_id,
                    "title": playlist.title,
                    "track_count": playlist.track_count,
                }
            )
        return 0
    table = Table()
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Tracks", justify="right")
    table.add_column("ID")
    for index, playlist in enumerate(playlists, 1):
        table.add_row(
            str(index),
            escape(playlist.title),
            playlist.track_count,
            playlist.playlist_id,
        )
    _console.print(table)
    return 0


def _playlists_tracks(args: argparse.Namespace, library: Any) -> int:
    tracks = library.tracks(args.id)
    if args.json:
        for track in tracks:
            _print_json(
                {
                    "video_id": track.video_id,
                    "title": track.title,
                    "artists": track.artists,
                    "duration": track.duration,
                }
            )
        return 0
    table = Table()
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Artists")
    table.add_column("Time")
    table.add_column("Video ID")
    for index, track in enumerate(tracks, 1):
        table.add_row(
            str(index),
            escape(track.title),
            escape(", ".join(track.artists)),
            format_duration(track.duration) or "--:--",
            track.video_id,
        )
    _console.print(table)
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    """Download a track for offline listening, streaming progress.

    When ``video_id`` is omitted the currently playing track is used.
    Runs through the daemon (which owns the audio cache) so the progress UI
    and the Downloads list stay in sync with the TUI.
    """
    _ensure_daemon(args)
    video_id = args.video_id
    if video_id is None:
        status_data = _send(args, {"cmd": "status"})
        if status_data is None:
            return 1
        track = status_data.get("track")
        if track is None:
            _errors.print(
                "music-cli: nothing is playing — specify a track id to download",
                style="bold red",
            )
            return 1
        video_id = track["video_id"]
    try:
        with _console.status("Downloading…", spinner="dots") as status:
            response = ipc.send_play_request(
                {"cmd": "download", "video_id": video_id},
                on_progress=lambda pct: status.update(
                    "Downloading…" if pct is None else f"Downloading… {pct:.0f}%"
                ),
            )
    except PlayerError as error:
        _errors.print(f"music-cli: {error}", style="bold red")
        return 1
    if not response.get("ok"):
        _errors.print(
            f"music-cli: {response.get('error', 'unknown error')}", style="bold red"
        )
        return 1
    _console.print(
        f"Downloaded {video_id} for offline listening — "
        "find it under 'music-cli playlists downloaded'",
        style="green",
    )
    return 0


def _playlists_downloaded(args: argparse.Namespace) -> int:
    """List locally downloaded tracks (the non-synced Downloads list)."""
    from .storage.state import DownloadsStore

    store = DownloadsStore()
    try:
        tracks = store.recent()
    finally:
        store.close()
    if args.json:
        for track in tracks:
            _print_json(
                {
                    "video_id": track.video_id,
                    "title": track.title,
                    "artists": list(track.artists),
                    "duration": track.duration,
                }
            )
        return 0
    if not tracks:
        _console.print("No downloads yet — use 'music-cli download <id>'")
        return 0
    table = Table()
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Artists")
    table.add_column("Time")
    table.add_column("Video ID")
    for index, track in enumerate(tracks, 1):
        table.add_row(
            str(index),
            escape(track.title),
            escape(", ".join(track.artists)),
            format_duration(track.duration) or "--:--",
            track.video_id,
        )
    _console.print(table)
    return 0


def _playlists_play(args: argparse.Namespace) -> int:
    """Play a playlist from ``--track`` (1-based) via the daemon.

    Only the start index matters here; the daemon queues the rest of the
    playlist for auto-next, so playback continues in order.
    """
    _ensure_daemon(args)
    request: dict[str, Any] = {
        "cmd": "play",
        "playlist_id": args.id,
        "playlist_index": max(args.track - 1, 0),
    }
    with _console.status("Resolving stream…", spinner="dots"):
        data = _send(args, request, timeout=180.0)
    if data is None:
        return 1
    _console.print(_track_line(data))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    store = PlayHistoryStore()
    try:
        tracks = store.recent(args.limit)
    finally:
        store.close()
    if args.json:
        for track in tracks:
            _print_json(
                {
                    "video_id": track.video_id,
                    "title": track.title,
                    "artists": list(track.artists),
                    "duration": track.duration,
                    "played": track.played,
                }
            )
        return 0
    if not tracks:
        _console.print("No history")
        return 0
    table = Table()
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Artists")
    table.add_column("Time")
    table.add_column("Plays", justify="right")
    for index, track in enumerate(tracks, 1):
        table.add_row(
            str(index),
            escape(track.title),
            escape(", ".join(track.artists)),
            format_duration(track.duration) or "--:--",
            str(track.played),
        )
    _console.print(table)
    return 0


_DISPATCH = {
    "play": _cmd_play,
    "download": _cmd_download,
    "pause": _cmd_transport,
    "resume": _cmd_resume,
    "toggle": _cmd_transport,
    "next": _cmd_next,
    "stop": _cmd_stop,
    "seek": _cmd_seek,
    "volume": _cmd_volume,
    "mute": _cmd_state,
    "loop": _cmd_state,
    "auto-next": _cmd_state,
    "status": _cmd_status,
    "queue": _cmd_queue,
    "search": _cmd_search,
    "playlists": _cmd_playlists,
    "history": _cmd_history,
}
