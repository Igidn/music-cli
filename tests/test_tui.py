"""Headless smoke tests for the TUI, using fakes for the network layer.

Runs the Textual app through ``run_test``; sync test wrappers call the
async scenario with ``asyncio.run`` so no async pytest plugin is needed.
"""

from __future__ import annotations

import asyncio
import threading

from music_cli.client import MusicClient
from music_cli.player import PlaylistTrack, StreamInfo
from music_cli.search import SearchResult


def make_result(i, video_id):
    return SearchResult(
        result_type="song",
        title=f"Song {i}",
        artists=[f"Artist {i}"],
        album="Album",
        duration="3:21",
        video_id=video_id,
        browse_id="",
        year="2024",
        raw={},
    )


class FakeSearch:
    def __init__(self):
        self.queries = []

    def search(self, query, limit=20, filter=None):
        self.queries.append((query, filter))
        return [make_result(i, f"v{i}") for i in range(3)]


class FakeExtractor:
    def resolve(self, video_id):
        return StreamInfo(
            video_id=video_id,
            title=f"Stream of {video_id}",
            stream_url="https://example.test/audio",
            artists=["Streamed Artist"],
            duration=213.0,
        )


class FakeWatch:
    def get(
        self, video_id=None, playlist_id=None, radio=False, shuffle=False, limit=None
    ):
        if radio:
            return []
        return [
            PlaylistTrack(video_id=video_id or "t0", title="Current", artists=["A"]),
            PlaylistTrack(
                video_id="t1", title="Up next", artists=["B"], duration="3:00"
            ),
            PlaylistTrack(video_id="t2", title="Later", artists=["C"]),
        ]


class FakePlayer:
    def __init__(self):
        self.played = []
        self.pause = False
        self._muted = False
        self._volume = 80
        self.playback_time = 0.0
        self._duration = 213.0

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = max(0, min(100, value))

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = value

    @property
    def position(self):
        return self.playback_time

    @property
    def eof_reached(self):
        return False

    @property
    def paused(self):
        return self.pause

    @property
    def duration(self):
        return self._duration

    @duration.setter
    def duration(self, value):
        self._duration = value

    def play(self, stream):
        self.played.append(stream)
        self.playback_time = 0.5

    def toggle(self):
        self.pause = not self.pause

    def seek_relative(self, delta):
        self.playback_time += delta

    def close(self):
        pass


def make_client() -> MusicClient:
    client = MusicClient.__new__(MusicClient)
    client.search_api = FakeSearch()
    client.extractor = FakeExtractor()
    client.watch = FakeWatch()
    client.queue = []
    client.current = None
    client.player = FakePlayer()
    client._in_flight = set()
    client._play_lock = threading.Lock()
    return client


def _run(coro):
    return asyncio.run(coro)


def test_tui_search_play_queue_and_next():
    from music_cli.tui.app import MusicTUI, QueueList, ResultsTable
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            search = app.query_one("#search-input")
            search.value = "midnight city"
            await pilot.pause(0.6)
            results = app.query_one(ResultsTable)
            assert len(results._results) == 3
            assert results._results["v1"].title == "Song 1"

            app.play_result(results._results["v1"])
            await pilot.pause()
            await pilot.pause()
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Stream of v1"
            assert client.player.played

            await pilot.pause(0.6)
            queue_list = app.query_one(QueueList)
            assert len(queue_list.children) == 2

            app.action_next_track()
            await pilot.pause()
            await pilot.pause()
            assert client.player.played[-1].video_id == "t1"

            client.player.playback_time = 5.0
            await pilot.pause(0.6)
            assert str(app.query_one("#np-time").content) == "0:05 / 3:33"

    _run(scenario())


def test_tui_search_empty_and_track_end():
    from music_cli.tui.app import MusicTUI, ResultsTable
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            search = app.query_one("#search-input")
            search.value = ""
            await pilot.pause(0.6)
            assert app.query_one(ResultsTable).row_count == 0

            search.value = "coldplay"
            await pilot.pause(0.6)
            assert app.query_one(ResultsTable).row_count == 3

            app.client.player.played.append("x")
            client.current = FakeExtractor().resolve("v1")
            client.queue = []
            app._last_video_id = "v1"
            app.on_track_ended()
            await pilot.pause()
            await pilot.pause(0.6)
            assert app.client.player.played[-1] == "x"
            assert (
                str(app.query_one(NowPlaying).query_one("#np-status").content)
                == "End of station"
            )

    _run(scenario())


def test_transport_actions():
    from music_cli.tui.app import MusicTUI

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            client.current = FakeExtractor().resolve("v1")
            app.action_toggle_playback()
            assert client.player.pause is True
            app.action_toggle_playback()
            assert client.player.pause is False
            app.action_seek_forward()
            assert client.player.playback_time == 5.0
            app.action_volume_up()
            assert client.player.volume == 85
            app.action_toggle_mute()
            assert client.player.muted is True

    _run(scenario())


class CountingExtractor(FakeExtractor):
    """Fake extractor that counts how many resolutions actually happen."""

    def __init__(self):
        self.resolves = 0

    def resolve(self, video_id):
        self.resolves += 1
        return super().resolve(video_id)


def test_tui_double_click_plays_once():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        client.extractor = CountingExtractor()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            result = make_result(0, "v1")
            app.play_result(result)
            app.play_result(result)
            await pilot.pause()
            await pilot.pause()
            assert client.extractor.resolves == 1
            assert len(client.player.played) == 1
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Stream of v1"

    _run(scenario())


def test_tui_queue_double_select_plays_once():
    from music_cli.tui.app import MusicTUI, QueueList

    async def scenario():
        client = make_client()
        client.extractor = CountingExtractor()
        client.queue = [
            PlaylistTrack(video_id="t1", title="One", artists=["A"]),
            PlaylistTrack(video_id="t2", title="Two", artists=["B"]),
            PlaylistTrack(video_id="t3", title="Three", artists=["C"]),
        ]
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            queue = app.query_one(QueueList)
            queue.set_tracks(client.queue)
            selected = QueueList.Selected(queue, queue.children[0], index=0)
            app._on_queue_selected(selected)
            app._on_queue_selected(selected)
            await pilot.pause()
            await pilot.pause()
            assert client.extractor.resolves == 1
            assert len(client.player.played) == 1
            assert [t.video_id for t in client.queue] == ["t2", "t3"]

    _run(scenario())


def test_tui_queue_click_after_refresh_keeps_track_identity():
    from music_cli.tui.app import MusicTUI, QueueList

    async def scenario():
        client = make_client()
        client.extractor = CountingExtractor()
        client.queue = [
            PlaylistTrack(video_id="t1", title="One", artists=["A"]),
            PlaylistTrack(video_id="t2", title="Two", artists=["B"]),
        ]
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            queue = app.query_one(QueueList)
            queue.set_tracks(client.queue)
            assert queue.track_at(0).video_id == "t1"
            assert queue.track_at(5) is None
            app._on_queue_selected(
                QueueList.Selected(queue, queue.children[1], index=1)
            )
            await pilot.pause()
            await pilot.pause()
            assert client.extractor.resolves == 1
            assert client.player.played[0].video_id == "t2"
            assert [t.video_id for t in client.queue] == ["t1"]

    _run(scenario())
