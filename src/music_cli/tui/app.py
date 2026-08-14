"""Textual TUI orchestrator for music-cli: threading, state and playback control."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Footer, Input, Label, Select
from textual.worker import Worker, WorkerState

from music_cli.client import MusicClient
from music_cli.storage.state import (
    SETTING_AUTO_NEXT,
    SETTING_LOOP,
    SETTING_MUTED,
    SETTING_VOLUME,
    PlayedTrack,
    PlayHistoryStore,
    SettingsStore,
)
from music_cli.yt.extract import PlaylistTrack, StreamInfo
from music_cli.yt.playlists import LibraryPlaylist
from music_cli.yt.search import SearchFilter, SearchResult

from .components import (
    AddToPlaylistRequested,
    FilterSelect,
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


class TrackEnded(Message):
    """Posted on the app thread when the player reports end-of-file."""


@dataclass
class _PlayRequest:
    """One play attempt: video id (for dedup), the thread-side stream
    resolver, and how to finish the attempt back on the app thread."""

    video_id: str
    resolve: Callable[[], StreamInfo]
    kind: Literal["result", "queue", "playlist"]
    on_fail: Callable[[], None] = lambda: None


class MusicTUI(App[None]):
    """The music-cli terminal user interface."""

    CSS_PATH = "theme.tcss"
    TITLE = "music-cli"

    # Below this width the side panes (playlists, up next) are hidden so the
    # search results and now-playing bar keep the full terminal.
    NARROW_WIDTH = 100
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
        "playlist-pane": {"right": "results"},
    }

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("slash", "focus_search", "Search"),
        Binding("space", "toggle_playback", "Play/Pause"),
        Binding("ctrl+right", "seek_forward", "Seek +5s"),
        Binding("ctrl+left", "seek_back", "Seek -5s"),
        Binding("n", "next_track", "Next"),
        Binding("a", "toggle_auto_next", "Auto next"),
        Binding("l", "toggle_loop", "Loop"),
        Binding("minus", "volume_down", "Vol -"),
        Binding("plus", "volume_up", "Vol +"),
        Binding("m", "toggle_mute", "Mute"),
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
    ) -> None:
        super().__init__()
        self.client = client or MusicClient()
        self._history_store = history_store or PlayHistoryStore()
        self._settings_store = settings_store or SettingsStore()
        self.client.on_track_end = self._on_player_eof
        self._search_timer: Timer | None = None
        self._search_worker: Worker[list[SearchResult]] | None = None
        self._queue_worker: Worker[list[PlaylistTrack]] | None = None
        self._play_worker: Worker[StreamInfo] | None = None
        self._play_request: _PlayRequest | None = None
        self._library_worker: Worker[list[LibraryPlaylist]] | None = None
        self._playlist_workers: dict[str, Worker[list[PlaylistTrack]]] = {}
        self._library_playlists: list[LibraryPlaylist] = []
        self._last_video_id: str = ""
        self._auto_next = True
        self._loop_enabled = False

    def on_mount(self) -> None:
        self.set_interval(0.5, self._tick)
        self.query_one("#search-input", Input).focus()
        self.run_worker(self.pump_platform(), exclusive=False)
        if self.client.library.authenticated:
            self._library_worker = self.library_worker()
        else:
            self.query_one(LibraryTree).set_unavailable(
                "Sign in to browse your playlists\n"
                "(run 'music-cli login' in a terminal, or pass --cookies)"
            )
        self._apply_saved_settings()
        self._resume_last_track()

    def _apply_saved_settings(self) -> None:
        """Restore persisted player preferences; saved settings always win."""
        player = self.client.player
        player.volume = self._settings_store.get_int(SETTING_VOLUME, player.volume)
        player.muted = self._settings_store.get_bool(SETTING_MUTED)
        self._loop_enabled = self._settings_store.get_bool(SETTING_LOOP)
        self.client.loop = self._loop_enabled
        self._auto_next = self._settings_store.get_bool(SETTING_AUTO_NEXT, True)

    def _resume_last_track(self) -> None:
        """Auto-play the most recently played track from history, if any."""
        track = self._history_store.most_recent()
        if track is not None:
            self.play_video(track.video_id, track.title, " • ".join(track.artists))

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
        self.client.close()
        self._history_store.close()
        self._settings_store.close()

    def compose(self) -> ComposeResult:
        yield TopBar()
        with Horizontal(id="body"):
            yield LibraryTree("Library", id="playlist-pane")
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
        handlers = {
            "run_search": self._on_search_finished,
            "play_worker": self._on_play_finished,
            "fetch_queue_worker": self._on_queue_fetched,
            "refill_worker": self._on_refill_finished,
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

    @on(ResultsTable.RowSelected)
    def _on_result_selected(self, event: ResultsTable.RowSelected) -> None:
        result = self.query_one(ResultsTable).selected_result()
        if result is not None:
            self.play_result(result)

    def _play_pending(self, video_id: str) -> bool:
        """Whether an identical playback request is already underway.

        Covers both a duplicate still being resolved and the same song already
        on, so a double click on a row never spawns a second stream
        resolution for the same video (which would risk YouTube throttling).
        """
        if self.client.current is not None and self.client.current.video_id == video_id:
            return True
        request = self._play_request
        return (
            request is not None
            and request.video_id == video_id
            and self._play_worker is not None
            and self._play_worker.state in (WorkerState.PENDING, WorkerState.RUNNING)
        )

    def play_result(self, result: SearchResult) -> None:
        self._start_play(
            _PlayRequest(
                result.video_id, lambda: self.client.play_result(result), "result"
            ),
            result.title,
            result.subtitle,
        )

    def play_video(self, video_id: str, title: str, subtitle: str = "") -> None:
        """Play a bare video id (used to resume the last played track)."""
        self._start_play(
            _PlayRequest(
                video_id, lambda: self.client.play_video(video_id, title), "result"
            ),
            title,
            subtitle,
        )

    def _start_play(self, request: _PlayRequest, title: str, subtitle: str) -> None:
        """Resolve and play ``request``, skipping duplicates of what's on."""
        if self._play_pending(request.video_id):
            return
        self._last_video_id = request.video_id
        self._cancel_queue_fetch()
        self.query_one(NowPlaying).set_track(title, subtitle)
        self.set_status("Resolving stream…")
        self._play_request = request
        self._play_worker = self.play_worker(request)

    @work(thread=True, exit_on_error=False)
    def play_worker(self, request: _PlayRequest) -> StreamInfo:
        return request.resolve()

    def _on_play_finished(self, worker: Worker[StreamInfo]) -> None:
        request = self._play_request
        if worker is not self._play_worker or request is None:
            return
        if worker.state is WorkerState.SUCCESS:
            stream = worker.result
            self._record_play(stream)
            self.query_one(NowPlaying).set_track(
                stream.title,
                ", ".join(stream.artists) or "Unknown artist",
                stream.duration,
            )
            if request.kind == "result":
                self.set_status("Loading up-next queue…")
                self._queue_worker = self.fetch_queue_worker(stream.video_id)
            else:
                self._refresh_queue()
                self._prefetch_next()
                self.set_status(f"Playing from {request.kind}")
        else:
            request.on_fail()
            self.set_status("Playback failed")
            self.notify(
                f"Could not play: {worker.error}", title="Playback", severity="error"
            )

    def _cancel_queue_fetch(self) -> None:
        if self._queue_worker is not None and self._queue_worker.is_running:
            self._queue_worker.cancel()
        self._queue_worker = None

    @work(thread=True, exit_on_error=False)
    def fetch_queue_worker(self, video_id: str) -> list[PlaylistTrack]:
        return self.client.load_queue(video_id, radio=False)

    def _on_queue_fetched(self, worker: Worker[list[PlaylistTrack]]) -> None:
        if worker.state is WorkerState.SUCCESS:
            self._refresh_queue()
            self._prefetch_next()
            self.set_status(
                "Playing" if self.client.queue else "Playing — no up next available"
            )
        else:
            self.set_status("Playing — queue unavailable")
            self.notify(
                f"Could not load the queue: {worker.error}",
                title="Queue",
                severity="warning",
            )

    def _prefetch_next(self) -> None:
        """Background-download the next queued track so the transition is instant."""
        if self.client.queue:
            self.prefetch_worker(self.client.queue[0].video_id)

    @work(thread=True, exit_on_error=False)
    def prefetch_worker(self, video_id: str) -> None:
        self.client.prefetch(video_id)

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

    @on(LibraryTree.TrackActivated)
    def _on_playlist_track_activated(self, message: LibraryTree.TrackActivated) -> None:
        track = message.track
        self._start_play(
            _PlayRequest(
                track.video_id,
                lambda: self.client.play_from_playlist(
                    message.playlist_id, message.index
                ),
                "playlist",
                on_fail=lambda: self._restore_queue(0, track),
            ),
            track.title,
            " • ".join(track.artists),
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

    @on(QueueList.Selected)
    def _on_queue_selected(self, event: QueueList.Selected) -> None:
        self._play_queued_track(self.query_one(QueueList).track_at(event.index))

    def _play_queued_track(self, track: PlaylistTrack | None) -> None:
        """Play ``track`` from the queue, ignoring duplicates of the current request.

        The track is looked up by video id rather than by the event index so a
        double click on one row cannot pop a *different* track that shifted
        into the same index after the first pop.
        """
        if track is None or self._play_pending(track.video_id):
            return
        for index, queued in enumerate(self.client.queue):
            if queued.video_id == track.video_id:
                del self.client.queue[index]
                break
        else:
            return
        self._start_play(
            _PlayRequest(
                track.video_id,
                lambda: self.client.play_track(track),
                "queue",
                on_fail=lambda: self._restore_queue(index, track),
            ),
            track.title,
            " • ".join(track.artists),
        )

    def _record_play(self, stream: StreamInfo) -> None:
        """Persist the resolved track into play history."""
        self._history_store.record(
            PlayedTrack(
                video_id=stream.video_id,
                title=stream.title,
                artists=tuple(stream.artists),
                duration=stream.duration,
            )
        )

    def _restore_queue(self, index: int, track: PlaylistTrack) -> None:
        """Put ``track`` back in the queue after a failed play."""
        self.client.queue.insert(index, track)
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        self.query_one(QueueList).set_tracks(self.client.queue)
        self.query_one("#queue-count", Label).update(
            f"{len(self.client.queue)} up next"
        )

    def action_next_track(self) -> None:
        if self.client.queue:
            self._play_queued_track(self.client.queue[0])
        elif self._last_video_id:
            self.set_status("Refilling station…")
            self.refill_worker(self._last_video_id)

    @work(thread=True, exit_on_error=False)
    def refill_worker(self, video_id: str) -> None:
        self.client.load_queue(video_id, radio=True)

    def _on_refill_finished(self, worker: Worker[None]) -> None:
        if worker.state is WorkerState.SUCCESS:
            if self.client.queue:
                self._play_queued_track(self.client.queue[0])
                self._prefetch_next()
            else:
                self._refresh_queue()
                self.set_status("End of station")
        else:
            self.set_status("Station unavailable")
            self.notify(
                f"Could not refill the queue: {worker.error}",
                title="Queue",
                severity="warning",
            )

    async def pump_platform(self) -> None:
        """Pump the main NSRunLoop so the AVFoundation pipeline advances.

        Runs on the app's main thread (asyncio task); the player requires the
        main run loop to be serviced for playback to progress.
        """
        while True:
            self.client.player.pump()
            await asyncio.sleep(0.02)

    def _on_player_eof(self) -> None:
        if not self.is_running:
            return
        self.post_message(TrackEnded())

    def on_track_ended(self) -> None:
        self.client.current = None
        if self._auto_next:
            self.action_next_track()
        else:
            self.set_status("Track ended — auto next is off")

    def action_toggle_auto_next(self) -> None:
        self._auto_next = not self._auto_next
        self.query_one(NowPlaying).set_modes(self._auto_next, self._loop_enabled)
        self._settings_store.set(SETTING_AUTO_NEXT, self._auto_next)

    def action_toggle_loop(self) -> None:
        self._loop_enabled = not self._loop_enabled
        self.client.loop = self._loop_enabled
        self.query_one(NowPlaying).set_modes(self._auto_next, self._loop_enabled)
        self._settings_store.set(SETTING_LOOP, self._loop_enabled)

    def action_toggle_playback(self) -> None:
        if self.client.current is not None:
            self.client.player.toggle()

    def action_seek_forward(self) -> None:
        self._seek(5)

    def action_seek_back(self) -> None:
        self._seek(-5)

    def _seek(self, delta: int) -> None:
        if self.client.current is not None:
            self.client.player.seek_relative(delta)

    def action_volume_up(self) -> None:
        self._change_volume(5)

    def action_volume_down(self) -> None:
        self._change_volume(-5)

    def _change_volume(self, delta: int) -> None:
        player = self.client.player
        player.volume = player.volume + delta
        self._settings_store.set(SETTING_VOLUME, player.volume)

    def action_toggle_mute(self) -> None:
        player = self.client.player
        player.muted = not player.muted
        self._settings_store.set(SETTING_MUTED, player.muted)

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
        if target is not None:
            widget = self.query_one(f"#{target}")
            if widget.display:
                widget.focus()
                if (
                    direction == "up"
                    and isinstance(widget, ResultsTable)
                    and widget.row_count
                ):
                    widget.move_cursor(row=widget.row_count - 1)

    def set_status(self, text: str) -> None:
        self.query_one(NowPlaying).set_status(text)

    def _tick(self) -> None:
        player = self.client.player
        now_playing = self.query_one(NowPlaying)
        now_playing.set_progress(player.position, player.duration or 0.0)
        now_playing.set_paused(
            player.paused if self.client.current is not None else False
        )
        now_playing.set_volume(player.volume, player.muted)
        now_playing.set_modes(self._auto_next, self._loop_enabled)
