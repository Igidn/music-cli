"""Headless smoke tests for the TUI, using fakes for the network layer.

Runs the Textual app through ``run_test``; sync test wrappers call the
async scenario with ``asyncio.run`` so no async pytest plugin is needed.
"""

from __future__ import annotations

import asyncio
import threading

from music_cli.client import MusicClient
from music_cli.player import PlaylistTrack, StreamInfo
from music_cli.playlists import LibraryPlaylist
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


class FakeLibrary:
    def __init__(self, authenticated=True):
        self.authenticated = authenticated
        self.playlist_calls = []
        self.track_calls = []
        self._tracks = {
            "p1": [
                PlaylistTrack(video_id="p1t1", title="Mix One", artists=["A"]),
                PlaylistTrack(video_id="p1t2", title="Mix Two", artists=["B"]),
            ],
            "p2": [PlaylistTrack(video_id="p2t1", title="Chill One", artists=["C"])],
        }

    def playlists(self, limit=None):
        self.playlist_calls.append(limit)
        return [
            LibraryPlaylist(playlist_id="p1", title="My Mix", track_count="2"),
            LibraryPlaylist(playlist_id="p2", title="Chill", track_count="1"),
        ]

    def tracks(self, playlist_id):
        self.track_calls.append(playlist_id)
        return self._tracks.get(playlist_id, [])


class FakePlayer:
    def __init__(self):
        self.played = []
        self.pause = False
        self._muted = False
        self._volume = 80
        self.playback_time = 0.0
        self._duration = 213.0
        self.loop = False

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

    def pump(self):
        pass


def make_client() -> MusicClient:
    client = MusicClient.__new__(MusicClient)
    client.search_api = FakeSearch()
    client.extractor = FakeExtractor()
    client.watch = FakeWatch()
    client.library = FakeLibrary()
    client.queue = []
    client.current = None
    client.cache = None
    client.player = FakePlayer()
    client._in_flight = set()
    client._play_lock = threading.Lock()
    return client


def _run(coro):
    return asyncio.run(coro)


def test_arrow_key_pane_navigation():
    from music_cli.tui.app import MusicTUI, QueueList, ResultsTable

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            search = app.query_one("#search-input")
            results = app.query_one(ResultsTable)
            queue = app.query_one(QueueList)
            filter_select = app.query_one("#filter-select")
            assert app.focused is search

            await pilot.press("down")
            assert app.focused is results

            await pilot.press("right")
            assert app.focused is queue

            await pilot.press("left")
            assert app.focused is results

            await pilot.press("up")
            assert app.focused is search

            search.value = "hello"
            await pilot.press("left")
            assert app.focused is search
            assert search.cursor_position == 4
            await pilot.press("right")
            assert app.focused is search
            assert search.cursor_position == 5

            await pilot.press("down")
            assert app.focused is results
            await pilot.press("right")
            assert app.focused is queue
            await pilot.press("up")
            assert app.focused is filter_select
            await pilot.press("left")
            assert app.focused is search
            await pilot.press("down")
            assert app.focused is results

            search.value = "coldplay"
            await pilot.pause(0.6)
            assert results.row_count == 3
            results.focus()
            await pilot.press("down")
            assert results.cursor_coordinate.row == 1
            await pilot.press("up")
            assert results.cursor_coordinate.row == 0
            await pilot.press("up")
            assert app.focused is search
            assert search.selection.is_empty  # landing in search must not select all

            # Up from search wraps around to the bottom of the results.
            await pilot.press("up")
            assert app.focused is results
            assert results.cursor_coordinate.row == 2

            queue.set_tracks([PlaylistTrack(video_id="t1", title="One", artists=["A"])])
            queue.focus()
            assert queue.index == 0
            await pilot.press("up")
            assert app.focused is filter_select
            await pilot.press("right")
            assert app.focused is queue
            await pilot.press("left")
            assert app.focused is results

            playlist = app.query_one("#playlist-pane")
            await pilot.press("left")
            assert app.focused is playlist
            await pilot.press("right")
            assert app.focused is results
            results.focus()
            await pilot.pause()
            await pilot.press("p")
            assert app.focused is playlist

    _run(scenario())


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


