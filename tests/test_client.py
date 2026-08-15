from __future__ import annotations

import threading
from pathlib import Path

import pytest

from music_cli.client import MusicClient
from music_cli.core.errors import PlayerError
from music_cli.storage.cache import AudioCache, TrackMeta
from music_cli.yt.extract import PlaylistTrack, StreamExtractor, StreamInfo
from music_cli.yt.search import parse_artists, parse_search_result


def make_result(video_id="abc", title="Some Song", artists=("Some Artist",)):
    from music_cli.yt.search import SearchResult

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
        return StreamInfo(
            video_id=video_id, title=f"Stream {video_id}", stream_url="https://u"
        )


class FakePlayer:
    def __init__(self):
        self.played = []
        self.volume = 80
        self.position = 0.0
        self.duration = None
        self.playing = False
        self.paused = True
        self.eof_reached = False

    def play(self, stream):
        self.played.append(stream)
        self.position = 0.5
        self.duration = 200.0
        self.playing = True
        self.paused = False

    def stop(self):
        self.playing = False
        self.paused = True
        self.position = 0.0
        self.duration = None

    def pump(self):
        pass


class FakeWatch:
    def __init__(self, tracks):
        self._tracks = tracks
        self.calls = []

    def get(
        self, video_id=None, playlist_id=None, radio=False, shuffle=False, limit=None
    ):
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
def client(tmp_path):
    c = MusicClient.__new__(MusicClient)
    c.extractor = FakeExtractor()
    c.player = FakePlayer()
    c.queue = []
    c.current = None
    c._cookies = None
    c._extractor_factory = StreamExtractor
    c._in_flight = set()
    c._play_lock = threading.Lock()
    c.cache = AudioCache(directory=tmp_path / "cache")
    return c


def seed_cache(client, video_id="abc", *, title="Cached Title"):
    target = client.cache.tmp_path(video_id)
    src = Path(f"{target}.m4a")
    src.write_bytes(b"audio-bytes")
    return client.cache.commit(
        video_id,
        TrackMeta(title=title, artists=("Cached Artist",), duration=120.0, ext="m4a"),
        src=src,
    )


class FlakyPlayer(FakePlayer):
    """FakePlayer whose first play call fails to start playback."""

    def __init__(self):
        super().__init__()
        self._failed_once = False

    def play(self, stream):
        if not self._failed_once:
            self._failed_once = True
            self.playing = False
            self.position = 0.0
            self.duration = None
            return
        super().play(stream)


class LoadedButPausedPlayer(FakePlayer):
    """FakePlayer whose item loads (duration known) but never starts playing.

    Replicates the auto-next race: the end-of-item pause dropped the play
    request, so the new item is loaded while the rate stays at zero.
    """

    def play(self, stream):
        self.played.append(stream)
        self.position = 0.0
        self.duration = 200.0
        self.playing = True
        self.paused = True


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
            def resolve(self, video_id):
                raise PlayerError("nope")

        client.extractor = BrokenExtractor()
        client._extractor_factory = lambda *args, **kwargs: BrokenExtractor()
        with pytest.raises(PlayerError):
            client.next()
        assert [t.video_id for t in client.queue] == ["t1"]

    def test_play_result_ignores_duplicate_already_playing(self, client):
        first = client.play_result(make_result())
        second = client.play_result(make_result())
        assert second is first
        assert client.extractor.resolved == ["abc"]
        assert len(client.player.played) == 1

    def test_play_result_dedupes_concurrent_requests(self, client):
        started = threading.Event()
        release = threading.Event()

        class SlowExtractor(FakeExtractor):
            def resolve(self, video_id):
                started.set()
                release.wait(timeout=5)
                return super().resolve(video_id)

        client.extractor = SlowExtractor()
        errors = []

        def play():
            try:
                client.play_result(make_result())
            except PlayerError as error:
                errors.append(error)

        thread = threading.Thread(target=play)
        thread.start()
        assert started.wait(5)
        try:
            assert client.play_result(make_result()) is None
        finally:
            release.set()
        thread.join(10)
        assert not errors
        assert client.extractor.resolved == ["abc"]
        assert client.current.video_id == "abc"

    def test_play_result_allows_different_video_after_previous(self, client):
        client.play_result(make_result("abc"))
        client.play_result(make_result("xyz"))
        assert client.extractor.resolved == ["abc", "xyz"]
        assert len(client.player.played) == 2


class TestCacheIntegration:
    def test_play_result_uses_cached_track_without_extraction(self, client):
        seed_cache(client)
        stream = client.play_result(make_result("abc"))
        assert stream.title == "Cached Title"
        assert client.extractor.resolved == []
        assert client.current is stream
        assert len(client.player.played) == 1

    def test_play_result_invalidates_broken_cache_and_redownloads(self, client):
        seed_cache(client)
        client.player = FlakyPlayer()
        stream = client.play_result(make_result("abc"))
        assert client.cache.lookup("abc") is None
        assert client.extractor.resolved == ["abc"]
        assert stream.video_id == "abc"
        assert client.current.video_id == "abc"

    def test_playback_started_false_when_loaded_but_paused(self, client):
        """A loaded item with the player still paused is not playback.

        After a track ends the end-of-item pause can drop the next play
        request; the item reports a duration but the rate is zero. That must
        not count as started, or the queue advances silently.
        """
        client.player = LoadedButPausedPlayer()
        client.player.play(
            StreamInfo(video_id="abc", title="T", stream_url="https://u")
        )
        assert client._playback_started(timeout=0.5) is False

    def test_prefetch_populates_cache(self, client):
        class DownloadingExtractor(FakeExtractor):
            def download(self, video_id, outtmpl):
                path = f"{outtmpl}.m4a"
                Path(path).write_bytes(b"audio")
                return path

        client.extractor = DownloadingExtractor()
        assert client.prefetch("xyz") is True
        track = client.cache.lookup("xyz")
        assert track is not None
        assert track.title == "Stream xyz"
        assert track.ext == "m4a"

    def test_prefetch_is_noop_when_already_cached(self, client):
        seed_cache(client, video_id="xyz")
        assert client.prefetch("xyz") is True
        assert client.extractor.resolved == []

    def test_parse_artists_dedupes_repeats_in_order(self):
        info = {
            "artists": [
                {"name": "A"},
                {"name": "B"},
                {"name": "B"},
                {"name": "A"},
            ]
        }
        assert parse_artists(info) == ["A", "B"]

    def test_parse_artists_reads_singular_artist_key(self):
        # Artist-filtered ytmusicapi rows carry the name as a plain string.
        assert parse_artists({"artist": "Tame Impala"}) == ["Tame Impala"]
        assert parse_artists({"artist": {"name": "Tame Impala"}}) == ["Tame Impala"]

    def test_parse_search_result_uses_artist_key_for_artist_rows(self):
        # ytmusicapi artist rows have no `title`; without a fallback the
        # results table showed "Unknown" with an empty Artist column.
        result = parse_search_result(
            {
                "resultType": "artist",
                "artist": "Tame Impala",
                "browseId": "UCGz-eguN8tcic5kUG4s1ZgA",
            }
        )
        assert result.title == "Tame Impala"
        assert result.artists == ["Tame Impala"]

    def test_parse_search_result_titles_artist_top_result_from_artists(self):
        # Unfiltered search's top artist row: `title` missing but `artists`
        # is parsed by ytmusicapi.
        result = parse_search_result(
            {
                "resultType": "artist",
                "artists": [{"name": "Radiohead", "id": "UCq0Ow..."}],
            }
        )
        assert result.title == "Radiohead"
        assert result.artists == ["Radiohead"]
