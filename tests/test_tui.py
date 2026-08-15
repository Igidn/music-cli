"""Headless smoke tests for the TUI, using fakes for the network layer.

Runs the Textual app through ``run_test``; sync test wrappers call the
async scenario with ``asyncio.run`` so no async pytest plugin is needed.

Playback is driven through a ``FakeDaemon`` standing in for the IPC control +
events sockets: every action is asserted as the request the TUI sends, and
pushed status dicts are injected by calling the render handler directly.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from pathlib import Path

import music_cli.ipc as ipc
from music_cli.client import MusicClient
from music_cli.yt.extract import PlaylistTrack, StreamInfo
from music_cli.yt.playlists import LibraryPlaylist
from music_cli.yt.search import SearchResult

STATUS = {
    "state": "playing",
    "track": {
        "video_id": "abc",
        "title": "Some Song",
        "artists": ["Some Artist"],
        "duration": 201.0,
    },
    "position": 72.0,
    "duration": 201.0,
    "volume": 80,
    "muted": False,
    "loop": False,
    "auto_next": True,
    "queue": [
        {
            "video_id": "q1",
            "title": "Next One",
            "artists": ["Artist B"],
            "duration": "2:01",
        },
        {
            "video_id": "q2",
            "title": "Later One",
            "artists": ["Artist C"],
            "duration": None,
        },
    ],
}

IDLE_STATUS = {
    "state": "stopped",
    "track": None,
    "position": 0.0,
    "duration": None,
    "volume": 80,
    "muted": False,
    "loop": False,
    "auto_next": True,
    "queue": [],
}


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


def make_album_result(browse_id="MPREb_album"):
    return SearchResult(
        result_type="album",
        title="Some Album",
        artists=["Some Artist"],
        album="Some Album",
        duration="",
        video_id="",
        browse_id=browse_id,
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
        self.create_calls = []
        self.rename_calls = []
        self.add_calls = []
        self.remove_calls = []
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

    def create_playlist(self, title):
        self.create_calls.append(title)
        return "p3"

    def rename_playlist(self, playlist_id, title):
        self.rename_calls.append((playlist_id, title))

    def add_tracks(self, playlist_id, video_ids):
        self.add_calls.append((playlist_id, video_ids))

    def remove_track(self, playlist_id, video_id):
        self.remove_calls.append((playlist_id, video_id))


class FakeDaemon:
    """Stands in for ipc.send_request; records requests and answers canned."""

    def __init__(self, status=None):
        self.requests = []
        self.status = dict(status) if status is not None else dict(STATUS)
        self.ok = True
        self.error = "boom"

    def respond(self, request, timeout=30.0):
        self.requests.append(request)
        if not self.ok:
            return {"ok": False, "error": self.error}
        cmd = request["cmd"]
        if cmd == "volume":
            delta = request.get("delta", 0)
            return {
                "ok": True,
                "data": {"volume": (self.status["volume"] or 0) + delta},
            }
        if cmd in ("mute", "loop", "auto_next"):
            return {"ok": True, "data": {cmd: not self.status.get(cmd, False)}}
        if cmd == "status":
            return {"ok": True, "data": self.status}
        # play / toggle / next / seek / stop all hand back the full status.
        return {"ok": True, "data": self.status}


def make_client() -> MusicClient:
    client = MusicClient.__new__(MusicClient)
    client.search_api = FakeSearch()
    client.extractor = FakeExtractor()
    client.watch = FakeWatch()
    client.library = FakeLibrary()
    client.queue = []
    client.current = None
    client._playlist = []
    client.cache = None
    client.player = FakePlayer()
    client._in_flight = set()
    client._play_lock = threading.Lock()
    return client


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

    def close(self):
        pass

    def pump(self):
        pass


def install_daemon(monkeypatch, status=None):
    """Point the TUI at a fake control+events socket so tests run headless."""
    fake = FakeDaemon(status)
    # events_socket_path yields a socket that can never connect: the events
    # worker just backs off and drains until the app closes. The engine shard
    # provides these functions, so add them onto the module for the tests.
    monkeypatch.setattr(ipc, "events_socket_path", _unused_socket_path, raising=False)
    monkeypatch.setattr(
        ipc, "ensure_daemon", lambda cookies=None, volume=None: None, raising=False
    )
    monkeypatch.setattr(ipc, "send_request", fake.respond)
    return fake


def _unused_socket_path() -> Path:
    return Path(tempfile.mkdtemp()) / "no-daemon.sock"


def _run(coro):
    return asyncio.run(coro)


async def _settle(pilot, n=6):
    for _ in range(n):
        await pilot.pause()


def _push(app, status):
    """Inject a status dict exactly as the events worker would."""
    app._render_status(status)


def test_arrow_key_pane_navigation(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import QueueList, ResultsTable

    install_daemon(monkeypatch)

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

            playlist = app.query_one("#library-tree")
            await pilot.press("left")
            assert app.focused is playlist
            await pilot.press("right")
            assert app.focused is results

    _run(scenario())


def test_theme_switch_restyles_and_persists(monkeypatch):
    """Switching themes (Ctrl+P → Theme) recolors the UI and is saved."""
    from music_cli.storage.state import SETTING_THEME
    from music_cli.tui.app import MUSIC_CLI_THEME, MusicTUI

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.theme == MUSIC_CLI_THEME.name
            bg = app.screen.styles.background
            from textual.widgets import ProgressBar

            from music_cli.tui.components import Waveform

            waveform = app.query_one(Waveform)
            progress = app.query_one("#np-progress", ProgressBar)
            wave_gradient = waveform._gradient
            bar_gradient = progress.gradient
            app.theme = "tokyo-night"
            await pilot.pause()
            await pilot.pause()
            assert app.screen.styles.background != bg
            assert waveform._gradient != wave_gradient
            assert progress.gradient != bar_gradient
            assert app._settings_store.get(SETTING_THEME) == "tokyo-night"

    _run(scenario())


def test_theme_restored_from_settings(monkeypatch):
    """A theme saved by a previous run wins over the built-in default."""
    from music_cli.tui.app import MusicTUI

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)):
            app.theme = "nord"
        restored = MusicTUI(make_client())
        assert restored.theme == "nord"

    _run(scenario())


class CountingExtractor(FakeExtractor):
    """Spawned via MusicClient to count resolutions that would have happened."""

    def __init__(self):
        self.resolves = 0

    def resolve(self, video_id):
        self.resolves += 1
        return super().resolve(video_id)


def _push_playing(app, track_id="abc", title="Some Song", paused=False):
    status = dict(STATUS)
    status["track"] = {
        "video_id": track_id,
        "title": title,
        "artists": ["Some Artist"],
        "duration": 201.0,
    }
    status["state"] = "paused" if paused else "playing"
    _push(app, status)


async def _run_action(pilot, fn):
    fn()
    await _settle(pilot)


def test_search_select_sends_play_request(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import NowPlaying, ResultsTable

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            search = app.query_one("#search-input")
            search.value = "midnight city"
            await pilot.pause(0.6)
            results = app.query_one(ResultsTable)
            app.play_result(results._results["v1"])
            await _settle(pilot)

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1
            assert plays[0]["video_id"] == "v1"
            assert plays[0]["title"] == "Song 1"

            _push_playing(app, track_id="v1", title="Stream of v1")
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Stream of v1"

    _run(scenario())


def test_album_result_sends_album_play_request(monkeypatch):
    from music_cli.tui.app import MusicTUI

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.play_result(make_album_result("MPREb_album"))
            await _settle(pilot)

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1
            # An album row must never be sent as an empty video_id play,
            # which the daemon rejects with "a video id is required".
            assert "video_id" not in plays[0]
            assert plays[0]["album_id"] == "MPREb_album"

    _run(scenario())


def test_queue_select_sends_queue_index(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import QueueList

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            # Render a queue from a pushed status, then select row 1.
            _push(app, STATUS)
            queue = app.query_one(QueueList)
            queue.focus()
            await pilot.pause()
            app._on_queue_selected(
                QueueList.Selected(queue, queue.children[1], index=1)
            )
            await _settle(pilot)

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1
            assert plays[0]["queue_index"] == 1

    _run(scenario())


def test_library_track_activates_sends_playlist_play(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
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

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1
            assert plays[0]["playlist_id"] == "p1"
            assert plays[0]["playlist_index"] == 0

    _run(scenario())


def test_history_select_sends_play_request(monkeypatch):
    from music_cli.storage.state import PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import HistoryList

    fake = install_daemon(monkeypatch)

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        from music_cli.storage.state import PlayedTrack

        store.record(PlayedTrack("h1", "History One", ("A",)))
        store.record(PlayedTrack("h2", "History Two", ("B",)))
        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 8)
            history = app.query_one(HistoryList)
            history.focus()
            history.index = 0
            await _settle(pilot)
            app._on_history_selected(HistoryList.Selected(history, None, index=0))
            await _settle(pilot)

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1
            assert plays[0]["video_id"] == "h2"

    _run(scenario())


def test_list_selection_plays_only_the_clicked_pane_track(monkeypatch):
    """A history selection must not also play the queue row at the same index.

    QueueList and HistoryList share the inherited ``ListView.Selected``
    message class; without a CSS selector the app's ``@on`` handlers both
    fire for either list, so clicking history row i also fired
    ``play queue_index=i`` — the daemon resolved, played and recorded that
    up-next track before the requested one. Same for a queue click firing a
    history play.
    """
    from music_cli.storage.state import PlayedTrack, PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import HistoryList, QueueList

    fake = install_daemon(monkeypatch)

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        store.record(PlayedTrack("h1", "History One", ("A",)))
        store.record(PlayedTrack("h2", "History Two", ("B",)))
        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 8)
            _push(app, STATUS)  # populates the up-next queue (q1, q2)
            await _settle(pilot)
            history = app.query_one(HistoryList)
            # Real dispatch: the Selected message bubbles to the app.
            history.post_message(HistoryList.Selected(history, None, index=0))
            await _settle(pilot, 8)

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1
            assert plays[0]["video_id"] == "h2"

            queue = app.query_one(QueueList)
            queue.post_message(QueueList.Selected(queue, None, index=0))
            await _settle(pilot, 8)

            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 2
            assert plays[1].get("queue_index") == 0
            assert "video_id" not in plays[1]

    _run(scenario())


def test_in_flight_play_ignores_stale_track_status(monkeypatch):
    """A per-track push for a different track must not flash while play is pending.

    Selecting a track (e.g. from history) optimistically shows it, then the
    daemon resolves it asynchronously. Between the two, stale statuses can
    arrive for the up-next head or the previous track; the control bar must
    not briefly regress to those before the requested track lands.
    """
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import NowPlaying

    install_daemon(monkeypatch)

    def status_for(track_id, title, artist):
        return {
            "state": "playing",
            "track": {
                "video_id": track_id,
                "title": title,
                "artists": [artist],
                "duration": 200.0,
            },
            "position": 10.0,
            "duration": 200.0,
            "volume": 80,
            "muted": False,
            "loop": False,
            "auto_next": True,
            "queue": [],
        }

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 6)
            _push(app, status_for("A", "Track A", "Artist A"))
            await _settle(pilot)
            np = app.query_one(NowPlaying)

            def title() -> str:
                return str(np.query_one("#np-title").content)

            # User selects a track: optimistic display + in-flight play guard.
            app._pending_play = "C"
            np.set_track("Track C", "Artist C")
            assert title() == "Track C"

            # The up-next head auto-advances (or a stale heartbeat arrives) and
            # is pushed before the requested track has resolved.
            _push(app, status_for("B", "Up Next B", "Artist B"))
            await _settle(pilot)
            assert title() == "Track C"
            assert app._current_video_id == "A"

            # The requested track finally resolves and takes over the bar.
            app._pending_play = None
            _push(app, status_for("C", "Track C", "Artist C"))
            await _settle(pilot)
            assert title() == "Track C"
            assert app._current_video_id == "C"

    _run(scenario())


def test_transport_actions_send_ipc(monkeypatch):
    from music_cli.tui.app import MusicTUI

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            cmds = []
            before = len(fake.requests)
            app.action_toggle_playback()
            app.action_seek_forward()
            app.action_seek_back()
            app.action_volume_up()
            app.action_toggle_mute()
            await _settle(pilot)
            new = fake.requests[before:]
            cmds = [r["cmd"] for r in new]
            assert "toggle" in cmds
            assert {"cmd": "seek", "offset": 5} in new
            assert {"cmd": "seek", "offset": -5} in new
            volume = [r for r in new if r["cmd"] == "volume"]
            assert volume == [{"cmd": "volume", "delta": 5}]
            mute = [r for r in new if r["cmd"] == "mute"]
            assert mute == [{"cmd": "mute", "state": "toggle"}]

    _run(scenario())


def test_next_guard_collapses_double_press(monkeypatch):
    from music_cli.tui.app import MusicTUI

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.action_next_track()
            app.action_next_track()  # in flight -> dropped
            await _settle(pilot)
            assert [r["cmd"] for r in fake.requests].count("next") == 1

    _run(scenario())


def test_double_click_plays_once(monkeypatch):
    from music_cli.tui.app import MusicTUI

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            result = make_result(0, "v1")
            app.play_result(result)
            app.play_result(result)  # same video, in flight -> dropped
            await _settle(pilot)
            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert len(plays) == 1

    _run(scenario())


def test_pushed_status_updates_now_playing_and_queue(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import NowPlaying, QueueList

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app._render_status(IDLE_STATUS)
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Nothing playing"

            _push(app, STATUS)
            assert str(np.query_one("#np-title").content) == "Some Song"
            assert str(np.query_one("#np-subtitle").content) == "Some Artist"
            queue = app.query_one(QueueList)
            assert queue.track_at(0).video_id == "q1"
            assert str(app.query_one("#queue-count").content) == "2 up next"
            assert str(np.query_one("#np-time").content) == "1:12 / 3:21"

    _run(scenario())


def test_pointer_on_popped_head_stays_highlighted(monkeypatch):
    """Cursor parked on the up-next head survives that track starting to play.

    The head pops out of the queue when it begins playing, so the rebuilt row
    must be re-highlighted rather than left with the stale (removed) row.
    """
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import QueueList

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            _push(app, STATUS)
            await _settle(pilot)
            queue = app.query_one(QueueList)
            assert queue.index == 0  # pointer on the top (next) track
            st = dict(STATUS)
            st["track"] = dict(STATUS["track"])
            st["track"]["video_id"] = "q1"  # q1 starts playing
            st["queue"] = [st["queue"][1]]  # ...and leaves the queue (now [q2])
            _push(app, st)
            await _settle(pilot)
            assert queue.index == 0
            assert len(queue._nodes) == 1
            assert queue._nodes[0].highlighted is True

    _run(scenario())


def test_progress_position_from_status_event(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import NowPlaying

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            status = dict(STATUS)
            status["position"] = 5.0
            _push(app, status)
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-time").content) == "0:05 / 3:21"

    _run(scenario())


def test_track_change_refreshes_history(monkeypatch):
    from music_cli.storage.state import PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import HistoryList

    install_daemon(monkeypatch)

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        from music_cli.storage.state import PlayedTrack

        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 8)
            history = app.query_one(HistoryList)
            assert history.index is None

            # A pushed track with a fresh id triggers a re-read of the store.
            store.record(PlayedTrack("new1", "New Track", ("N",)))
            _push_playing(app, track_id="new1", title="New Track")
            await _settle(pilot)
            assert history.track_at(0).video_id == "new1"

            # The same track id again does not re-render history.
            store.record(PlayedTrack("other", "Other", ("O",)))
            _push_playing(app, track_id="new1", title="New Track")
            await _settle(pilot)
            assert history.track_at(0).video_id == "new1"

    _run(scenario())


def test_idle_status_shows_last_played_track(monkeypatch):
    """An idle daemon (track=None) shows the last history track, not a blank bar."""
    from music_cli.storage.state import PlayedTrack, PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    install_daemon(monkeypatch)

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        store.record(PlayedTrack("last1", "Last Track", ("Some Artist",), 180.0))
        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            _push(app, IDLE_STATUS)  # daemon idle event would previously wipe the bar
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Last Track"
            assert str(np.query_one("#np-icon").content) == "⏸"

    _run(scenario())


def test_mode_toogles_send_ipc(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            np = app.query_one(NowPlaying)

            status = dict(STATUS)
            status["loop"] = True
            status["auto_next"] = False
            _push(app, status)
            assert np.query_one("#np-loop").has_class("on")
            assert np.query_one("#np-auto").has_class("off")

            app.action_toggle_loop()
            app.action_toggle_auto_next()
            app.action_toggle_mute()
            await _settle(pilot)
            toggles = [
                r
                for r in fake.requests
                if r["cmd"] in ("loop", "auto_next", "mute")
                and r.get("state") == "toggle"
            ]
            assert {r["cmd"] for r in toggles} == {"loop", "auto_next", "mute"}

    _run(scenario())


def test_volume_mute_reflect_pushed_status(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            np = app.query_one(NowPlaying)
            status = dict(STATUS)
            status["volume"] = 45
            status["muted"] = True
            _push(app, status)
            assert str(np.query_one("#np-volume").content) == "Vol 45% · muted"

    _run(scenario())


def test_mount_render_nothing_playing_when_daemon_idle(monkeypatch):
    from music_cli.storage.state import PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    install_daemon(monkeypatch, status=IDLE_STATUS)

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 10)
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Nothing playing"

    _run(scenario())


def test_mount_shows_last_history_paused_when_idle(monkeypatch):
    from music_cli.storage.state import PlayedTrack, PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    install_daemon(monkeypatch, status=IDLE_STATUS)

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        store.record(PlayedTrack("last1", "Last One", ("L",), duration=180.0))
        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 10)
            np = app.query_one(NowPlaying)
            assert str(np.query_one("#np-title").content) == "Last One"

    _run(scenario())


def test_playback_failure_reported_and_history_untouched(monkeypatch):
    from music_cli.storage.state import PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    fake = install_daemon(monkeypatch)
    fake.ok = False

    async def scenario():
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        app = MusicTUI(make_client(), history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.play_result(make_result(0, "v1"))
            await _settle(pilot)
            assert store.most_recent() is None
            assert (
                str(app.query_one(NowPlaying).query_one("#np-status").content)
                == "Playback failed"
            )

    _run(scenario())


def test_volume_up_sends_delta_5(monkeypatch):
    from music_cli.tui.app import MusicTUI

    fake = install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            before = len(fake.requests)
            app.action_volume_up()
            await _settle(pilot)
            new = fake.requests[before:]
            assert {"cmd": "volume", "delta": 5} in new

    _run(scenario())


def test_library_tree_renders_playlists(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            tree = app.query_one(LibraryTree)
            assert tree.id == "library-tree"
            assert tree.can_focus
            playlists = list(tree.root.children)
            assert [node.data["kind"] for node in playlists] == ["playlist", "playlist"]
            assert "My Mix" in str(playlists[0].label.plain)

    _run(scenario())


def test_library_tree_sign_in_notice_when_unauthenticated(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        client.library = FakeLibrary(authenticated=False)
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one(LibraryTree)
            assert "Sign in" in str(tree.root.children[0].label.plain)

    _run(scenario())


def test_library_tree_expand_loads_tracks(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree

    install_daemon(monkeypatch)

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


def test_narrow_layout_hides_side_panes(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import ResultsTable

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            playlist = app.query_one("#playlist-pane")
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


def test_search_edges_jump_to_side_panes(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree, QueueList, ResultsTable

    install_daemon(monkeypatch)

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


def test_waveform_widget_tracks_playback_state(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying
    from music_cli.tui.components.waveform import Waveform

    install_daemon(monkeypatch)

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
            assert not waveform._active
            # The waveform stays flat until playback actually starts.
            await pilot.pause(0.3)
            assert "▁" in waveform.render().plain
            np.set_progress(0.5, 213.0)
            assert waveform._active
            await pilot.pause(0.3)
            animated = waveform.render()
            assert len(animated.plain.splitlines()) == 3
            assert any(block in animated.plain for block in "▂▃▄▅▆▇█")

            # Pausing freezes the animation; resuming advances it again.
            np.set_paused(True)
            await pilot.pause(0.7)
            assert waveform._paused
            frozen_time = waveform._time
            await pilot.pause(0.3)
            assert waveform._time == frozen_time

            np.set_paused(False)
            await pilot.pause(0.7)
            assert not waveform._paused
            assert waveform._time > frozen_time

            np.clear_track()
            assert not waveform._active
            assert "▁" in waveform.render().plain

    _run(scenario())


def test_lyrics_survive_status_heartbeats(monkeypatch):
    """A heartbeat re-set_track for the same track must not wipe lyrics.

    The lyric worker finishes after the first status push; the next heartbeat
    used to reset _lyrics to [] and the bar showed "♪ ♪ ♪" forever.
    """
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components.now_playing import NowPlaying

    install_daemon(monkeypatch)

    def status_for(position):
        return {
            "state": "playing",
            "track": {
                "video_id": "abc",
                "title": "Some Song",
                "artists": ["Some Artist"],
                "duration": 200.0,
            },
            "position": position,
            "duration": 200.0,
            "volume": 80,
            "muted": False,
            "loop": False,
            "auto_next": True,
            "queue": [],
        }

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 6)
            _push(app, status_for(1.0))
            await _settle(pilot)
            np = app.query_one(NowPlaying)
            # Lyric worker finishes and hands over the synced lines.
            np.set_lyrics([(0.0, "first line"), (5.0, "second line")])
            np.set_progress(6.0, 200.0)
            lyric = lambda: str(np.query_one("#np-lyric").content)
            assert lyric() == "second line"
            # Next heartbeat for the same track: lyrics must survive.
            _push(app, status_for(7.0))
            await _settle(pilot)
            assert lyric() == "second line"
            # A genuinely different track still resets.
            _push(
                app,
                {
                    **status_for(0.0),
                    "track": {
                        "video_id": "xyz",
                        "title": "Other Song",
                        "artists": ["Other Artist"],
                        "duration": 100.0,
                    },
                },
            )
            await _settle(pilot)
            assert lyric() == "♪ ♪ ♪"

    _run(scenario())


def test_add_to_playlist_from_results(monkeypatch):
    from textual.widgets import SelectionList

    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import ResultsTable
    from music_cli.tui.screens.modals import AddToPlaylistScreen

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            search = app.query_one("#search-input")
            search.value = "coldplay"
            await pilot.pause(0.6)
            results = app.query_one(ResultsTable)
            results.focus()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, AddToPlaylistScreen)
            app.screen.query_one(SelectionList).toggle("p1")
            await pilot.press("escape")
            for _ in range(4):
                await pilot.pause()
            assert client.library.add_calls == [("p1", ["v0"])]
            assert len(client.library.playlist_calls) == 2

    _run(scenario())


def test_add_to_playlist_from_queue(monkeypatch):
    from textual.widgets import SelectionList

    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import QueueList
    from music_cli.tui.screens.modals import AddToPlaylistScreen

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            queue = app.query_one(QueueList)
            queue.set_tracks([PlaylistTrack(video_id="t1", title="One", artists=["A"])])
            queue.focus()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, AddToPlaylistScreen)
            app.screen.query_one(SelectionList).toggle("p2")
            await pilot.press("escape")
            for _ in range(4):
                await pilot.pause()
            assert client.library.add_calls == [("p2", ["t1"])]

    _run(scenario())


def test_remove_track_from_playlist_tree(monkeypatch):
    from textual.widgets import Button

    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree
    from music_cli.tui.screens.modals import ConfirmScreen

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
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
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            app.screen.query_one("#confirm-modal-yes", Button).press()
            for _ in range(4):
                await pilot.pause()
            assert client.library.remove_calls == [("p1", "p1t1")]

    _run(scenario())


def test_create_playlist_from_tree(monkeypatch):
    from textual.widgets import Button, Input

    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree
    from music_cli.tui.screens.modals import PlaylistNameScreen

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            tree = app.query_one(LibraryTree)
            tree.focus()
            await pilot.press("down")
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, PlaylistNameScreen)
            app.screen.query_one(Input).value = "My New Mix"
            app.screen.query_one("#name-modal-save", Button).press()
            for _ in range(4):
                await pilot.pause()
            assert client.library.create_calls == ["My New Mix"]

    _run(scenario())


def test_rename_playlist_from_tree(monkeypatch):
    from textual.widgets import Button, Input

    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree
    from music_cli.tui.screens.modals import PlaylistNameScreen

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(10):
                await pilot.pause()
            tree = app.query_one(LibraryTree)
            tree.focus()
            await pilot.press("down")
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, PlaylistNameScreen)
            name_input = app.screen.query_one(Input)
            assert name_input.value == "My Mix"
            name_input.value = "Renamed Mix"
            app.screen.query_one("#name-modal-save", Button).press()
            for _ in range(4):
                await pilot.pause()
            assert client.library.rename_calls == [("p1", "Renamed Mix")]

    _run(scenario())


def test_playlist_keybinds_show_only_with_selection(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree, ResultsTable

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            results = app.query_one(ResultsTable)
            results.focus()
            await pilot.pause()
            assert "s" not in app.screen.active_bindings  # focused but no rows

            search = app.query_one("#search-input")
            search.value = "coldplay"
            await pilot.pause(0.6)
            results.focus()
            await pilot.pause()
            assert "s" in app.screen.active_bindings

            tree = app.query_one(LibraryTree)
            for _ in range(10):
                await pilot.pause()
            tree.focus()
            await pilot.press("down")
            await pilot.pause()
            bindings = app.screen.active_bindings
            assert "c" in bindings and "r" in bindings
            assert "s" not in bindings and "d" not in bindings

            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            bindings = app.screen.active_bindings
            assert "s" in bindings and "d" in bindings
            assert "r" not in bindings and "c" not in bindings

    _run(scenario())


def test_playlist_keybinds_hidden_when_unauthenticated(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import LibraryTree, ResultsTable

    install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        client.library = FakeLibrary(authenticated=False)
        app = MusicTUI(client)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            search = app.query_one("#search-input")
            search.value = "coldplay"
            await pilot.pause(0.6)
            results = app.query_one(ResultsTable)
            results.focus()
            await pilot.pause()
            assert "s" not in app.screen.active_bindings

            tree = app.query_one(LibraryTree)
            tree.focus()
            await pilot.press("down")
            await pilot.pause()
            bindings = app.screen.active_bindings
            assert "c" not in bindings and "r" not in bindings
            assert "s" not in bindings and "d" not in bindings

    _run(scenario())


def test_history_panel_renders_and_plays(monkeypatch):
    from music_cli.storage.state import PlayedTrack, PlayHistoryStore
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import HistoryList

    fake = install_daemon(monkeypatch)

    async def scenario():
        client = make_client()
        store = PlayHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        for i in ("v0", "v1", "v2"):
            store.record(PlayedTrack(i, f"Track {i}", (f"A{i}",)))
        app = MusicTUI(client, history_store=store)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot, 8)
            history = app.query_one(HistoryList)
            # Newest first from the store (dedup is a store concern).
            assert history.track_at(0).video_id == "v2"
            assert history._tracks[0].video_id == "v2"

            # Selecting a history row replays it (newest-first: row 1 = v1).
            history.focus()
            history.index = 1
            app._on_history_selected(HistoryList.Selected(history, None, index=1))
            await _settle(pilot)
            plays = [r for r in fake.requests if r["cmd"] == "play"]
            assert plays
            assert plays[-1]["video_id"] == "v1"

    _run(scenario())


def test_search_empty_clears_results(monkeypatch):
    from music_cli.tui.app import MusicTUI
    from music_cli.tui.components import ResultsTable

    install_daemon(monkeypatch)

    async def scenario():
        app = MusicTUI(make_client())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            search = app.query_one("#search-input")
            search.value = ""
            await pilot.pause(0.6)
            assert app.query_one(ResultsTable).row_count == 0

            search.value = "coldplay"
            await pilot.pause(0.6)
            assert app.query_one(ResultsTable).row_count == 3

    _run(scenario())


def test_parse_lrc_synced_lines():
    from music_cli.yt.lyrics import parse_lrc

    lrc = "[00:05.50]first\n[00:12]second line\nnot timed\n[01:02.3]later"
    assert parse_lrc(lrc) == [
        (5.0, "first"),
        (12.0, "second line"),
        (62.0, "later"),
    ]
    assert parse_lrc("no timestamps here") == []