def test_tui_persists_and_restores_player_settings():
    from music_cli.tui.app import MusicTUI

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_volume_up()
            app.action_volume_up()
            app.action_toggle_mute()
            app.action_toggle_loop()
            app.action_toggle_auto_next()
            await pilot.pause()
            assert client.player.volume == 90
            assert client.player.muted is True
            assert client.player.loop is True
            assert app._auto_next is False

        resumed_client = make_client()
        resumed = MusicTUI(resumed_client)
        async with resumed.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert resumed_client.player.volume == 90
            assert resumed_client.player.muted is True
            assert resumed_client.player.loop is True
            assert resumed._auto_next is False

    _run(scenario())


def test_tui_auto_next_toggle_stops_at_track_end():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        client.queue = [
            PlaylistTrack(video_id="t1", title="Up next", artists=["B"]),
        ]
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._auto_next is True

            app.action_toggle_auto_next()
            assert app._auto_next is False

            client.current = FakeExtractor().resolve("v1")
            app.on_track_ended()
            await pilot.pause()
            assert client.current is None
            assert not client.player.played
            assert "auto next is off" in str(
                app.query_one(NowPlaying).query_one("#np-status").content
            )

            app.action_toggle_auto_next()
            assert app._auto_next is True
            app.on_track_ended()
            await pilot.pause()
            assert client.player.played[-1].video_id == "t1"

    _run(scenario())


def test_tui_loop_toggle():
    from music_cli.tui.app import MusicTUI

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._loop_enabled is False

            app.action_toggle_loop()
            assert app._loop_enabled is True
            assert client.player.loop is True

            app.action_toggle_loop()
            assert app._loop_enabled is False
            assert client.player.loop is False

    _run(scenario())


def test_tui_mode_indicators():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            np = app.query_one(NowPlaying)
            loop = np.query_one("#np-loop")
            auto = np.query_one("#np-auto")
            assert loop.has_class("off") and not loop.has_class("on")
            assert auto.has_class("on") and not auto.has_class("off")

            app.action_toggle_loop()
            app.action_toggle_auto_next()
            assert loop.has_class("on")
            assert auto.has_class("off")

            await pilot.pause(0.6)
            assert loop.has_class("on")
            assert auto.has_class("off")

    _run(scenario())


def test_tui_wires_player_eof_and_auto_advances():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        client.queue = [
            PlaylistTrack(video_id="t1", title="Up next", artists=["B"]),
        ]
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert client.player.on_track_end == app._on_player_eof

            client.current = FakeExtractor().resolve("v1")
            client.player.on_track_end()
            await pilot.pause()
            await pilot.pause()
            assert client.player.played[-1].video_id == "t1"
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Stream of t1"

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


def test_library_tree_renders_playlists():
    from music_cli.tui.app import LibraryTree, MusicTUI

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            tree = app.query_one(LibraryTree)
            assert tree.id == "playlist-pane"
            assert tree.can_focus
            playlists = list(tree.root.children)
            assert [node.data["kind"] for node in playlists] == ["playlist", "playlist"]
            assert "My Mix" in str(playlists[0].label.plain)

    _run(scenario())


def test_library_tree_sign_in_notice_when_unauthenticated():
    from music_cli.tui.app import LibraryTree, MusicTUI

    async def scenario():
        client = make_client()
        client.library = FakeLibrary(authenticated=False)
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one(LibraryTree)
            assert "Sign in" in str(tree.root.children[0].label.plain)

    _run(scenario())


def test_library_tree_expand_loads_tracks():
    from music_cli.tui.app import LibraryTree, MusicTUI

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            tree = app.query_one(LibraryTree)
            tree.focus()
            await pilot.press("down")
            node = tree.cursor_node
            assert node.data["playlist_id"] == "p1"
            assert not node.is_expanded

            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
            assert client.library.track_calls == ["p1"]
            assert node.is_expanded
            children = list(node.children)
            assert [child.data["kind"] for child in children] == ["track", "track"]
            assert children[0].data["track"].video_id == "p1t1"

            # Activating a loaded playlist collapses it again.
            await pilot.press("enter")
            assert not node.is_expanded

    _run(scenario())


def test_library_tree_activates_track_plays_and_queues_playlist():
    from music_cli.tui.app import LibraryTree, MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        client.extractor = CountingExtractor()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            tree = app.query_one(LibraryTree)
            tree.focus()
            await pilot.press("down")
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()

            assert client.player.played[-1].video_id == "p1t1"
            assert [track.video_id for track in client.queue] == ["p1t2"]
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Stream of p1t1"

    _run(scenario())


