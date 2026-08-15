from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from music_cli.client import MusicClient
from music_cli.core.errors import PlayerError
from music_cli.session import PlaybackSession
from music_cli.storage.cache import AudioCache
from music_cli.storage.state import (
    SETTING_AUTO_NEXT,
    SETTING_LOOP,
    SETTING_MUTED,
    SETTING_VOLUME,
    PlayedTrack,
    PlayHistoryStore,
    SettingsStore,
)
from music_cli.yt.extract import PlaylistTrack, StreamExtractor, StreamInfo
from music_cli.yt.search import SearchResult


def make_result(video_id="abc", title="Some Song", artists=("Some Artist",)):
    return SearchResult(
        result_type="song" if video_id else "artist",
        title=title,
        artists=list(artists),
        album="An Album",
        duration="3:21",
        video_id=video_id,
        browse_id="",
        year="2024",
        raw={},
    )


def make_track(video_id="t1", title="Track One", artists=("Artist A",)):
    return PlaylistTrack(video_id=video_id, title=title, artists=list(artists))


class FakeExtractor:
    def __init__(self):
        self.resolved = []

    def resolve(self, video_id):
        self.resolved.append(video_id)
        return StreamInfo(
            video_id=video_id, title=f"Stream {video_id}", stream_url="https://u"
        )

    def download(self, video_id, target):
        path = Path(f"{target}.m4a")
        path.write_bytes(b"audio")
        return str(path)


class FakePlayer:
    def __init__(self):
        self.played = []
        self._volume = 80
        self.muted = False
        self.loop = False
        self.position = 0.0
        self.duration = None
        self.playing = False
        self.paused = True
        self.seek_calls = []

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = max(0, min(100, value))

    def play(self, stream):
        self.played.append(stream)
        self.position = 0.5
        self.duration = 200.0
        self.playing = True
        self.paused = False

    def pause(self):
        self.paused = True
        self.playing = False

    def resume(self):
        self.paused = False
        self.playing = True

    def toggle(self):
        self.pause() if not self.paused else self.resume()

    def seek(self, seconds):
        self.seek_calls.append(("seek", seconds))
        self.position = seconds

    def seek_relative(self, delta):
        self.seek_calls.append(("relative", delta))
        self.position += delta

    def pump(self):
        pass

    def stop(self):
        self.paused = True
        self.playing = False
        self.position = 0.0
        self.duration = None

    def close(self):
        pass


class FakeWatch:
    """Serves up-next tracks; can fail or only answer radio requests."""

    def __init__(self, tracks=(), radio_tracks=None, error=None):
        self._tracks = list(tracks)
        self._radio_tracks = radio_tracks
        self._error = error
        self.calls = []

    def get(
        self, video_id=None, playlist_id=None, radio=False, shuffle=False, limit=None
    ):
        self.calls.append((video_id, radio))
        if self._error is not None:
            raise self._error
        if radio and self._radio_tracks is not None:
            return self._radio_tracks
        return self._tracks


class FakeSearch:
    def __init__(self, results):
        self._results = results

    def search(self, query, limit=20, filter=None):
        return self._results


class FakeLibrary:
    def __init__(self, tracks=()):
        self._tracks = list(tracks)

    def tracks(self, playlist_id):
        return self._tracks

    def album_tracks(self, browse_id):
        return self._tracks


@pytest.fixture
def client(tmp_path):
    c = MusicClient.__new__(MusicClient)
    c.search_api = FakeSearch([])
    c.extractor = FakeExtractor()
    c.watch = FakeWatch()
    c.library = FakeLibrary()
    c.player = FakePlayer()
    c.queue = []
    c.current = None
    c._playlist = []
    c._cookies = None
    c._extractor_factory = StreamExtractor
    c._in_flight = set()
    c._play_lock = threading.Lock()
    c.cache = AudioCache(directory=tmp_path / "cache")
    return c


@pytest.fixture
def history():
    store = PlayHistoryStore()
    yield store
    store.close()


@pytest.fixture
def settings():
    store = SettingsStore()
    yield store
    store.close()


@pytest.fixture
def session(client, history, settings):
    return PlaybackSession(client, history, settings)


class TestSettingsRestore:
    def test_restores_saved_settings_onto_player(self, client, settings):
        settings.set(SETTING_VOLUME, 42)
        settings.set(SETTING_MUTED, True)
        settings.set(SETTING_LOOP, True)
        settings.set(SETTING_AUTO_NEXT, False)
        session = PlaybackSession(client, settings_store=settings)
        assert client.player.volume == 42
        assert client.player.muted is True
        assert client.loop is True
        assert session.auto_next is False

    def test_defaults_hold_on_a_fresh_store(self, client):
        session = PlaybackSession(client)
        assert client.player.volume == 80
        assert client.player.muted is False
        assert client.loop is False
        assert session.auto_next is True
        assert session.last_video_id == ""


