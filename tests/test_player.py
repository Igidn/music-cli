from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yt_dlp
from AVFoundation import (
    AVPlayerActionAtItemEndPause,
    CMTimeGetSeconds,
    CMTimeMakeWithSeconds,
)

from music_cli.cache import AudioCache, TrackMeta
from music_cli.player import (
    AVFoundationPlayer,
    Cookies,
    LocalFile,
    PlayerError,
    PlaylistTrack,
    StreamExtractor,
    StreamInfo,
    WatchPlaylist,
    _default_fetch_stream,
    parse_watch_track,
)

NETSCAPE_COOKIE_FILE = """\
# Netscape HTTP Cookie File
#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1820768484\t__Secure-3PSID\tg.a000secret
.youtube.com\tTRUE\t/\tTRUE\t1820768484\tSID\tanothersecret
.com\tTRUE\t/\tTRUE\t1820768484\tSOCS\tCAI
"""


@pytest.fixture
def cookie_file(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE_COOKIE_FILE)
    return str(path)


class TestCookies:
    def test_from_file(self, cookie_file):
        cookies = Cookies.from_file(cookie_file)
        assert cookies.cookiefile == cookie_file
        assert cookies.cookiesfrombrowser is None
        assert cookies.enabled

    def test_from_browser(self):
        cookies = Cookies.from_browser("firefox", "default", "BASICTEXT")
        assert cookies.cookiesfrombrowser == ("firefox", "default", "BASICTEXT")

    def test_from_browser_unknown_raises(self):
        with pytest.raises(PlayerError, match="Unsupported browser"):
            Cookies.from_browser("netscape")

    def test_empty_disabled(self):
        assert not Cookies().enabled

    def test_ydl_options_manual(self, cookie_file):
        assert Cookies.from_file(cookie_file).ydl_options() == {
            "cookiefile": cookie_file
        }

    def test_ydl_options_browser(self):
        assert Cookies.from_browser("chrome").ydl_options() == {
            "cookiesfrombrowser": ("chrome",)
        }

    def test_requests_session_has_cookies(self, cookie_file):
        session = Cookies.from_file(cookie_file).requests_session()
        assert session is not None
        names = {c.name for c in session.cookies}
        assert {"__Secure-3PSID", "SID", "SOCS"} <= names
        assert session.headers["User-Agent"]

    def test_requests_session_missing_file(self, tmp_path):
        with pytest.raises(PlayerError, match="Cookie file not found"):
            Cookies.from_file(str(tmp_path / "nope.txt")).requests_session()


class FakeYDL:
    def __init__(self, result):
        self._result = result
        self.options = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        return self._result


