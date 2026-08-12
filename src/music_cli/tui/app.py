"""Textual TUI for music-cli: search, queue and playback control."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
)
from textual.worker import Worker, WorkerState

from music_cli.client import MusicClient
from music_cli.player import PlaylistTrack, StreamInfo
from music_cli.search import SearchFilter, SearchResult

from .now_playing import NowPlaying

SEARCH_FILTERS: list[tuple[str, SearchFilter | None]] = [
    ("All results", None),
    ("Songs", "songs"),
    ("Videos", "videos"),
    ("Albums", "albums"),
    ("Artists", "artists"),
    ("Playlists", "playlists"),
]

TYPE_COLORS = {
    "SONG": "#a78bfa",
    "VIDEO": "#67e8f9",
    "ALBUM": "#fbbf24",
    "ARTIST": "#f472b6",
    "PLAYLIST": "#34d399",
    "PROFILE": "#94a3b8",
    "PODCAST": "#fb923c",
    "EPISODE": "#fb923c",
}


class TrackEnded(Message):
    """Posted on the app thread when the player reports end-of-file."""


class TopBar(Widget):
    """Slim header with the brand, tagline and queue count."""

    def compose(self) -> ComposeResult:
        yield Label("♪ music-cli", id="brand")
        yield Label("", id="queue-count")


class ResultsTable(DataTable):
    """Search results table with per-row search-result lookup."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._results: dict[str, SearchResult] = {}

    def on_mount(self) -> None:
        self.add_column("", key="type", width=10)
        self.add_column("Title", key="title", width=34)
        self.add_column("Artist", key="artist", width=13)
        self.add_column("Album", key="album", width=8)
        self.add_column("Time", key="duration", width=7)

    def set_results(self, results: list[SearchResult]) -> None:
        self.clear()
        self._results.clear()
        for result in results:
            key = result.video_id or result.browse_id
            if not key:
                continue
            artists = ", ".join(result.artists)
            self.add_row(
                Text(
                    f" {result.type_label}",
                    style=TYPE_COLORS.get(result.type_label, "grey58"),
                ),
                self._fit(result.title, "title"),
                self._fit(artists, "artist"),
                self._fit(result.album or "", "album"),
                self._fit(result.duration or "", "duration"),
                key=key,
            )
            self._results[key] = result

    def _fit(self, value: str, column_key: str) -> str:
        """Truncate a cell value to its column width, appending '...' when cut off."""
        column = self.columns[column_key]
        max_width = column.get_render_width(self) - 2 * self.cell_padding
        if cell_len(value) <= max_width:
            return value
        return set_cell_size(value, max_width - 3) + "... "

    def selected_result(self) -> SearchResult | None:
        coordinate = self.coordinate_to_cell_key(self.cursor_coordinate)
        return self._results.get(str(coordinate.row_key.value))