def test_narrow_layout_hides_side_panes():
    from music_cli.tui.app import LibraryTree, MusicTUI, ResultsTable

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            playlist = app.query_one(LibraryTree)
            queue = app.query_one("#queue-pane")
            assert not app.screen.has_class("-narrow")
            assert queue.display and playlist.display

            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            assert app.screen.has_class("-narrow")
            assert not queue.display
            assert not playlist.display

            # Pane navigation skips panes hidden in narrow mode.
            results = app.query_one(ResultsTable)
            results.focus()
            await pilot.pause()
            await pilot.press("left")
            assert app.focused is results
            await pilot.press("right")
            assert app.focused is results
            await pilot.press("p")
            assert app.focused is results

            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert not app.screen.has_class("-narrow")
            assert queue.display and playlist.display

    _run(scenario())


def test_search_edges_jump_to_side_panes():
    from music_cli.tui.app import LibraryTree, MusicTUI, QueueList, ResultsTable

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            search = app.query_one("#search-input")
            queue = app.query_one(QueueList)
            playlist = app.query_one(LibraryTree)

            # Empty input: left/right jump to the side panes.
            await pilot.press("left")
            assert app.focused is playlist
            await pilot.press("right")
            assert app.focused is app.query_one(ResultsTable)

            search.focus()
            search.value = "hello"
            await pilot.press("right")  # at end of text → up next
            assert app.focused is queue
            await pilot.press("left")
            assert app.focused is app.query_one(ResultsTable)

            # Mid-text, left/right still move the cursor.
            search.focus()
            await pilot.pause()
            search.cursor_position = 2
            await pilot.press("left")
            assert app.focused is search
            assert search.cursor_position == 1

    _run(scenario())


def test_tui_saves_last_played_track_and_resumes_it():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.play_result(make_result(0, "v1"))
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            saved = app._history_store.most_recent()
            assert saved is not None
            assert saved.video_id == "v1"
            assert saved.title == "Stream of v1"
            assert saved.artists == ("Streamed Artist",)

        resumed_client = make_client()
        resumed = MusicTUI(resumed_client)
        async with resumed.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            assert resumed_client.player.played[-1].video_id == "v1"
            np = resumed.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Stream of v1"

    _run(scenario())


def test_tui_without_saved_track_stays_idle_on_mount():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert not client.player.played
            assert app.query_one(NowPlaying).query_one("#np-title").content == (
                "Nothing playing"
            )

    _run(scenario())


def test_tui_saves_track_played_from_queue():
    from music_cli.tui.app import MusicTUI, QueueList

    async def scenario():
        client = make_client()
        client.queue = [
            PlaylistTrack(video_id="t1", title="One", artists=["A"]),
        ]
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            queue = app.query_one(QueueList)
            queue.set_tracks(client.queue)
            app._on_queue_selected(QueueList.Selected(queue, queue.children[0], index=0))
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            saved = app._history_store.most_recent()
            assert saved is not None
            assert saved.video_id == "t1"

    _run(scenario())


def test_tui_does_not_save_when_playback_fails():
    from music_cli.tui.app import MusicTUI

    class FailingExtractor:
        def resolve(self, video_id):
            raise RuntimeError("boom")

    async def scenario():
        client = make_client()
        client.extractor = FailingExtractor()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.play_result(make_result(0, "v1"))
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            assert app._history_store.most_recent() is None

    _run(scenario())


def test_waveform_widget_tracks_playback_state():
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.now_playing import NowPlaying
    from music_cli.tui.waveform import Waveform

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            np = app.query_one(NowPlaying)
            waveform = app.query_one(Waveform)

            idle = waveform.render()
            assert "▁" in idle.plain
            assert len(idle.plain.splitlines()) == waveform.size.height

            np.set_track("Title", "Artist", 213.0)
            assert waveform._active
            await pilot.pause(0.3)
            animated = waveform.render()
            assert len(animated.plain.splitlines()) == 3
            assert any(block in animated.plain for block in "▂▃▄▅▆▇█")

            # Pausing freezes the animation; resuming advances it again.
            client.current = FakeExtractor().resolve("v1")
            app.action_toggle_playback()
            await pilot.pause(0.7)
            assert waveform._paused
            frozen_time = waveform._time
            await pilot.pause(0.3)
            assert waveform._time == frozen_time

            app.action_toggle_playback()
            await pilot.pause(0.7)
            assert not waveform._paused
            assert waveform._time > frozen_time

            np.clear_track()
            assert not waveform._active
            assert "▁" in waveform.render().plain

    _run(scenario())