class TestStreamExtractor:
    def test_resolve_builds_stream_info(self):
        info = {
            "id": "abc123",
            "title": "Some Song",
            "url": "https://googlevideo.example/audio",
            "creator": "Some Artist",
            "duration": 213.0,
            "ext": "webm",
            "format_id": "251",
            "thumbnail": "https://img.example/thumb.jpg",
            "webpage_url": "https://www.youtube.com/watch?v=abc123",
            "http_headers": {"User-Agent": "curl/8"},
        }
        extractor = StreamExtractor(ydl_factory=lambda opts: FakeYDL(info))
        stream = extractor.resolve("abc123")
        assert isinstance(stream, StreamInfo)
        assert stream.title == "Some Song"
        assert stream.stream_url == "https://googlevideo.example/audio"
        assert stream.artists == ["Some Artist"]
        assert stream.duration == 213.0
        assert stream.ext == "webm"
        assert stream.format_id == "251"
        assert stream.http_headers == {"User-Agent": "curl/8"}

    def test_resolve_sets_cookies_and_client(self, cookie_file):
        info = {"id": "abc", "title": "T", "url": "https://u"}
        seen = {}

        def factory(opts):
            seen.update(opts)
            return FakeYDL(info)

        extractor = StreamExtractor(Cookies.from_file(cookie_file), ydl_factory=factory)
        extractor.resolve("abc")
        assert seen["cookiefile"] == cookie_file
        assert seen["extractor_args"] == {
            "youtube": {"player_client": ["web_embedded"]}
        }
        assert seen["format"] == "bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio"
        assert seen["skip_download"] is True

    def test_resolve_missing_url_raises(self):
        extractor = StreamExtractor(
            ydl_factory=lambda opts: FakeYDL({"id": "abc", "title": "T"})
        )
        with pytest.raises(PlayerError, match="No playable audio format"):
            extractor.resolve("abc")

    def test_resolve_empty_video_id_raises(self):
        with pytest.raises(PlayerError, match="video id is required"):
            StreamExtractor().resolve("")

    def test_resolve_wraps_download_error(self):
        class BrokenYDL:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                raise yt_dlp.utils.DownloadError("bot check")

        with pytest.raises(PlayerError, match="Failed to extract stream"):
            StreamExtractor(ydl_factory=lambda opts: BrokenYDL()).resolve("abc")

    def test_download_returns_filepath(self, tmp_path):
        out = tmp_path / "music-cli-x"

        class DownloadingYDL(FakeYDL):
            def __init__(self, result):
                super().__init__(result)
                self.downloads = []

            def extract_info(self, url, download=False):
                self.downloads.append((url, download))
                return {"requested_downloads": [{"filepath": f"{out}.webm"}]}

        ydl = DownloadingYDL({})
        seen = {}

        def factory(opts):
            seen.update(opts)
            return ydl

        extractor = StreamExtractor(ydl_factory=factory)
        path = extractor.download("abc", str(out))
        assert path == f"{out}.webm"
        assert ydl.downloads == [("https://www.youtube.com/watch?v=abc", True)]
        assert seen["skip_download"] is False
        assert seen["outtmpl"] == f"{out}.%(ext)s"

    def test_download_wraps_download_error(self, tmp_path):
        class BrokenYDL:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                raise yt_dlp.utils.DownloadError("rate limit")

        with pytest.raises(PlayerError, match="Failed to download stream"):
            StreamExtractor(ydl_factory=lambda opts: BrokenYDL()).download(
                "abc", str(tmp_path / "x")
            )

    def test_download_empty_video_id_raises(self, tmp_path):
        with pytest.raises(PlayerError, match="video id is required"):
            StreamExtractor().download("", str(tmp_path / "x"))


class TestWatchPlaylist:
    def test_parse_watch_track(self):
        raw = {
            "videoId": "v1",
            "title": "Foolish Of Me",
            "length": "3:07",
            "videoType": "MUSIC_VIDEO_TYPE_ATV",
            "artists": [{"name": "Seven Lions"}],
            "thumbnail": [{"url": "https://img/1.jpg", "width": 60}],
            "counterpart": {"videoId": "v2", "title": "alt"},
        }
        track = parse_watch_track(raw)
        assert track.video_id == "v1"
        assert track.title == "Foolish Of Me"
        assert track.artists == ["Seven Lions"]
        assert track.duration == "3:07"
        assert track.video_type == "MUSIC_VIDEO_TYPE_ATV"
        assert track.thumbnail == "https://img/1.jpg"
        assert track.counterpart_video_id == "v2"

    def test_parse_watch_track_sparse(self):
        track = parse_watch_track({"videoId": "v1", "title": "T"})
        assert track.artists == []
        assert track.duration == ""
        assert track.thumbnail == ""
        assert track.counterpart_video_id == ""

    def test_get_requires_identifier(self):
        playlist = WatchPlaylist()
        with pytest.raises(PlayerError, match="videoId or playlistId"):
            playlist.get()

    def test_get_passes_video_id_and_parses(self):
        def fake_api():
            class Api:
                def get_watch_playlist(self, **kwargs):
                    assert kwargs["videoId"] == "abc"
                    assert kwargs["limit"] == 25
                    assert kwargs["radio"] is False
                    assert kwargs["shuffle"] is False
                    return {
                        "tracks": [
                            {
                                "videoId": "t1",
                                "title": "Track One",
                                "artists": [{"name": "A"}],
                            },
                            {"videoId": "t2", "title": "Track Two"},
                        ]
                    }

            return Api()

        tracks = WatchPlaylist(api=fake_api()).get("abc")
        assert len(tracks) == 2
        assert all(isinstance(t, PlaylistTrack) for t in tracks)
        assert tracks[0].video_id == "t1"
        assert tracks[1].title == "Track Two"


