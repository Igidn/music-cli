"""Cross-platform player tests.

AVFoundationPlayer tests run only on macOS (where pyobjc is available).
GStreamerPlayer tests run only when GStreamer is installed (most Linux,
and some BSDs / Windows with MSYS2).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yt_dlp

from music_cli.core.errors import PlayerError
from music_cli.storage.cache import AudioCache, TrackMeta
from music_cli.yt.cookies import Cookies
from music_cli.yt.extract import (
    PlaylistTrack,
    StreamExtractor,
    StreamInfo,
    WatchPlaylist,
    parse_watch_track,
)

# ------------------------------------------------------------------
# Platform-detection helpers for skip markers
# ------------------------------------------------------------------

_HAS_AVFOUNDATION = False
_HAS_GSTREAMER = False

try:
    from AVFoundation import (
        AVPlayerActionAtItemEndPause,
        CMTimeGetSeconds,
        CMTimeMakeWithSeconds,
    )

    _HAS_AVFOUNDATION = True
except ImportError:
    pass

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: F401

    _HAS_GSTREAMER = True
except ImportError, ValueError:
    pass

# ------------------------------------------------------------------
# Shared imports (cross-platform)
# ------------------------------------------------------------------

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
        # "default" player client: yt-dlp picks its own maintained clients.
        assert "extractor_args" not in seen
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

    def test_download_registers_progress_hook(self, tmp_path):
        out = tmp_path / "music-cli-x"
        result = {"requested_downloads": [{"filepath": f"{out}.webm"}]}
        seen = {}
        hook = lambda p: None  # noqa: E731, RUF100

        class HookYDL(FakeYDL):
            def extract_info(self, url, download=False):
                return result

        StreamExtractor(
            ydl_factory=lambda opts: seen.update(opts) or HookYDL({})
        ).download("abc", str(out), progress_hook=hook)
        assert seen["progress_hooks"] == [hook]

    def test_download_omits_hook_by_default(self, tmp_path):
        out = tmp_path / "music-cli-x"
        result = {"requested_downloads": [{"filepath": f"{out}.webm"}]}
        seen = {}

        class HookYDL(FakeYDL):
            def extract_info(self, url, download=False):
                return result

        StreamExtractor(
            ydl_factory=lambda opts: seen.update(opts) or HookYDL({})
        ).download("abc", str(out))
        assert "progress_hooks" not in seen

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


# ------------------------------------------------------------------
# AVFoundationPlayer tests (macOS only via pyobjc)
# ------------------------------------------------------------------

if _HAS_AVFOUNDATION:
    from AVFoundation import (
        AVPlayerActionAtItemEndPause,
        CMTimeGetSeconds,
        CMTimeMakeWithSeconds,
    )

    from music_cli.player.audio import AVFoundationPlayer
    from music_cli.player.base import LocalFile

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
            self.audio_mix = None

        def duration(self):
            return self._duration

        def status(self):
            return self._status

        def setAudioMix_(self, mix):
            self.audio_mix = mix

    def _local(stream: StreamInfo) -> LocalFile:
        return LocalFile(path="file:///tmp/fake-audio.m4a")

    @pytest.mark.skipif(
        not _HAS_AVFOUNDATION, reason="pyobjc-avfoundation not installed (not macOS)"
    )
    class TestAVFoundationPlayer:
        def test_constructor_applies_volume(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(volume=50, player_factory=lambda: fake)
            assert player.volume == 50
            assert fake.volume() == 1.0
            assert not fake.isMuted()
            assert fake.action_at_item_end == AVPlayerActionAtItemEndPause

        def test_volume_carried_on_item_audio_mix(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(volume=40, player_factory=lambda: fake)
            item = FakeItem()
            player._current_item = item
            player.volume = 75
            assert item.audio_mix is not None
            assert item.audio_mix.inputParameters()
            assert player.volume == 75

        def test_play_sets_item_and_starts(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
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

        def test_play_retries_when_end_of_item_pause_drops_request(self):
            """The end-of-item pause can land after the next play() request.

            Replicates the auto-next race: play() is issued, the player briefly
            reports rate 0 (the previous item's pause), so playback must be
            re-requested until the rate sticks.
            """

            class DroppingAVPlayer(FakeAVPlayer):
                def __init__(self):
                    super().__init__()
                    self.play_calls = 0
                    self._drop_next = True

                def play(self):
                    self.play_calls += 1
                    if not self._drop_next:
                        self._rate = 1.0
                    self._drop_next = False

            fake = DroppingAVPlayer()
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
            player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
            assert fake.play_calls >= 2
            assert fake.rate() == 1.0

        def test_transport_controls(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
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
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
            player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
            fake._time = CMTimeMakeWithSeconds(30.0, 600)
            player.seek_relative(-5)
            assert fake.seek_calls[-1][0] == 25.0

        def test_metadata_properties(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
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
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
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

        def test_loop_replays_instead_of_ending(self):
            event = threading.Event()
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(
                loop=True,
                on_track_end=event.set,
                player_factory=lambda: fake,
                fetch_stream=_local,
            )
            player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
            fake._time = CMTimeMakeWithSeconds(95.0, 600)
            player._on_item_ended(None)
            assert CMTimeGetSeconds(fake._time) == 0.0
            assert fake.rate() == 1.0
            assert not player.eof_reached
            assert not event.is_set()

        def test_loop_off_notifies_on_end(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(
                loop=True,
                on_track_end=lambda: None,
                player_factory=lambda: fake,
                fetch_stream=_local,
            )
            player.play(StreamInfo(video_id="v", title="T", stream_url="https://u"))
            player.loop = False
            player._on_item_ended(None)
            assert player.eof_reached

        def test_stale_end_notification_from_replaced_item_is_ignored(self):
            """Replacing the current item must not register the old item's end.

            The previous item's DidPlayToEndTime can be delivered late (queued on
            the run loop before _unobserve ran, fired by the next pump()). Without
            a guard it would auto-advance past the track just requested — the
            up-next head flashing as current and landing in the play history.
            """
            ended = threading.Event()
            player = AVFoundationPlayer(
                on_track_end=ended.set,
                player_factory=lambda: FakeAVPlayer(),
                fetch_stream=_local,
            )
            player.play(StreamInfo(video_id="old", title="Old", stream_url="https://u"))
            old_item = player._current_item
            # User switches: a new item replaces the old one before the old item's
            # end notification is delivered.
            player.play(StreamInfo(video_id="new", title="New", stream_url="https://u"))
            assert player._current_item is not old_item

            class _Note:
                def __init__(self, obj):
                    self._obj = obj

                def object(self):
                    return self._obj

            # The stale notification for the replaced item must be ignored.
            player._on_item_ended(_Note(old_item))
            assert not player.eof_reached
            assert not ended.is_set()
            # A real end of the current item still registers.
            player._on_item_ended(_Note(player._current_item))
            assert player.eof_reached
            assert ended.is_set()

        def test_loop_property_defaults_off(self):
            player = AVFoundationPlayer(player_factory=lambda: FakeAVPlayer())
            assert player.loop is False
            player.loop = True
            assert player.loop is True

        def test_close_stops_and_unloads(self):
            fake = FakeAVPlayer()
            player = AVFoundationPlayer(
                player_factory=lambda: fake, fetch_stream=_local
            )
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

else:
    # Placeholder so `pytest --co` / pytest's test discovery doesn't
    # error on an absent class or tests referencing AVFoundation types.
    _ = None


# ------------------------------------------------------------------
# GStreamerPlayer tests (Linux / GStreamer-enabled platforms)
# ------------------------------------------------------------------

if _HAS_GSTREAMER:
    from music_cli.player.base import LocalFile
    from music_cli.player.gst import GStreamerPlayer

    def _gst_local(stream: StreamInfo) -> LocalFile:
        return LocalFile(path="file:///tmp/gst-fake-audio.m4a")

    @pytest.mark.skipif(
        not _HAS_GSTREAMER, reason="GStreamer not available (gi.repository.Gst)"
    )
    class TestGStreamerPlayer:
        def test_constructor_defaults(self):
            player = GStreamerPlayer()
            assert player.volume == 80
            assert player.loop is False
            assert player.muted is False
            assert player.paused is True
            assert player.position == 0.0
            assert not player.playing

        def test_constructor_applies_volume(self):
            player = GStreamerPlayer(volume=42)
            assert player.volume == 42
            # Volume is clamped 0-100.
            player = GStreamerPlayer(volume=-1)
            assert player.volume == 0
            player = GStreamerPlayer(volume=200)
            assert player.volume == 100

        def test_volume_property(self):
            player = GStreamerPlayer()
            player.volume = 65
            assert player.volume == 65
            player.volume = 150
            assert player.volume == 100

        def test_muted_property(self):
            player = GStreamerPlayer()
            assert player.muted is False
            player.muted = True
            assert player.muted is True

        def test_loop_property(self):
            player = GStreamerPlayer()
            assert player.loop is False
            player.loop = True
            assert player.loop is True

        def test_play_and_stop(self):
            """Smoke test: play() creates a pipeline and stop() tears it down.

            Without a real audio file GStreamer cannot preroll, so
            ``playing`` reports ``False`` — no exception is the signal
            that the pipeline was created successfully.
            """
            player = GStreamerPlayer()
            stream = StreamInfo(
                video_id="v",
                title="Test",
                stream_url="https://example.test/audio",
                http_headers={},
                artists=["Test Artist"],
                duration=100.0,
            )
            # No exception -> pipeline created without error.
            player.play(stream)
            # GStreamer can't reach PLAYING without a real file.
            # That's fine — the client's retry loop handles it.
            assert not player.playing
            player.stop()
            assert not player.playing

        def test_pause_resume(self):
            """Calling pause/resume on an idle pipeline is safe."""
            player = GStreamerPlayer()
            # Without a valid audio file the pipeline stays in NULL, but
            # pause/resume must not crash (they are no-ops internally).
            player.pause()
            player.resume()

        def test_toggle(self):
            """Calling toggle on an idle pipeline is safe."""
            player = GStreamerPlayer()
            player.toggle()
            player.toggle()

        def test_seek(self):
            player = GStreamerPlayer()
            # Seek on an idle pipeline is safe (no-op internally).
            player.seek(30.0)
            player.seek_relative(5.0)

        def test_close_cleans_up(self):
            player = GStreamerPlayer()
            stream = StreamInfo(
                video_id="v", title="T", stream_url="https://u", http_headers={}
            )
            player.play(stream)
            player.close()
            assert not player.playing

        def test_pump_no_crash(self):
            """pump() on an idle pipeline must not crash."""
            player = GStreamerPlayer()
            player.pump()

        def test_double_stop(self):
            """Calling stop() twice must not crash."""
            player = GStreamerPlayer()
            stream = StreamInfo(
                video_id="v", title="T", stream_url="https://u", http_headers={}
            )
            player.play(stream)
            player.stop()
            player.stop()

        def test_media_title(self):
            """media_title is set from the stream passed to play()."""
            player = GStreamerPlayer(
                fetch_stream=lambda s: LocalFile(path="file:///dev/null", owned=False)
            )
            assert player.media_title == ""
            stream = StreamInfo(
                video_id="v",
                title="My Song",
                stream_url="https://u",
                http_headers={},
            )
            player.play(stream)
            assert player.media_title == "My Song"

        def test_duration_and_position_no_track(self):
            player = GStreamerPlayer()
            assert player.duration is None
            assert player.position == 0.0

        def test_eof_reached_defaults_false(self):
            player = GStreamerPlayer()
            assert player.eof_reached is False

        def test_owned_file_deleted_on_stop(self, tmp_path):
            audio = tmp_path / "temp.m4a"
            audio.write_bytes(b"audio")

            def fetch(stream):
                return LocalFile(path=str(audio), owned=True)

            player = GStreamerPlayer(fetch_stream=fetch)
            stream = StreamInfo(
                video_id="v", title="T", stream_url="https://u", http_headers={}
            )
            player.play(stream)
            assert audio.exists()
            player.stop()
            assert not audio.exists()

        def test_unowned_file_survives_stop(self, tmp_path):
            audio = tmp_path / "cached.m4a"
            audio.write_bytes(b"audio")

            def fetch(stream):
                return LocalFile(path=str(audio), owned=False)

            player = GStreamerPlayer(fetch_stream=fetch)
            stream = StreamInfo(
                video_id="v", title="T", stream_url="https://u", http_headers={}
            )
            player.play(stream)
            player.stop()
            assert audio.exists()

        def test_replacing_track_removes_owned_file(self, tmp_path):
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

            player = GStreamerPlayer(fetch_stream=fetch)
            stream1 = StreamInfo(
                video_id="v1", title="T1", stream_url="https://u", http_headers={}
            )
            stream2 = StreamInfo(
                video_id="v2", title="T2", stream_url="https://u", http_headers={}
            )
            player.play(stream1)
            player.play(stream2)
            assert not temp.exists()
            assert cached.exists()

        def test_loop_property_affects_eos_behaviour(self):
            """With loop on, EOS should seek to 0 and continue."""
            player = GStreamerPlayer(loop=True)
            assert player.loop is True
            player.loop = False
            assert player.loop is False

        def test_wait_for_end_timeout(self):
            """wait_for_end() returns False on timeout when no track ended."""
            player = GStreamerPlayer()
            assert player.wait_for_end(0.05) is False

        def test_on_track_end_callback_without_loop(self):
            """When loop is off, the on_track_end callback must fire on EOS."""
            fired = threading.Event()
            player = GStreamerPlayer(loop=False, on_track_end=fired.set)
            # Simulate an EOS message via the internal handler.
            player._on_eos()
            assert fired.wait(1.0)
            assert player.eof_reached

        def test_loop_eos_does_not_fire_callback(self):
            """When loop is on, EOS must not fire the track-end callback."""
            fired = threading.Event()
            player = GStreamerPlayer(loop=True, on_track_end=fired.set)
            player._on_eos()
            assert not fired.is_set()
            assert not player.eof_reached

else:
    _ = None


# ------------------------------------------------------------------
# Cross-platform stream fetch / cache tests (use the base module)
# ------------------------------------------------------------------

from music_cli.player.base import (  # noqa: E402
    LocalFile,
    default_fetch_stream,
)


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
        fetch = default_fetch_stream(
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
        fetch = default_fetch_stream(
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
        fetch = default_fetch_stream(
            None,
            extractor=StreamExtractor(ydl_factory=WritingYDL.factory(ydl)),
        )
        local = fetch(StreamInfo(video_id="abc", title="T", stream_url="https://u"))
        assert local.owned is True
        assert Path(local.path).is_file()