class TestPlay:
    def test_play_query_skips_results_without_video_id(self, client, session, history):
        client.search_api = FakeSearch(
            [make_result(video_id="", title="An Artist"), make_result("v1")]
        )
        client.watch = FakeWatch([make_track("v1"), make_track("t1"), make_track("t2")])
        stream = session.play_query("some song")
        assert stream.video_id == "v1"
        assert client.player.played == [stream]
        assert session.last_video_id == "v1"
        assert [t.video_id for t in client.queue] == ["t1", "t2"]
        saved = history.most_recent()
        assert saved.video_id == "v1"
        assert saved.title == "Stream v1"

    def test_play_query_raises_when_nothing_playable(self, client, session):
        client.search_api = FakeSearch([make_result(video_id="")])
        with pytest.raises(PlayerError, match="No playable results"):
            session.play_query("nobody")
        assert not client.player.played

    def test_play_video_records_and_loads_queue(self, client, session, history):
        client.watch = FakeWatch([make_track("v1"), make_track("t1")])
        stream = session.play_video("v1", "Title")
        assert stream.video_id == "v1"
        assert history.most_recent().video_id == "v1"
        assert [t.video_id for t in client.queue] == ["t1"]

    def test_play_video_tolerates_queue_fetch_failure(self, client, session, history):
        client.watch = FakeWatch(error=PlayerError("boom"))
        stream = session.play_video("v1", "Title")
        assert stream.video_id == "v1"
        assert client.queue == []
        assert history.most_recent().video_id == "v1"

    def test_play_playlist_plays_first_and_queues_rest(self, client, session, history):
        client.library = FakeLibrary([make_track("p1"), make_track("p2")])
        stream = session.play_playlist("PL")
        assert stream.video_id == "p1"
        assert [t.video_id for t in client.queue] == ["p2"]
        assert history.most_recent().video_id == "p1"

    def test_play_album_plays_first_and_queues_rest(self, client, session, history):
        client.library = FakeLibrary([make_track("a1"), make_track("a2")])
        stream = session.play_album("MPREb_x")
        assert stream.video_id == "a1"
        assert [t.video_id for t in client.queue] == ["a2"]
        assert history.most_recent().video_id == "a1"

    def test_play_album_empty_raises(self, client, session):
        client.library = FakeLibrary([])
        with pytest.raises(PlayerError, match="Album is empty"):
            session.play_album("MPREb_x")

    def test_resume_last_returns_none_on_empty_history(self, session):
        assert session.resume_last() is None

    def test_resume_last_plays_most_recent(self, client, session, history):
        history.record(PlayedTrack(video_id="old", title="Old"))
        history.record(PlayedTrack(video_id="new", title="New"))
        stream = session.resume_last()
        assert stream.video_id == "new"
        assert client.player.played[-1].video_id == "new"
        assert history.most_recent().played == 2


class TestNextTrack:
    def test_advances_the_queue(self, client, session, history):
        client.queue = [make_track("t1"), make_track("t2")]
        track = session.next_track()
        assert track.video_id == "t1"
        assert client.player.played[-1].video_id == "t1"
        assert [t.video_id for t in client.queue] == ["t2"]
        assert history.most_recent().video_id == "t1"
        assert session.last_video_id == "t1"

    def test_loops_an_exhausted_playlist(self, client, session):
        client._playlist = [make_track("p1"), make_track("p2")]
        track = session.next_track()
        assert track.video_id == "p1"
        assert [t.video_id for t in client.queue] == ["p2"]

    def test_refills_via_radio_when_queue_is_empty(self, client, session):
        session.last_video_id = "cur"
        client.watch = FakeWatch(radio_tracks=[make_track("cur"), make_track("r1")])
        track = session.next_track()
        assert track.video_id == "r1"
        assert client.watch.calls == [("cur", True)]

    def test_radio_refill_failure_returns_none(self, client, session):
        session.last_video_id = "cur"
        client.watch = FakeWatch(error=PlayerError("boom"))
        assert session.next_track() is None

    def test_returns_none_when_nothing_anywhere(self, session):
        assert session.next_track() is None

    def test_play_errors_propagate(self, client, session):
        class BrokenExtractor:
            def resolve(self, video_id):
                raise PlayerError("nope")

        client.queue = [make_track("t1")]
        client.extractor = BrokenExtractor()
        client._extractor_factory = lambda *args, **kwargs: BrokenExtractor()
        with pytest.raises(PlayerError):
            session.next_track()

    def test_on_track_end_respects_auto_next_off(self, client, session):
        session.set_auto_next("off")
        client.current = StreamInfo(video_id="v0", title="T", stream_url="u")
        client.queue = [make_track("t1")]
        session.on_track_end()
        assert client.current is None
        assert not client.player.played

    def test_on_track_end_advances_when_on(self, client, session):
        client.current = StreamInfo(video_id="v0", title="T", stream_url="u")
        client.queue = [make_track("t1")]
        session.on_track_end()
        assert client.player.played[-1].video_id == "t1"