class FakeAVPlayer:
    """Minimal stand-in for AVPlayer exposing the pyobjc call surface used."""

    def __init__(self):
        self._rate = 0.0
        self._volume = 0.8
        self._muted = False
        self._time = CMTimeMakeWithSeconds(0.0, 600)
        self._item = None
        self.action_at_item_end = None
        self.seek_calls = []

    def setVolume_(self, value):
        self._volume = value

    def volume(self):
        return self._volume

    def setMuted_(self, value):
        self._muted = value

    def isMuted(self):
        return self._muted

    def setActionAtItemEnd_(self, action):
        self.action_at_item_end = action

    def rate(self):
        return self._rate

    def play(self):
        self._rate = 1.0

    def pause(self):
        self._rate = 0.0

    def currentTime(self):
        return self._time

    def seekToTime_toleranceBefore_toleranceAfter_(self, time, before, after):
        self.seek_calls.append((CMTimeGetSeconds(time), before, after))
        self._time = time

    def replaceCurrentItemWithPlayerItem_(self, item):
        self._item = item

    def currentItem(self):
        return self._item


class FakeItem:
    """Stand-in for AVPlayerItem, controllable per test."""

    def __init__(self, duration=100.0, status=1):
        self._duration = CMTimeMakeWithSeconds(duration, 600)
        self._status = status

    def duration(self):
        return self._duration

    def status(self):
        return self._status


def _local(stream: StreamInfo) -> LocalFile:
    return LocalFile(path="file:///tmp/fake-audio.m4a")