class QueueList(ListView):
    """Up-next queue; each item is one row of the queue."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tracks: list[PlaylistTrack] = []

    def on_mount(self) -> None:
        self.border_title = " UP NEXT "

    def set_tracks(self, tracks: list[PlaylistTrack]) -> None:
        self._tracks = list(tracks)
        self.clear()
        if not tracks:
            self.index = None
            self.append(
                ListItem(
                    Label(
                        "Queue is empty — play a song to start autoplay",
                        classes="queue-empty",
                    )
                )
            )
            return
        for track in tracks:
            self.append(self._item(track))
        self.index = 0

    def track_at(self, index: int | None) -> PlaylistTrack | None:
        """The track the user sees at ``index``, independent of queue mutations.

        The queue is popped as soon as a track is picked, so the list widget
        (rebuilt only on refresh) is the source of truth for what the user
        actually clicked; this is what makes rapid double clicks collapse into
        one request instead of picking whatever slid into the row.
        """
        if index is None or not 0 <= index < len(self._tracks):
            return None
        return self._tracks[index]

    @staticmethod
    def _item(track: PlaylistTrack) -> ListItem:
        subtitle = " • ".join(track.artists)
        if track.duration:
            subtitle = f"{subtitle} · {track.duration}" if subtitle else track.duration
        if not subtitle:
            subtitle = "Unknown artist"
        return ListItem(
            Label(track.title, classes="queue-title"),
            Label(subtitle, classes="queue-subtitle"),
        )


class MusicTUI(App[None]):
    """The music-cli terminal user interface."""

    CSS_PATH = "theme.tcss"
    TITLE = "music-cli"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("slash", "focus_search", "Search"),
        Binding("space", "toggle_playback", "Play/Pause"),
        Binding("ctrl+right", "seek_forward", "Seek +5s"),
        Binding("ctrl+left", "seek_back", "Seek -5s"),
        Binding("n", "next_track", "Next"),
        Binding("plus", "volume_up", "Vol +", show=False),
        Binding("minus", "volume_down", "Vol -"),
        Binding("m", "toggle_mute", "Mute"),
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "focus_results", show=False),
    ]

    def __init__(self, client: MusicClient | None = None) -> None:
        super().__init__()
        self.client = client or MusicClient(on_track_end=self._on_player_eof)
        self._search_timer: Timer | None = None
        self._search_worker: Worker[list[SearchResult]] | None = None
        self._queue_worker: Worker[list[PlaylistTrack]] | None = None
        self._play_worker: Worker[StreamInfo] | None = None
        self._play_worker_video_id: str = ""
        self._play_queued_worker: Worker[StreamInfo] | None = None
        self._play_queued_worker_video_id: str = ""
        self._last_video_id: str = ""
        self._pending_track: PlaylistTrack | None = None
        self._pending_index: int = 0

    def on_mount(self) -> None:
        self.set_interval(0.5, self._tick)
        self.query_one("#search-input", Input).focus()
        self.run_worker(self.pump_platform(), exclusive=False)

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
        self.client.close()

    def compose(self) -> ComposeResult:
        yield TopBar()
        with Horizontal(id="body"):
            with Vertical(id="results-pane"):
                with Horizontal(id="search-box"):
                    yield Input(
                        placeholder="Search songs, artists, albums…",
                        id="search-input",
                    )
                    yield Select(
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
            "play_queued_worker": self._on_play_queued_finished,
            "refill_worker": self._on_refill_finished,
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
        for worker, worker_video_id in (
            (self._play_worker, self._play_worker_video_id),
            (self._play_queued_worker, self._play_queued_worker_video_id),
        ):
            if (
                worker is not None
                and worker.state in (WorkerState.PENDING, WorkerState.RUNNING)
                and worker_video_id == video_id
            ):
                return True
        return False

    def play_result(self, result: SearchResult) -> None:
        if self._play_pending(result.video_id):
            return
        self._last_video_id = result.video_id
        self._cancel_queue_fetch()
        self.query_one(NowPlaying).set_track(
            result.title,
            result.subtitle,
        )
        self.set_status("Resolving stream…")
        self._play_worker_video_id = result.video_id
        self._play_worker = self.play_worker(result)

    @work(thread=True, exit_on_error=False)
    def play_worker(self, result: SearchResult) -> StreamInfo:
        return self.client.play_result(result)

    def _on_play_finished(self, worker: Worker[StreamInfo]) -> None:
        if worker.state is WorkerState.SUCCESS:
            stream = worker.result
            self.query_one(NowPlaying).set_track(
                stream.title,
                ", ".join(stream.artists) or "Unknown artist",
                stream.duration,
            )
            self.set_status("Loading up-next queue…")
            self._queue_worker = self.fetch_queue_worker(stream.video_id)
        else:
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
        self._last_video_id = track.video_id
        self._pending_track = track
        self._pending_index = index
        self.query_one(NowPlaying).set_track(track.title, " • ".join(track.artists))
        self.set_status("Resolving stream…")
        self._play_queued_worker_video_id = track.video_id
        self._play_queued_worker = self.play_queued_worker(track)

    @work(thread=True, exit_on_error=False)
    def play_queued_worker(self, track: PlaylistTrack) -> StreamInfo:
        return self.client.play_track(track)

    def _on_play_queued_finished(self, worker: Worker[StreamInfo]) -> None:
        if worker.state is WorkerState.SUCCESS:
            stream = worker.result
            self.query_one(NowPlaying).set_track(
                stream.title,
                ", ".join(stream.artists) or "Unknown artist",
                stream.duration,
            )
            self._refresh_queue()
            self._prefetch_next()
            self.set_status("Playing from queue")
        else:
            self.client.queue.insert(self._pending_index, self._pending_track)
            self._refresh_queue()
            self.set_status("Playback failed")
            self.notify(
                f"Could not play: {worker.error}", title="Playback", severity="error"
            )

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
        self.action_next_track()

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

    def action_toggle_mute(self) -> None:
        player = self.client.player
        player.muted = not player.muted

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_focus_results(self) -> None:
        self.query_one(ResultsTable).focus()

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