def _wait_cached(cache, video_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cache.path_for(video_id) is not None:
            return True
        time.sleep(0.02)
    return False


class TestPrefetchNext:
    def test_prefetches_only_next_up_track(self, client, session):
        client.queue = [make_track("next1"), make_track("next2")]
        session.record(
            StreamInfo(video_id="cur", title="Cur", stream_url="u")
        )
        assert _wait_cached(client.cache, "next1")
        assert client.cache.path_for("next2") is None

    def test_skipped_when_auto_next_off(self, client, session):
        session.set_auto_next("off")
        client.queue = [make_track("t1")]
        session.record(StreamInfo(video_id="cur", title="Cur", stream_url="u"))
        time.sleep(0.1)
        assert client.cache.path_for("t1") is None

    def test_skipped_when_queue_empty(self, client, session):
        client.queue = []
        session.record(StreamInfo(video_id="cur", title="Cur", stream_url="u"))
        assert client.cache.path_for("cur") is None


class TestControls:
    def test_transport_passthroughs(self, client, session):
        client.current = StreamInfo(video_id="v0", title="T", stream_url="u")
        session.pause()
        assert client.player.paused is True
        session.resume()
        assert client.player.paused is False
        session.toggle()
        assert client.player.paused is True

    def test_toggle_resumes_last_track_when_idle(self, client, session, history):
        history.record(PlayedTrack(video_id="old", title="Old"))
        client.current = None
        session.toggle()
        assert client.player.played[-1].video_id == "old"
        assert client.player.paused is False

    def test_seek_offset_and_position(self, client, session):
        client.current = StreamInfo(video_id="v0", title="T", stream_url="u")
        session.seek(position=42.0)
        session.seek(offset=5.0)
        assert client.player.seek_calls == [("seek", 42.0), ("relative", 5.0)]
        assert client.player.position == 47.0

    def test_seek_ignored_without_current_track(self, client, session):
        session.seek(position=42.0)
        session.seek(offset=5.0)
        assert client.player.seek_calls == []

    def test_set_volume_absolute_and_delta(self, client, session, settings):
        assert session.set_volume(volume=30) == 30
        assert settings.get_int(SETTING_VOLUME) == 30
        assert session.set_volume(delta=10) == 40
        assert settings.get_int(SETTING_VOLUME) == 40
        assert session.set_volume(delta=-100) == 0  # clamped by the player
        assert settings.get_int(SETTING_VOLUME) == 0

    def test_set_muted(self, client, session, settings):
        assert session.set_muted("on") is True
        assert client.player.muted is True
        assert settings.get_bool(SETTING_MUTED) is True
        assert session.set_muted("toggle") is False
        assert client.player.muted is False
        assert settings.get_bool(SETTING_MUTED) is False
        assert session.set_muted("off") is False

    def test_set_loop(self, client, session, settings):
        assert session.set_loop("on") is True
        assert client.loop is True
        assert settings.get_bool(SETTING_LOOP) is True
        assert session.set_loop("toggle") is False
        assert client.loop is False
        assert settings.get_bool(SETTING_LOOP) is False
        assert session.set_loop("off") is False

    def test_set_auto_next(self, session, settings):
        assert session.set_auto_next("off") is False
        assert session.auto_next is False
        assert settings.get_bool(SETTING_AUTO_NEXT) is False
        assert session.set_auto_next("toggle") is True
        assert session.auto_next is True
        assert settings.get_bool(SETTING_AUTO_NEXT) is True
        assert session.set_auto_next("on") is True

    def test_invalid_state_rejected(self, session):
        with pytest.raises(ValueError, match="Invalid state"):
            session.set_muted("maybe")


class TestStatus:
    def test_stopped(self, client, session):
        status = session.status()
        assert status == {
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

    def test_playing(self, client, session):
        client.watch = FakeWatch(
            [make_track("v1"), make_track("t1", artists=("A", "B"))]
        )
        session.play_video("v1", "Title")
        status = session.status()
        assert status["state"] == "playing"
        assert status["track"] == {
            "video_id": "v1",
            "title": "Stream v1",
            "artists": [],
            "duration": None,
        }
        assert status["position"] == 0.5
        assert status["duration"] == 200.0
        assert status["queue"] == [
            {
                "video_id": "t1",
                "title": "Track One",
                "artists": ["A", "B"],
                "duration": "",
            }
        ]
        session.pause()
        assert session.status()["state"] == "paused"

    def test_queue_dict_keys(self, client, session):
        client.queue = [make_track("t1", artists=("A",))]
        assert session.queue() == [
            {"video_id": "t1", "title": "Track One", "artists": ["A"], "duration": ""}
        ]