class TestAVFoundationPlayer:
    def test_constructor_applies_volume(self):
        fake = FakeAVPlayer()
        AVFoundationPlayer(volume=50, player_factory=lambda: fake)
        assert fake.volume() == 0.5
        assert not fake.isMuted()
        assert fake.action_at_item_end == AVPlayerActionAtItemEndPause

    def test_play_sets_item_and_starts(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        stream = StreamInfo(
            video_id="v",
            title="T",
            stream_url="https://u/audio",
            http_headers={"User-Agent": "ua"},
        )
        player.play(stream)
        assert fake._item is not None
        assert fake.rate() == 1.0
        assert player.media_title == "T"

    def test_transport_controls(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        assert player.paused is True
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        assert player.paused is False
        player.pause()
        assert player.paused is True
        player.resume()
        assert player.paused is False
        player.toggle()
        assert player.paused is True
        player.toggle()
        assert player.paused is False
        player.seek(42.0)
        assert fake.seek_calls[-1][0] == 42.0
        player.volume = 120
        assert fake.volume() == 1.0
        assert player.volume == 100
        player.muted = True
        assert fake.isMuted() is True

    def test_seek_relative(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        fake._time = CMTimeMakeWithSeconds(30.0, 600)
        player.seek_relative(-5)
        assert fake.seek_calls[-1][0] == 25.0

    def test_metadata_properties(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        player._current_item = FakeItem(duration=100.0, status=1)
        assert player.duration == 100.0
        assert player.media_title == "T"
        fake._time = CMTimeMakeWithSeconds(41.5, 600)
        assert player.position == 41.5

    def test_duration_none_without_item(self):
        player = AVFoundationPlayer(player_factory=lambda: FakeAVPlayer())
        assert player.duration is None
        assert not player.playing

    def test_playing_state_follows_item_status(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        player._current_item = FakeItem(status=1)
        assert player.playing
        player._current_item = FakeItem(status=2)  # Failed
        assert not player.playing

    def test_eof_event_and_wait(self):
        event = threading.Event()

        def on_end():
            event.set()

        player = AVFoundationPlayer(
            on_track_end=on_end, player_factory=lambda: FakeAVPlayer()
        )
        assert not player.eof_reached
        player._on_item_ended(None)
        assert player.eof_reached
        assert event.is_set()
        assert player.wait_for_end(0.1) is True

    def test_close_stops_and_unloads(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        player.close()
        assert fake._item is None
        assert player._observer_token is None


class TestFileOwnership:
    def test_unowned_file_survives_stop(self, tmp_path):
        audio = tmp_path / "cached.m4a"
        audio.write_bytes(b"audio")

        def fetch(stream):
            return LocalFile(path=str(audio), owned=False)

        player = AVFoundationPlayer(
            player_factory=lambda: FakeAVPlayer(), fetch_stream=fetch
        )
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        player.stop()
        assert audio.exists()

    def test_owned_file_is_deleted_on_stop(self, tmp_path):
        audio = tmp_path / "temp.m4a"
        audio.write_bytes(b"audio")

        def fetch(stream):
            return LocalFile(path=str(audio), owned=True)

        player = AVFoundationPlayer(
            player_factory=lambda: FakeAVPlayer(), fetch_stream=fetch
        )
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        player.stop()
        assert not audio.exists()

    def test_replacing_track_removes_owned_but_keeps_cached(self, tmp_path):
        temp = tmp_path / "temp.m4a"
        cached = tmp_path / "cached.m4a"
        temp.write_bytes(b"a")
        cached.write_bytes(b"b")
        first = {"value": True}

        def fetch(stream):
            if first["value"]:
                first["value"] = False
                return LocalFile(path=str(temp), owned=True)
            return LocalFile(path=str(cached), owned=False)

        player = AVFoundationPlayer(
            player_factory=lambda: FakeAVPlayer(), fetch_stream=fetch
        )
        player.play(StreamInfo(video_id="v1", title="T1", stream_url="https://u"))
        player.play(StreamInfo(video_id="v2", title="T2", stream_url="https://u"))
        assert not temp.exists()
        assert cached.exists()


class WritingYDL:
    """Writes a file at the yt-dlp outtmpl location and reports it back."""

    def __init__(self):
        self.options = {}
        self.calls = []

    @staticmethod
    def factory(ydl):
        return lambda opts: WritingYDL._set_options(ydl, opts)

    @staticmethod
    def _set_options(ydl, opts):
        ydl.options = opts
        return ydl

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        self.calls.append((url, download))
        path = self.options["outtmpl"].replace("%(ext)s", "m4a")
        Path(path).write_bytes(b"audio-bytes")
        return {"requested_downloads": [{"filepath": path}]}


class TestCachedFetch:
    def test_hit_returns_cached_file_without_downloading(self, tmp_path):
        cache = AudioCache(directory=tmp_path / "cache")
        target = cache.tmp_path("abc")
        src = Path(f"{target}.m4a")
        src.write_bytes(b"audio-bytes")
        cache.commit(
            "abc",
            TrackMeta(title="T", artists=("A",), duration=1.0, ext="m4a"),
            src=src,
        )

        ydl = WritingYDL()
        fetch = _default_fetch_stream(
            None,
            cache=cache,
            extractor=StreamExtractor(ydl_factory=WritingYDL.factory(ydl)),
        )
        local = fetch(StreamInfo(video_id="abc", title="T", stream_url="https://u"))
        assert local.owned is False
        assert Path(local.path).is_file()
        assert local.path.endswith("abc.m4a")
        assert ydl.calls == []

    def test_miss_downloads_and_commits(self, tmp_path):
        cache = AudioCache(directory=tmp_path / "cache")
        ydl = WritingYDL()
        fetch = _default_fetch_stream(
            None,
            cache=cache,
            extractor=StreamExtractor(ydl_factory=WritingYDL.factory(ydl)),
        )
        stream = StreamInfo(video_id="abc", title="T", stream_url="https://u")
        local = fetch(stream)
        assert local.owned is False
        assert Path(local.path).is_file()
        assert ydl.calls == [("https://www.youtube.com/watch?v=abc", True)]
        track = cache.lookup("abc")
        assert track is not None
        assert track.title == "T"
        assert track.ext == "m4a"

    def test_without_cache_falls_back_to_temp_download(self, tmp_path):
        ydl = WritingYDL()
        fetch = _default_fetch_stream(
            None,
            extractor=StreamExtractor(ydl_factory=WritingYDL.factory(ydl)),
        )
        local = fetch(StreamInfo(video_id="abc", title="T", stream_url="https://u"))
        assert local.owned is True
        assert Path(local.path).is_file()
