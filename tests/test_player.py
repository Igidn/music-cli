from __future__ import annotations

import threading

import pytest
import yt_dlp

from music_cli.player import (
    Cookies,
    MpvPlayer,
    PlayerError,
    PlaylistTrack,
    StreamExtractor,
    StreamInfo,
    WatchPlaylist,
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
        assert seen["format"] == "bestaudio/best"
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


class FakeMPV:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pause = False
        self.mute = False
        self.volume = kwargs.get("volume", 80)
        self.playback_time = 0.0
        self.duration = 100.0
        self.media_title = "title"
        self.audio_params = {"format": "floatp", "samplerate": 48000}
        self.audio_codec = "opus"
        self.force_media_title = None
        self.http_header_fields = None
        self.idle_active = True
        self.playlist = []
        self._handlers = {}
        self._loaded = None

    def observe_property(self, name, handler):
        self._handlers[name] = handler

    def play(self, filename):
        self._loaded = filename
        self.playback_time = 0.1
        self.playlist = [{"filename": filename}]
        self.idle_active = False

    def stop(self):
        pass

    def seek(self, seconds, reference):
        self.playback_time = seconds

    def terminate(self):
        self.playback_time = -1

    def fire_eof(self):
        self._handlers["eof-reached"]("eof-reached", True)


class TestMpvPlayer:
    def test_constructor_options(self):
        player = MpvPlayer(
            volume=50,
            audio_output="coreaudio",
            mpv_factory=FakeMPV,
        )
        assert player._mpv.kwargs == {
            "ytdl": False,
            "volume": 50,
            "keep_open": False,
            "idle": True,
            "ao": "coreaudio",
        }

    def test_play_applies_headers_and_url(self):
        player = MpvPlayer(mpv_factory=FakeMPV)
        stream = StreamInfo(
            video_id="v",
            title="T",
            stream_url="https://u/audio",
            http_headers={"User-Agent": "ua"},
        )
        player.play(stream)
        assert player._mpv._loaded == "https://u/audio"
        assert player._mpv.http_header_fields == ["User-Agent: ua"]
        assert player._mpv.force_media_title == "T"

    def test_transport_controls(self):
        player = MpvPlayer(mpv_factory=FakeMPV)
        assert player.paused is False
        player.pause()
        assert player.paused is True
        player.resume()
        assert player.paused is False
        player.toggle()
        assert player.paused is True
        player.seek(42.0)
        assert player.position == 42.0
        player.volume = 120
        assert player.volume == 100
        player.muted = True
        assert player.muted is True

    def test_metadata_properties(self):
        player = MpvPlayer(mpv_factory=FakeMPV)
        assert player.duration == 100.0
        assert player.media_title == "title"
        assert player.audio_codec == "opus"

    def test_playing_state(self):
        player = MpvPlayer(mpv_factory=FakeMPV)
        assert not player.playing
        player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
        assert player.playing
        player._mpv.idle_active = True
        assert not player.playing

    def test_eof_event_and_wait(self):
        event = threading.Event()

        def on_end():
            event.set()

        player = MpvPlayer(on_track_end=on_end, mpv_factory=FakeMPV)
        assert not player.eof_reached
        player._mpv.fire_eof()
        assert player.eof_reached
        assert event.is_set()
        assert player.wait_for_end(0.1) is True

    def test_close_terminates(self):
        player = MpvPlayer(mpv_factory=FakeMPV)
        player.close()
        assert player._mpv.playback_time == -1
