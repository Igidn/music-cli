from __future__ import annotations

import pytest

from music_cli.client import MusicClient
from music_cli.player import PlayerError, PlaylistTrack, StreamExtractor, StreamInfo


def make_result(video_id="abc", title="Some Song", artists=("Some Artist",)):
    from music_cli.search import SearchResult

    return SearchResult(
        result_type="song",
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
        return StreamInfo(video_id=video_id, title=f"Stream {video_id}", stream_url="https://u")


class FakePlayer:
    def __init__(self):
        self.played = []
        self.volume = 80
        self.position = 0.0
        self.duration = None
        self.playing = False
        self.eof_reached = False

    def play(self, stream):
        self.played.append(stream)
        self.position = 0.5
        self.duration = 200.0
        self.playing = True


class FakeWatch:
    def __init__(self, tracks):
        self._tracks = tracks
        self.calls = []

    def get(self, video_id=None, playlist_id=None, radio=False, shuffle=False, limit=None):
        self.calls.append((video_id, radio))
        return self._tracks


class FakeSearch:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def search(self, query, limit=20, filter=None):
        self.calls.append((query, limit, filter))
        return self._results


@pytest.fixture
def client():
    c = MusicClient.__new__(MusicClient)
    c.extractor = FakeExtractor()
    c.player = FakePlayer()
    c.queue = []
    c.current = None
    c._cookies = None
    c._extractor_factory = StreamExtractor
    return c


class TestMusicClient:
    def test_search_delegates(self, client):
        client.search_api = FakeSearch([make_result()])
        results = client.search("hello", limit=10, filter="songs")
        assert results[0].title == "Some Song"
        assert client.search_api.calls == [("hello", 10, "songs")]

    def test_play_result_resolves_and_plays(self, client):
        stream = client.play_result(make_result())
        assert stream.video_id == "abc"
        assert client.player.played == [stream]
        assert client.current is stream

    def test_play_track_resolves_and_plays(self, client):
        stream = client.play_track(make_track())
        assert stream.video_id == "t1"
        assert client.player.played == [stream]
        assert client.current is stream

    def test_load_queue_drops_current_track(self, client):
        client.watch = FakeWatch([make_track("t0"), make_track("t1"), make_track("t2")])
        queue = client.load_queue("t0")
        assert [t.video_id for t in queue] == ["t1", "t2"]
        assert client.watch.calls == [("t0", False)]

    def test_load_queue_radio(self, client):
        client.watch = FakeWatch([make_track("t1")])
        client.load_queue("t0", radio=True)
        assert client.watch.calls == [("t0", True)]

    def test_load_queue_keeps_tracks_when_none_match(self, client):
        client.watch = FakeWatch([make_track("x1"), make_track("x2")])
        queue = client.load_queue("t0")
        assert [t.video_id for t in queue] == ["x1", "x2"]

    def test_next_plays_next_and_pops(self, client):
        client.queue = [make_track("t1"), make_track("t2")]
        track = client.next()
        assert track.video_id == "t1"
        assert client.queue == [make_track("t2")]
        assert client.player.played[0].video_id == "t1"

    def test_next_empty_queue_returns_none(self, client):
        assert client.next() is None

    def test_next_restores_track_on_failure(self, client):
        client.queue = [make_track("t1")]

        class BrokenExtractor:
            def resolve(self, video_id):  # noqa: ARG002
                raise PlayerError("nope")

        client.extractor = BrokenExtractor()
        client._extractor_factory = lambda *args, **kwargs: BrokenExtractor()
        with pytest.raises(PlayerError):
            client.next()
        assert [t.video_id for t in client.queue] == ["t1"]
