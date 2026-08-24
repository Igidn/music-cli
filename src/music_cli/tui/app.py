"""Textual TUI orchestrator for music-cli: threading, state and playback control.

The TUI is a controller + view over the background playback daemon. All
transport and playback actions are sent over IPC (``ipc.send_request``) from
``@work(thread=True)`` workers so the UI never blocks; daemon push events on
the events socket drive all now-playing / progress / queue rendering. The
local ``MusicClient`` is kept only for stateless queries (search, library,
history, theme).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Footer, Input, Label, Select
from textual.worker import Worker, WorkerState

from music_cli import ipc
from music_cli.client import MusicClient
from music_cli.storage.state import (
    SETTING_THEME,
    PlayHistoryStore,
    SettingsStore,
)
from music_cli.yt.extract import PlaylistTrack
from music_cli.yt.lyrics import fetch_synced_lyrics
from music_cli.yt.playlists import LibraryPlaylist
from music_cli.yt.search import SearchFilter, SearchResult, format_duration

from .components import (
    AddToPlaylistRequested,
    FilterSelect,
    HistoryList,
    LibraryTree,
    NowPlaying,
    QueueList,
    ResultsTable,
    SearchInput,
    TopBar,
)
from .screens.modals import AddToPlaylistScreen, ConfirmScreen, PlaylistNameScreen

SEARCH_FILTERS: list[tuple[str, SearchFilter | None]] = [
    ("All results", None),
    ("Songs", "songs"),
    ("Videos", "videos"),
    ("Albums", "albums"),
    ("Artists", "artists"),
    ("Playlists", "playlists"),
]

# GitHub Dark palette, as a switchable Textual theme.
GITHUB_THEME = Theme(
    name="github-dark",
    primary="#58a6ff",
    secondary="#7ee787",
    accent="#58a6ff",
    foreground="#c9d1d9",
    background="#0d1117",
    surface="#161b22",
    warning="#d29922",
    error="#f85149",
    dark=True,
    variables={
        "border": "#30363d",
        "border-blurred": "#21262d",
        "text-muted": "#8b949e",
        "accent-lighten-1": "#79c0ff",
        "surface-lighten-1": "#1c2128",
        "scrollbar": "#30363d",
        "scrollbar-hover": "#484f58",
        "scrollbar-active": "#58a6ff",
        "scrollbar-background": "#161b22",
        "scrollbar-background-hover": "#1c2128",
        "scrollbar-background-active": "#1c2128",
        "scrollbar-corner-color": "#0d1117",
    },
)

# The app's original palette, as a switchable Textual theme (the default).
# Other themes come from Textual's built-ins via Ctrl+P → "Theme".
MUSIC_CLI_THEME = Theme(
    name="music-cli",
    primary="#a78bfa",
    secondary="#67e8f9",
    accent="#a78bfa",
    foreground="#e2e8f0",
    background="#0d1117",
    surface="#141a24",
    warning="#fbbf24",
    error="#f87171",
    dark=True,
    variables={
        "border": "#2a3242",
        "border-blurred": "#232b3a",
        "text-muted": "#8a93a6",
        "accent-lighten-1": "#c4b5fd",
        # boost is transparent in most themes; surface-lighten-1 is what
        # theme.tcss uses for raised surfaces.
        "surface-lighten-1": "#1c2432",
        "scrollbar": "#3a4358",
        "scrollbar-hover": "#5b6580",
        "scrollbar-active": "#a78bfa",
        "scrollbar-background": "#141a24",
        "scrollbar-background-hover": "#1c2432",
        "scrollbar-background-active": "#1c2432",
        "scrollbar-corner-color": "#0d1117",
    },
)

_EVENTS_RECONNECT_SECS = 2.0
_DEFAULT_SOCKET_TIMEOUT = 0.5
# Playback downloads finish before the daemon answers its play/next request, and
# the daemon serves requests single-threaded (frozen during the download). The
# per-request IPC timeout must wait out a slow download or the TUI would flash
# "Playback failed" while the daemon is still fetching. Tracks on a slow link
# take minutes, so stay generous.
_PLAY_RPC_TIMEOUT = 600.0
# After this long with a play still in flight, hint that it is a slow download.
_SLOW_DOWNLOAD_HINT_SECS = 15.0


class MusicTUI(App[None]):
    """The music-cli terminal user interface."""

    CSS_PATH = "theme.tcss"
    TITLE = "music-cli"

    # Below this width the side panes (playlists, up next) are hidden so the
    # search results and now-playing bar keep the full terminal. The two panes
    # take ~70 columns plus margins, so below this the results table would be
    # too cramped to read.
    NARROW_WIDTH = 130
    HORIZONTAL_BREAKPOINTS: ClassVar = [(0, "-narrow"), (NARROW_WIDTH, "-wide")]

    PANE_NAV: ClassVar[dict[str, dict[str, str]]] = {
        "search-input": {
            "up": "results",
            "down": "results",
            "left": "playlist-pane",
            "right": "queue-pane",
        },
        "filter-select": {
            "down": "results",
            "left": "search-input",
            "right": "queue-pane",
        },
        "results": {"left": "playlist-pane", "right": "queue-pane"},
        "queue-pane": {"left": "results"},
        "library-tree": {"right": "results", "down": "history-pane"},
        "history-pane": {"right": "results", "up": "library-tree"},
    }

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("slash", "focus_search", "Search"),
        Binding("space", "toggle_playback", "Play/Pause"),
        Binding("alt+right", "seek_forward", "Seek +5s", key_display="alt+→"),
        Binding("alt+left", "seek_back", "Seek -5s", key_display="alt+←"),
        Binding("n", "next_track", "Next"),
        Binding("a", "toggle_auto_next", "Auto next"),
        Binding("l", "toggle_loop", "Loop"),
        Binding("minus", "volume_down", "Vol -"),
        Binding("plus", "volume_up", "Vol +"),
        Binding("m", "toggle_mute", "Mute"),
        Binding("ctrl+d", "download_track", "Download track"),
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "focus_results", show=False),
        Binding("left", "pane_left", "Prev pane"),
        Binding("right", "pane_right", "Next pane"),
        Binding("up", "pane_up", "Pane up", show=False),
        Binding("down", "pane_down", "Pane down", show=False),
    ]

    def __init__(
        self,
        client: MusicClient | None = None,
        history_store: PlayHistoryStore | None = None,
        settings_store: SettingsStore | None = None,
        cookies: str | None = None,
    ) -> None:
        # Native ANSI colors let the TUI be truly transparent: surfaces styled
        # "ansi_default"/"transparent" emit no background escape, so the
        # terminal's own background/transparency compositor shows through.
        super().__init__(ansi_color=True)
        self.client = client or MusicClient()
        self._cookies = cookies
        self._history_store = history_store or PlayHistoryStore()
        self._settings_store = settings_store or SettingsStore()
        self._search_timer: Timer | None = None
        self._search_worker: Worker[list[SearchResult]] | None = None
        self._library_worker: Worker[list[LibraryPlaylist]] | None = None
        self._playlist_workers: dict[str, Worker[list[PlaylistTrack]]] = {}
        self._library_playlists: list[LibraryPlaylist] = []
        # Reflect-the-daemon state; the daemon owns the truth and pushes it.
        self._current_video_id: str | None = None
        self._pending_play: str | None = None
        self._play_worker: Worker[dict] | None = None
        self._next_pending = False
        self._next_worker: Worker[dict] | None = None
        # While a download is live the status line must keep showing it;
        # the state heartbeat's "Playing" label must not clobber it.
        self._download_status: str | None = None
        # Guard to ensure _download_status is cleared when a new track actually
        # starts playing (safety net against a stray "downloading 100%" event
        # where the matching "finished" never arrives).
        self._download_track_id: str | None = None
        self._auto_next = True
        self._loop_enabled = False
        self._volume = 80
        self._muted = False
        self._queue_video_ids: tuple[str, ...] = ()
        self._history_track_id: str | None = None
        self._lyrics_video_id: str | None = None
        # A raw socket for daemon push events; None until we've connected.
        self._events_socket: socket.socket | None = None
        self._events_thread: threading.Thread | None = None
        self._events_lock = threading.Lock()
        self.register_theme(GITHUB_THEME)
        self.register_theme(MUSIC_CLI_THEME)
        saved_theme = self._settings_store.get(SETTING_THEME)
        self.theme = (
            saved_theme
            if saved_theme in self.available_themes
            else MUSIC_CLI_THEME.name
        )

    def watch_theme(self, theme_name: str) -> None:
        """Persist the palette-chosen theme (Ctrl+P → Theme) across restarts."""
        self._settings_store.set(SETTING_THEME, theme_name)
        if self._is_mounted:
            now_playing = self.query_one(NowPlaying)
            now_playing._apply_theme()
            now_playing.refresh()

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()
        if self.client.library.authenticated:
            self._library_worker = self.library_worker()
        else:
            self.query_one(LibraryTree).set_unavailable(
                "Sign in to browse your playlists\n"
                "(run 'music-cli login' in a terminal, or pass --cookies)"
            )
        self._refresh_history()
        self._start_events_thread()
        self.init_status_worker()

    def _start_events_thread(self) -> None:
        """Subscribe to daemon push events on a background thread.

        The worker reconnects with a short backoff when the connection drops
        (daemon restart), and drains as it exits with the app.
        """
        with self._events_lock:
            if self._events_thread is not None and self._events_thread.is_alive():
                return
            self._events_thread = threading.Thread(
                target=self._events_worker, name="tui-events", daemon=True
            )
            self._events_thread.start()

    def _events_worker(self) -> None:
        while self.is_running:
            try:
                with self._open_events_socket() as conn:
                    ipc.send_message(conn, {"cmd": "subscribe"})
                    self._read_events(conn)
            except OSError:
                pass  # daemon not up / socket dropped; back off and retry
            finally:
                self._clear_events_socket()
            if self.is_running:
                time.sleep(_EVENTS_RECONNECT_SECS)

    def _open_events_socket(self) -> socket.socket:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(str(ipc.events_socket_path()))
        except OSError:
            conn.close()
            raise
        with self._events_lock:
            self._events_socket = conn
        return conn

    def _clear_events_socket(self) -> None:
        with self._events_lock:
            if self._events_socket is not None:
                try:
                    self._events_socket.close()
                except OSError:
                    pass
                self._events_socket = None

    def _read_events(self, conn: socket.socket) -> None:
        conn.settimeout(_DEFAULT_SOCKET_TIMEOUT)
        buffer = b""
        while self.is_running:
            try:
                data = conn.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            buffer += data
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "state":
                    status = event.get("status")
                    if status is not None:
                        self.call_from_thread(self._render_status, status)
                elif event.get("event") == "downloads":
                    self.call_from_thread(self._refresh_downloads)
                elif event.get("event") == "download":
                    if event.get("finished"):
                        self._download_status = None
                        continue
                    percent = event.get("percent")
                    downloaded = event.get("downloaded")
                    size = f" {_fmt_bytes(downloaded)}" if downloaded else ""
                    self._download_status = (
                        f"Downloading… {percent}%{size}"
                        if percent is not None
                        else f"Downloading…{size}"
                    )
                    self.call_from_thread(
                        self.set_status, self._download_status
                    )

    def on_unmount(self) -> None:
        self._clear_events_socket()
        if self._search_timer is not None:
            self._search_timer.stop()
        # Best-effort stop: quitting the TUI ends playback; the daemon stays
        # alive and idles out on its own. Never blocks on a dead daemon.
        try:
            ipc.send_request({"cmd": "stop"}, timeout=_EVENTS_RECONNECT_SECS)
        except Exception:  # noqa: BLE001, S110 — shutdown path must never raise
            pass
        self.client.close()
        self._history_store.close()
        self._settings_store.close()

    def compose(self) -> ComposeResult:
        yield TopBar()
        with Horizontal(id="body"):
            with Vertical(id="playlist-pane"):
                yield LibraryTree("Library", id="library-tree")
                yield HistoryList(id="history-pane")
            with Vertical(id="results-pane"):
                with Horizontal(id="search-box"):
                    yield SearchInput(
                        placeholder="Search songs, artists, albums…",
                        id="search-input",
                        select_on_focus=False,
                    )
                    yield FilterSelect(
                        [
                            (label, index)
                            for index, (label, _) in enumerate(SEARCH_FILTERS)
                        ],
                        prompt="Filter",
                        value=0,
                        compact=True,
                        id="filter-select",
                    )
                yield ResultsTable(
                    zebra_stripes=True,
                    cursor_type="row",
                    show_row_labels=False,
                    cell_padding=1,
                    id="results",
                )
            yield QueueList(id="queue-pane")
        yield NowPlaying(id="now-playing")
        yield Footer()

    @on(Input.Changed, "#search-input")
    def _on_query_changed(self, event: Input.Changed) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None
        query = event.value.strip()
        if not query:
            self._cancel_search()
            self.query_one(ResultsTable).set_results([])
            self.query_one(NowPlaying).set_status("Ready — type to search")
            return
        self._search_timer = self.set_timer(0.4, lambda: self._start_search(query))

    @on(Input.Submitted, "#search-input")
    def _on_query_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self._start_search(query)

    @on(Select.Changed, "#filter-select")
    def _on_filter_changed(self, event: Select.Changed) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if query:
            self._start_search(query)

    def _cancel_search(self) -> None:
        if self._search_worker is not None and self._search_worker.is_running:
            self._search_worker.cancel()
        self._search_worker = None

    def _start_search(self, query: str) -> None:
        self._cancel_search()
        self.set_status(f"Searching for “{query}”…")
        filter = SEARCH_FILTERS[self.query_one("#filter-select", Select).value][1]
        self._search_worker = self.run_search(query, filter)

    @work(thread=True, exit_on_error=False)
    def run_search(self, query: str, filter: SearchFilter | None) -> list[SearchResult]:
        return self.client.search(query, filter=filter)

    @on(Worker.StateChanged)
    def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if not worker.is_finished:
            return
        # A background worker (e.g. the lyric fetch) can finish after the app
        # has started tearing down; its handler must not touch the widgets then.
        if not self.is_running:
            return
        handlers = {
            "run_search": self._on_search_finished,
            "rpc_worker": self._on_rpc_finished,
            "play_rpc_worker": self._on_rpc_finished,
            "state_worker": self._on_state_finished,
            "download_worker": self._on_download_finished,
            "init_status_worker": self._on_init_status_finished,
            "lyric_worker": self._on_lyric_fetched,
            "library_worker": self._on_library_fetched,
            "playlist_tracks_worker": self._on_playlist_tracks_fetched,
            "playlist_mutation_worker": self._on_playlist_mutation_finished,
        }
        handler = handlers.get(worker.name)
        if handler is not None:
            handler(worker)

    def _on_search_finished(self, worker: Worker[list[SearchResult]]) -> None:
        if worker is not self._search_worker:
            return
        if worker.state is WorkerState.SUCCESS:
            results = worker.result or []
            self.query_one(ResultsTable).set_results(results)
            self.set_status(f"{len(results)} results" if results else "No results")
        else:
            self.set_status("Search failed")
            self.notify(
                f"Search failed: {worker.error}", title="Search", severity="error"
            )

    # ------------------------------------------------------------------
    # Daemon IPC: actions run off the UI thread and render from responses.
    # ------------------------------------------------------------------

    @work(thread=True, exit_on_error=False)
    def rpc_worker(self, request: object, timeout: float = 30.0) -> dict:
        return ipc.send_request(request, timeout=timeout)

    @work(thread=True, exit_on_error=False)
    def play_rpc_worker(self, request: dict, timeout: float = 1200.0) -> dict:
        return ipc.send_play_request(request, timeout=timeout)

    @work(thread=True, exit_on_error=False)
    def state_worker(self, request: object) -> dict:
        return ipc.send_request(request)

    @work(thread=True, exit_on_error=False)
    def download_worker(self, request: dict) -> dict:
        return ipc.send_play_request(request)

    @work(thread=True, exit_on_error=False)
    def lyric_worker(
        self,
        video_id: str,
        title: str,
        artists: tuple[str, ...],
        duration: float | None,
    ) -> list[tuple[float, str]]:
        return fetch_synced_lyrics(title, artists, duration or None)

    @work(thread=True, exit_on_error=False)
    def init_status_worker(self) -> dict:
        try:
            ipc.ensure_daemon(self._cookies)
        except Exception:  # noqa: BLE001, S110 — no daemon yet; status reports it
            pass
        try:
            return ipc.send_request({"cmd": "status"})
        except Exception as error:  # noqa: BLE001 — surface as a status line
            return {"ok": False, "error": str(error)}

    def _on_init_status_finished(self, worker: Worker[dict]) -> None:
        if worker.state is WorkerState.SUCCESS:
            response = worker.result or {}
            if response.get("ok"):
                status = response["data"]
                self._render_status(status)
                return
            self.set_status(f"Daemon unavailable: {response.get('error')}")
        else:
            self.set_status("Daemon unavailable")

    def _on_lyric_fetched(
        self, worker: Worker[list[tuple[float, str]]]
    ) -> None:
        if worker.state is WorkerState.SUCCESS:
            self.query_one(NowPlaying).set_lyrics(worker.result or [])

    def _show_last_track_paused(self) -> None:
        """Show the most recently played track, without playing it."""
        track = self._history_store.most_recent()
        if track is not None:
            self.query_one(NowPlaying).set_track(
                track.title, " • ".join(track.artists), track.duration
            )
            self.query_one(NowPlaying).set_paused(True)
        else:
            self.query_one(NowPlaying).clear_track()

    def _on_rpc_finished(self, worker: Worker[dict]) -> None:
        # Only the play request itself clears the play guard: an unrelated RPC
        # (toggle, seek, …) settling mid-download must not, or the stale track's
        # heartbeat would slip back into the bar and transport actions would
        # hit the track the pending play is replacing.
        if worker is self._play_worker:
            self._play_worker = None
            self._pending_play = None
        if worker is self._next_worker:
            self._next_worker = None
            self._next_pending = False
        if worker.state is WorkerState.SUCCESS:
            response = worker.result or {}
            if response.get("ok"):
                self._render_status(response.get("data"))
            else:
                err = response.get("error") or "unknown error"
                self.set_status("Playback failed")
                self.notify(
                    f"Playback failed: {err}",
                    title="Playback",
                    severity="error",
                )
        else:
            self.set_status("Playback failed")

    def _on_state_finished(self, worker: Worker[dict]) -> None:
        if worker.state is not WorkerState.SUCCESS:
            self.notify(
                f"Settings update failed: {worker.error}",
                title="Settings",
                severity="warning",
            )
            return
        response = worker.result or {}
        if not response.get("ok"):
            self.notify(
                f"Settings update failed: {response.get('error')}",
                title="Settings",
                severity="warning",
            )
            return
        data = response.get("data") or {}
        now_playing = self.query_one(NowPlaying)
        if "volume" in data:
            self._volume = int(data["volume"])
            now_playing.set_volume(self._volume, self._muted)
        if "mute" in data:
            self._muted = bool(data["mute"])
            now_playing.set_volume(self._volume, self._muted)
        if "auto_next" in data:
            self._auto_next = bool(data["auto_next"])
            now_playing.set_modes(self._auto_next, self._loop_enabled)
        if "loop" in data:
            self._loop_enabled = bool(data["loop"])
            now_playing.set_modes(self._auto_next, self._loop_enabled)

    def _on_download_finished(self, worker: Worker[dict]) -> None:
        """A download request settled: refresh the list and clear progress."""
        self._download_status = None
        if worker.state is not WorkerState.SUCCESS:
            self.set_status("Download failed")
            self.notify(
                f"Download failed: {worker.error}",
                title="Download",
                severity="error",
            )
            return
        response = worker.result or {}
        if not response.get("ok"):
            self.set_status("Download failed")
            self.notify(
                f"Download failed: {response.get('error')}",
                title="Download",
                severity="error",
            )
            return
        self._refresh_downloads()
        self.set_status("Download ready")

    # ------------------------------------------------------------------
    # Event-driven rendering from pushed daemon status.
    # ------------------------------------------------------------------

    def _render_status(self, status: dict) -> None:
        now_playing = self.query_one(NowPlaying)
        track = status.get("track")
        # If a download progress event got stuck (e.g. a "downloading 100%"
        # without a matching "finished"), clear it when a new track starts
        # playing so the status bar shows the correct state label.
        if track is not None and self._download_track_id != track.get("video_id"):
            self._download_status = None
        if track is not None:
            self._download_track_id = track.get("video_id")
        # While a play request is in flight (_pending_play), a per-track push can
        # arrive for a different track than the one we asked for — the up-next
        # head auto-advancing, or a stale heartbeat of the previous track. Show
        # the requested target, not those transient frames.
        holds_target = not (
            track is not None
            and self._pending_play
            and track["video_id"] != self._pending_play
        )
        if holds_target:
            self._current_video_id = track["video_id"] if track else None
            if track is None:
                self._show_last_track_paused()
            else:
                now_playing.set_track(
                    track["title"],
                    ", ".join(track.get("artists") or []) or "Unknown artist",
                    track.get("duration"),
                )
                if track["video_id"] != self._history_track_id:
                    self._history_track_id = track["video_id"]
                    self._refresh_history()
                if track["video_id"] != self._lyrics_video_id:
                    self._lyrics_video_id = track["video_id"]
                    self.lyric_worker(
                        track["video_id"],
                        track["title"],
                        tuple(track.get("artists") or []),
                        track.get("duration"),
                    )
        state = status.get("state")
        # While a play/next is resolving, keep the "Resolving stream…" status:
        # the pushed frames describe the stale track, not the pending one.
        if (
            self._download_status is None
            and self._pending_play is None
            and not self._next_pending
        ):
            if state == "stopped" or track is None:
                self.set_status("Ready")
            else:
                self.set_status("Playing" if state == "playing" else "Paused")
        now_playing.set_progress(
            status.get("position", 0.0), status.get("duration") or 0.0
        )
        if track is not None:
            now_playing.set_paused(state == "paused")
        self._volume = int(status.get("volume", self._volume))
        self._muted = bool(status.get("muted", self._muted))
        now_playing.set_volume(self._volume, self._muted)
        self._auto_next = bool(status.get("auto_next", self._auto_next))
        self._loop_enabled = bool(status.get("loop", self._loop_enabled))
        now_playing.set_modes(self._auto_next, self._loop_enabled)
        self._refresh_queue_from_status(status.get("queue") or [])

    def _refresh_queue_from_status(self, queue: list[dict]) -> None:
        ids = tuple(entry.get("video_id") for entry in queue)
        if ids == self._queue_video_ids:
            return
        self._queue_video_ids = ids
        tracks = [self._queue_track(entry) for entry in queue]
        self.query_one(QueueList).set_tracks(tracks)
        self.query_one("#queue-count", Label).update(
            f"{len(tracks)} up next" if tracks else ""
        )

    @staticmethod
    def _queue_track(entry: dict) -> PlaylistTrack:
        return PlaylistTrack(
            video_id=entry.get("video_id", ""),
            title=entry.get("title", "Unknown track"),
            artists=list(entry.get("artists") or []),
            duration=format_duration(entry.get("duration")),
        )

    # ------------------------------------------------------------------
    # Query side (unchanged): search results, library, history, settings.
    # ------------------------------------------------------------------

    @on(ResultsTable.RowSelected)
    def _on_result_selected(self, event: ResultsTable.RowSelected) -> None:
        result = self.query_one(ResultsTable).selected_result()
        if result is not None:
            self.play_result(result)

    def play_result(self, result: SearchResult) -> None:
        if result.video_id:
            self._start_play(
                result.video_id,
                result.title,
                result.subtitle,
                {"cmd": "play", "video_id": result.video_id, "title": result.title},
            )
        elif result.result_type == "album" and result.browse_id:
            # Album rows carry a browse id, not a video id: the daemon
            # fetches the album's track list and queues the remainder.
            self._start_play(
                result.browse_id,
                result.title,
                result.subtitle,
                {"cmd": "play", "album_id": result.browse_id},
            )
        elif result.result_type == "playlist" and result.browse_id:
            self._start_play(
                result.browse_id,
                result.title,
                result.subtitle,
                {"cmd": "play", "playlist_id": result.browse_id},
            )
        else:
            self.notify(
                f"{result.type_label} results can't be played directly",
                title="Playback",
                severity="warning",
            )

    def play_video(self, video_id: str, title: str, subtitle: str = "") -> None:
        """Play a bare video id (used to resume the last played track)."""
        self._start_play(
            video_id,
            title,
            subtitle,
            {"cmd": "play", "video_id": video_id, "title": title},
        )

    # QueueList.Selected and HistoryList.Selected are the same inherited
    # ListView.Selected class: scope each handler by selector or a selection
    # in one list would fire a play for the same row in the other.
    @on(QueueList.Selected, "#queue-pane")
    def _on_queue_selected(self, event: QueueList.Selected) -> None:
        track = self.query_one(QueueList).track_at(event.index)
        if track is None:
            return
        self._start_play(
            track.video_id,
            track.title,
            " • ".join(track.artists),
            {"cmd": "play", "queue_index": event.index},
        )

    @on(HistoryList.Selected, "#history-pane")
    def _on_history_selected(self, event: HistoryList.Selected) -> None:
        track = self.query_one(HistoryList).track_at(event.index)
        if track is None:
            return
        self._start_play(
            track.video_id,
            track.title,
            " • ".join(track.artists),
            {"cmd": "play", "video_id": track.video_id, "title": track.title},
        )

    @on(LibraryTree.TrackActivated)
    def _on_playlist_track_activated(self, message: LibraryTree.TrackActivated) -> None:
        track = message.track
        self._start_play(
            track.video_id,
            track.title,
            " • ".join(track.artists),
            {
                "cmd": "play",
                "playlist_id": message.playlist_id,
                "playlist_index": message.index,
            },
        )

    @on(LibraryTree.DownloadsExpandRequested)
    def _on_downloads_expand(self, message: LibraryTree.DownloadsExpandRequested) -> None:
        self.query_one(LibraryTree).show_downloads(self.client.downloads.recent())

    @on(LibraryTree.DownloadActivated)
    def _on_download_activated(self, message: LibraryTree.DownloadActivated) -> None:
        track = message.track
        self._start_play(
            track.video_id,
            track.title,
            " • ".join(track.artists),
            {
                "cmd": "play",
                "video_id": track.video_id,
                "title": track.title,
                "from_downloads": True,
            },
        )

    @on(LibraryTree.DownloadRemoveRequested)
    def _on_download_remove(self, message: LibraryTree.DownloadRemoveRequested) -> None:
        track = message.track
        self.push_screen(
            ConfirmScreen(f"Remove “{track.title}” from your downloads?"),
            lambda confirmed: self._remove_download(
                track.video_id, track.title, confirmed
            ),
        )

    def _remove_download(self, video_id: str, title: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.rpc_worker({"cmd": "remove_download", "video_id": video_id})
        self.set_status(f"Removed “{title}” from downloads")

    def _start_play(
        self, video_id: str, title: str, subtitle: str, request: dict
    ) -> None:
        """Send one play request, skipping duplicates already on or in flight."""
        if video_id == self._current_video_id or video_id == self._pending_play:
            return
        self._pending_play = video_id
        self.query_one(NowPlaying).set_track(title, subtitle)
        self.set_status("Resolving stream…")
        # A download on a slow connection can take minutes; match the IPC timeout
        # to it and hint the cause, so it reads as a slow fetch rather than a
        # hang that "Playback failed" would mislabel.
        self.set_timer(
            _SLOW_DOWNLOAD_HINT_SECS, lambda: self._hint_slow_download(video_id)
        )
        self._play_worker = self.play_rpc_worker(request, timeout=_PLAY_RPC_TIMEOUT)

    def _hint_slow_download(self, video_id: str) -> None:
        """Nudge the status line when a play is still downloading after a while."""
        if self._pending_play == video_id:
            self.set_status(
                "Downloading… may take a while on a slow connection"
            )

    def _refresh_history(self) -> None:
        self.query_one(HistoryList).set_tracks(self._history_store.recent(15))

    def _refresh_downloads(self) -> None:
        """Refresh the Downloads pseudo-playlist node from the local index."""
        tree = self.query_one(LibraryTree)
        tracks = self.client.downloads.recent()
        if tree.downloads_loaded:
            tree.show_downloads(tracks)
        else:
            tree.update_downloads_count(len(tracks))
        self._downloads_loaded = tree.downloads_loaded

    @work(thread=True, exit_on_error=False)
    def library_worker(self) -> list[LibraryPlaylist]:
        return self.client.library.playlists()

    def _on_library_fetched(self, worker: Worker[list[LibraryPlaylist]]) -> None:
        if worker is not self._library_worker:
            return
        if worker.state is WorkerState.SUCCESS:
            self._library_playlists = worker.result or []
            self.query_one(LibraryTree).set_playlists(self._library_playlists)
        else:
            self.query_one(LibraryTree).set_unavailable(
                "Couldn't load library playlists\n"
                "(run 'music-cli login' if your sign-in expired)"
            )
            self.notify(
                f"Could not load library playlists: {worker.error}",
                title="Playlists",
                severity="warning",
            )

    @on(LibraryTree.PlaylistExpandRequested)
    def _on_playlist_expand_requested(
        self, message: LibraryTree.PlaylistExpandRequested
    ) -> None:
        """Expand a playlist node and lazily fetch its tracks."""
        message.node.remove_children()
        message.node.add_leaf("Loading…")
        message.node.expand()
        self._playlist_workers[message.playlist_id] = self.playlist_tracks_worker(
            message.playlist_id
        )

    @work(thread=True, exit_on_error=False)
    def playlist_tracks_worker(self, playlist_id: str) -> list[PlaylistTrack]:
        return self.client.library.tracks(playlist_id)

    def _on_playlist_tracks_fetched(self, worker: Worker[list[PlaylistTrack]]) -> None:
        for playlist_id, pending in self._playlist_workers.items():
            if pending is worker:
                del self._playlist_workers[playlist_id]
                break
        else:
            return
        if worker.state is WorkerState.SUCCESS:
            self.query_one(LibraryTree).show_tracks(playlist_id, worker.result or [])
        else:
            self.query_one(LibraryTree).fail_playlist(playlist_id)
            self.notify(
                f"Could not load playlist: {worker.error}",
                title="Playlists",
                severity="warning",
            )

    @on(AddToPlaylistRequested)
    def _on_add_to_playlist_requested(self, message: AddToPlaylistRequested) -> None:
        self.push_screen(
            AddToPlaylistScreen(self._library_playlists, message.title),
            lambda selected: self._add_to_playlists(message.video_id, selected),
        )

    def _add_to_playlists(self, video_id: str, selected: set[str] | None) -> None:
        if not selected:
            return
        self.playlist_mutation_worker(lambda: self._do_add(video_id, set(selected)))

    def _do_add(self, video_id: str, playlist_ids: set[str]) -> str:
        for playlist_id in playlist_ids:
            self.client.library.add_tracks(playlist_id, [video_id])
        count = len(playlist_ids)
        return f"Added to {count} playlist{'s' if count > 1 else ''}"

    @on(LibraryTree.TrackRemoveRequested)
    def _on_track_remove_requested(
        self, message: LibraryTree.TrackRemoveRequested
    ) -> None:
        track = message.track
        self.push_screen(
            ConfirmScreen(f"Remove “{track.title}” from this playlist?"),
            lambda confirmed: self._remove_track(
                message.playlist_id, track.video_id, track.title, confirmed
            ),
        )

    def _remove_track(
        self, playlist_id: str, video_id: str, title: str, confirmed: bool | None
    ) -> None:
        if not confirmed:
            return
        self.playlist_mutation_worker(
            lambda: self._do_remove(playlist_id, video_id, title)
        )

    def _do_remove(self, playlist_id: str, video_id: str, title: str) -> str:
        self.client.library.remove_track(playlist_id, video_id)
        return f"Removed “{title}” from playlist"

    @on(LibraryTree.CreatePlaylistRequested)
    def _on_create_playlist_requested(
        self, message: LibraryTree.CreatePlaylistRequested
    ) -> None:
        self.push_screen(PlaylistNameScreen("Create playlist"), self._create_playlist)

    def _create_playlist(self, name: str | None) -> None:
        if not name:
            return
        self.playlist_mutation_worker(lambda: self._do_create(name))

    def _do_create(self, name: str) -> str:
        self.client.library.create_playlist(name)
        return f"Created playlist “{name}”"

    @on(LibraryTree.RenamePlaylistRequested)
    def _on_rename_playlist_requested(
        self, message: LibraryTree.RenamePlaylistRequested
    ) -> None:
        self.push_screen(
            PlaylistNameScreen("Rename playlist", initial=message.title),
            lambda name: self._rename_playlist(
                message.playlist_id, message.title, name
            ),
        )

    def _rename_playlist(
        self, playlist_id: str, old_title: str, name: str | None
    ) -> None:
        if not name or name == old_title:
            return
        self.playlist_mutation_worker(lambda: self._do_rename(playlist_id, name))

    def _do_rename(self, playlist_id: str, name: str) -> str:
        self.client.library.rename_playlist(playlist_id, name)
        return f"Renamed playlist to “{name}”"

    @work(thread=True, exit_on_error=False)
    def playlist_mutation_worker(self, mutate: Callable[[], str]) -> str:
        """Run one playlist mutation off the UI thread and return a status line."""
        return mutate()

    def _on_playlist_mutation_finished(self, worker: Worker[str]) -> None:
        if worker.state is WorkerState.SUCCESS:
            self._refresh_library()
            status = worker.result or "Done"
            self.set_status(status)
            self.notify(status, title="Playlists")
        else:
            self.notify(
                f"Playlist update failed: {worker.error}",
                title="Playlists",
                severity="error",
            )

    def _refresh_library(self) -> None:
        """Reload library playlists after a mutation.

        ponytail: full tree rebuild collapses open playlists; patch the
        affected node in place if that ever annoys anyone.
        """
        if self._library_worker is not None and self._library_worker.is_running:
            self._library_worker.cancel()
        self._library_worker = self.library_worker()

    # ------------------------------------------------------------------
    # Transport / settings actions → IPC workers.
    # ------------------------------------------------------------------

    def action_next_track(self) -> None:
        """Advance to the next track; drop a second press while one is in flight.

        The daemon also advances on track-end, so a queued double skip would
        otherwise fire twice; the guard collapses a double press into one.
        """
        if self._next_pending:
            return
        self._next_pending = True
        self.set_status("Resolving stream…")
        self._next_worker = self.rpc_worker(
            {"cmd": "next"}, timeout=_PLAY_RPC_TIMEOUT
        )

    def action_toggle_auto_next(self) -> None:
        self.state_worker({"cmd": "auto_next", "state": "toggle"})

    def action_toggle_loop(self) -> None:
        self.state_worker({"cmd": "loop", "state": "toggle"})

    def action_toggle_playback(self) -> None:
        # While a track is resolving, the daemon still has the previous track
        # loaded; a toggle would play/pause that stale track, then the resolved
        # one would start on top. Drop the press until the play lands.
        if self._pending_play or self._next_pending:
            return
        self.rpc_worker({"cmd": "toggle"})

    def action_download_track(self) -> None:
        """Download the track currently selected in the focused pane."""
        track = self._selected_track()
        if track is None:
            self.notify(
                "Select a track to download", title="Download", severity="warning"
            )
            return
        video_id, title = track[0], track[1]
        self.set_status(f"Downloading “{title}”…")
        self.download_worker({"cmd": "download", "video_id": video_id})

    def _selected_track(self) -> tuple[str, str, str] | None:
        """(video_id, title, subtitle) for the selected row in the focused pane."""
        focused = self.focused
        if focused is None:
            return None
        if isinstance(focused, LibraryTree):
            return focused.selected_track()
        if isinstance(focused, (QueueList, HistoryList)):
            track = focused.track_at(getattr(focused, "index", None))
            if track is None:
                return None
            return track.video_id, track.title, " • ".join(track.artists)
        if isinstance(focused, ResultsTable):
            result = focused.selected_result()
            if result is not None and result.video_id:
                return result.video_id, result.title, result.subtitle
        return None

    def action_seek_forward(self) -> None:
        self._seek(5)

    def action_seek_back(self) -> None:
        self._seek(-5)

    def _seek(self, delta: int) -> None:
        self.rpc_worker({"cmd": "seek", "offset": delta})

    def action_volume_up(self) -> None:
        self._change_volume(5)

    def action_volume_down(self) -> None:
        self._change_volume(-5)

    def _change_volume(self, delta: int) -> None:
        self.state_worker({"cmd": "volume", "delta": delta})

    def action_toggle_mute(self) -> None:
        self.state_worker({"cmd": "mute", "state": "toggle"})

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_focus_results(self) -> None:
        self.query_one(ResultsTable).focus()

    def action_pane_left(self) -> None:
        self._move_pane("left")

    def action_pane_right(self) -> None:
        self._move_pane("right")

    def action_pane_up(self) -> None:
        self._move_pane("up")

    def action_pane_down(self) -> None:
        self._move_pane("down")

    def _move_pane(self, direction: str) -> None:
        focused = self.focused
        if focused is None:
            return
        target = self.PANE_NAV.get(focused.id, {}).get(direction)
        if target is None:
            return
        widget = self.query_one(f"#{target}")
        if not widget.display:
            return
        # Panes may be containers (e.g. #playlist-pane); sink to their first
        # focusable child.
        child = next((c for c in widget.walk_children(Widget) if c.focusable), widget)
        child.focus()
        if direction == "up" and isinstance(child, ResultsTable) and child.row_count:
            child.move_cursor(row=child.row_count - 1)

    def set_status(self, text: str) -> None:
        self.query_one(NowPlaying).set_status(text)


def _fmt_bytes(n: int) -> str:
    """Pretty-print a byte count ("900 B", "3.4 MB")."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if unit == "B":
            return f"{int(size)} B" if size >= 1 else ""
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
