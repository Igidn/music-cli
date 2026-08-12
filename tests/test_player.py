from __future__ import annotations

import os
import threading
import time

import pytest
import yt_dlp
from AVFoundation import (
    AVPlayerActionAtItemEndPause,
    CMTimeGetSeconds,
    CMTimeMakeWithSeconds,
)

from music_cli.player import (
    STREAM_START_THRESHOLD,
    AVFoundationPlayer,
    Cookies,
    PlayerError,
    PlaylistTrack,
    StreamExtractor,
    StreamFile,
    StreamInfo,
    WatchPlaylist,
    _stream_to_file,
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
        assert seen["nopart"] is True
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


class SlowFakeExtractor:
    """A fake StreamExtractor whose download writes progressively."""

    def __init__(self, total: int, *, fail: bool = False) -> None:
        self._total = total
        self._fail = fail
        self.done = threading.Event()

    def download(self, video_id: str, outtmpl: str) -> str:
        if self._fail:
            time.sleep(0.1)
            raise PlayerError("stream failed for v1")
        path = f"{outtmpl}.m4a"
        with open(path, "wb") as file:
            for _ in range(32):
                file.write(b"\x00" * (self._total // 32))
                file.flush()
                time.sleep(0.02)
        self.done.set()
        return path


class TestStreamFetch:
    def test_returns_before_download_completes(self, tmp_path):
        out = tmp_path / "music-cli-x"
        extractor = SlowFakeExtractor(1 << 20)  # 1 MiB, threshold is 512 KiB
        result = _stream_to_file(
            extractor,
            StreamInfo(video_id="v1", title="T", stream_url="https://u"),
            str(out),
        )
        assert os.path.getsize(result.path) >= STREAM_START_THRESHOLD
        assert not extractor.done.is_set(), "download should still be running"
        assert result.error is None

    def test_returns_small_completed_file(self, tmp_path):
        out = tmp_path / "music-cli-y"
        extractor = SlowFakeExtractor(1024)
        result = _stream_to_file(
            extractor,
            StreamInfo(video_id="v1", title="T", stream_url="https://u"),
            str(out),
        )
        assert os.path.getsize(result.path) == 1024
        assert extractor.done.is_set()
        assert result.done.is_set()

    def test_raises_when_download_fails(self, tmp_path):
        out = tmp_path / "music-cli-z"
        extractor = SlowFakeExtractor(1 << 20, fail=True)
        with pytest.raises(PlayerError, match="stream failed"):
            _stream_to_file(
                extractor,
                StreamInfo(video_id="v1", title="T", stream_url="https://u"),
                str(out),
            )

    def test_delivers_failure_after_start(self, tmp_path):
        """A download that fails after playback started surfaces on StreamFile."""
        out = tmp_path / "music-cli-w"

        class FailAfterStart(SlowFakeExtractor):
            def download(self, video_id, outtmpl):
                path = f"{outtmpl}.m4a"
                with open(path, "wb") as file:
                    file.write(b"\x00" * (STREAM_START_THRESHOLD * 2))
                raise PlayerError("died mid-stream")

        result = _stream_to_file(
            FailAfterStart(0),
            StreamInfo(video_id="v1", title="T", stream_url="https://u"),
            str(out),
        )
        assert result.path  # early return on threshold
        assert result.done.wait(5)
        assert result.error is not None
        assert "died mid-stream" in str(result.error)


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


def _local(stream: StreamInfo) -> str:
    return "file:///tmp/fake-audio.m4a"


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

    def test_duration_clamped_to_track_duration(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(
            StreamInfo(video_id="v", title="T", stream_url="https://u", duration=213.0)
        )
        player._current_item = FakeItem(duration=426.0, status=1)  # doubled moov
        assert player.duration == 213.0

    def test_duration_none_without_item(self):
        player = AVFoundationPlayer(player_factory=lambda: FakeAVPlayer())
        assert player.duration is None
        assert not player.playing

    def test_watchdog_ends_track_at_real_duration(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        stream = StreamInfo(video_id="v", title="T", stream_url="https://u", duration=213.0)
        player.play(stream)
        player._current_item = FakeItem(duration=426.0, status=1)  # doubled moov
        fake._time = CMTimeMakeWithSeconds(213.0, 600)
        stream_file = StreamFile(path="/tmp/fake.m4a", done=threading.Event())
        stream_file.done.set()
        player._watch_stream(stream_file, player._watch_generation)
        assert player.eof_reached
        assert fake.rate() == 0.0  # paused at the real end

    def test_watchdog_ends_track_when_stream_dies(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        fake._time = CMTimeMakeWithSeconds(45.0, 600)
        stream_file = StreamFile(
            path="/tmp/fake.m4a",
            done=threading.Event(),
            error=PlayerError("died mid-stream"),
        )
        stream_file.done.set()
        player._watch_stream(stream_file, player._watch_generation)
        assert player.eof_reached

    def test_watchdog_ignores_stream_death_before_playback(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        stream_file = StreamFile(
            path="/tmp/fake.m4a",
            done=threading.Event(),
            error=PlayerError("died mid-stream"),
        )
        stream_file.done.set()
        player._watch_stream(stream_file, player._watch_generation)
        assert not player.eof_reached

    def test_watchdog_stale_generation_exits(self):
        fake = FakeAVPlayer()
        player = AVFoundationPlayer(player_factory=lambda: fake, fetch_stream=_local)
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        fake._time = CMTimeMakeWithSeconds(213.0, 600)
        stream_file = StreamFile(path="/tmp/fake.m4a", done=threading.Event())
        stream_file.done.set()
        player._watch_stream(stream_file, player._watch_generation - 1)
        assert not player.eof_reached

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
