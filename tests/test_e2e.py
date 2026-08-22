"""End-to-end tests against the live YouTube API.

These require a YouTube account cookie file (defaults to ``cookie.txt`` in the
project root, overridable via ``MUSIC_CLI_COOKIE_FILE``). They verify that a
real audio stream is extracted, that real audio bytes are delivered, that the
watch playlist (autoplay queue) comes back, and that AVFoundation actually
decodes and plays the audio.

Run with:  uv run pytest -m e2e tests/test_e2e.py -v
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import time

import pytest

from music_cli.client import MusicClient
from music_cli.storage.cache import AudioCache
from music_cli.yt.cookies import Cookies
from music_cli.yt.extract import PlaylistTrack, StreamExtractor, WatchPlaylist

KNOWN_VIDEO = "dQw4w9WgXcQ"  # Rick Astley - Never Gonna Give You Up

pytestmark = [pytest.mark.e2e]

# AVFoundation is only available on macOS (pyobjc).  These tests are already
# gated behind ``-m e2e``, but the import still needs to survive collection
# on Linux.
try:
    from music_cli.player.audio import AVFoundationPlayer as _AVFoundationPlayer
except ImportError:
    _AVFoundationPlayer = None  # type: ignore[assignment]

COOKIE_FILE = os.environ.get("MUSIC_CLI_COOKIE_FILE", "cookie.txt")

STREAM_CMD = [
    ".venv/bin/yt-dlp",
    "--cookies",
    COOKIE_FILE,
    "--extractor-args",
    "youtube:player_client=web_embedded",
    "-f",
    "bestaudio[ext=m4a]/bestaudio",
    "-o",
    "-",
]


@pytest.fixture(scope="module")
def cookies():
    if not os.path.isfile(COOKIE_FILE):
        pytest.skip(f"cookie file not found: {COOKIE_FILE}")
    return Cookies.from_file(COOKIE_FILE)


@pytest.fixture(scope="module")
def extractor(cookies):
    return StreamExtractor(cookies)


@pytest.fixture(scope="module")
def stream(extractor):
    return extractor.resolve(KNOWN_VIDEO)


class TestStreamExtraction:
    def test_extract_known_video(self, stream):
        assert stream.video_id == KNOWN_VIDEO
        assert stream.title
        assert stream.stream_url.startswith("https://")
        assert stream.duration and stream.duration > 100
        assert stream.ext in {"webm", "m4a", "opus", "mp4"}
        assert stream.http_headers

    def test_stream_delivers_real_audio_bytes(self):
        proc = subprocess.run(
            [*STREAM_CMD, f"https://www.youtube.com/watch?v={KNOWN_VIDEO}"],
            capture_output=True,
            timeout=120,
            check=True,
        )
        data = proc.stdout[:262144]
        assert len(data) == 262144, "expected at least 256 KiB of audio bytes"
        assert data[:4] == b"\x1aE\xdf\xa3" or data[4:8] == b"ftyp", (
            f"expected EBML (WebM) or MP4 header, got {data[:8].hex()}"
        )


class TestWatchPlaylist:
    def test_autoplay_queue(self, cookies):
        playlist = WatchPlaylist(cookies=cookies)
        tracks = playlist.get(KNOWN_VIDEO, limit=10)
        assert len(tracks) >= 5
        for track in tracks[:5]:
            assert track.video_id
            assert track.title
        assert tracks[0].video_id == KNOWN_VIDEO

    def test_autoplay_queue_radio(self, cookies):
        playlist = WatchPlaylist(cookies=cookies)
        tracks = playlist.get(KNOWN_VIDEO, limit=10, radio=True)
        assert len(tracks) >= 5


@pytest.mark.skipif(_AVFoundationPlayer is None, reason="pyobjc-avfoundation not installed")
class TestAVFoundationPlayback:
    AVFoundationPlayer = _AVFoundationPlayer

    def test_plays_real_audio(self, cookies, stream, extractor):
        candidate = stream
        for _attempt in range(3):
            player = self.AVFoundationPlayer(cookies=cookies)
            try:
                player.play(candidate)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and player.position < 1.0:
                    player.pump()
                    time.sleep(0.2)
                if player.position >= 1.0:
                    assert player.media_title == candidate.title
                    assert player.playing
                    return
            finally:
                player.close()
            candidate = extractor.resolve(stream.video_id)  # throttle: try a fresh URL
        raise AssertionError("AVFoundation failed to decode audio after 3 attempts")

    def test_end_of_track_event(self, cookies, stream, extractor):
        """Seek to the last seconds and expect the end-of-track notification."""
        for _attempt in range(3):
            player = self.AVFoundationPlayer(cookies=cookies)
            try:
                player.play(stream)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not player.duration:
                    player.pump()
                    time.sleep(0.2)
                duration = player.duration
                if duration and duration > 100:
                    player.seek(duration - 5)
                    deadline = time.monotonic() + 30
                    while not player.eof_reached and time.monotonic() < deadline:
                        player.pump()
                        time.sleep(0.2)
                    assert player.eof_reached, "end-of-track event did not fire"
                    return
                # Duration might not resolve on some codecs; that is fine.
            finally:
                player.close()
            stream = extractor.resolve(stream.video_id)
        raise AssertionError("AVFoundation never reached end of track")


class TestCacheEndToEnd:
    """The disk cache with real network: download, replay and no-network playback."""

    @pytest.fixture
    def cache_dir(self, tmp_path):
        return tmp_path / "cache"

    def test_prefetch_downloads_real_track_into_cache(self, cookies, cache_dir):
        client = MusicClient(
            cookies=cookies,
            cache=AudioCache(directory=cache_dir),
        )
        try:
            downloaded = False
            for _attempt in range(3):
                if client.prefetch(KNOWN_VIDEO):
                    downloaded = True
                    break
            assert downloaded, "prefetch did not cache the track"
            cached = client.cache.lookup(KNOWN_VIDEO)
            assert cached is not None
            assert cached.title, "metadata missing from cache entry"
            path = client.cache.path_for(KNOWN_VIDEO)
            assert path is not None and path.is_file()
            assert cached.size > 100_000, "cached file looks empty"
            assert path.stat().st_size == cached.size
            header = path.read_bytes()[:12]
            assert header[4:8] == b"ftyp" or header[:4] == b"\x1aE\xdf\xa3", (
                f"cached file is not a valid audio container: {header.hex()}"
            )
        finally:
            client.close()

    def test_playback_starts_from_cache_after_download(self, cookies, cache_dir):
        client = MusicClient(
            cookies=cookies,
            cache=AudioCache(directory=cache_dir),
        )
        try:
            assert client.prefetch(KNOWN_VIDEO)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    client.play_track,
                    PlaylistTrack(video_id=KNOWN_VIDEO, title="cached"),
                )
                deadline = time.monotonic() + 60
                while (
                    time.monotonic() < deadline
                    and client.player.position < 1.0
                    and not future.done()
                ):
                    client.player.pump()
                    time.sleep(0.1)
                future.result(timeout=15)
                while time.monotonic() < deadline and client.player.position < 1.0:
                    client.player.pump()
                    time.sleep(0.1)
                assert client.player.position >= 1.0, "cached track did not play"
            assert client.current is not None
            assert client.current.video_id == KNOWN_VIDEO
            assert client.cache.lookup(KNOWN_VIDEO) is not None
        finally:
            client.close()

    def test_restart_replays_from_cache_with_no_network(self, cookies, cache_dir):
        first = MusicClient(
            cookies=cookies,
            cache=AudioCache(directory=cache_dir),
        )
        try:
            assert first.prefetch(KNOWN_VIDEO)
        finally:
            first.close()

        class NetworkBlocked:
            def __init__(self, *args, **kwargs):
                pass

            def resolve(self, video_id):
                raise AssertionError(f"network used to resolve {video_id}")

            def download(self, video_id, target):
                raise AssertionError(f"network used to download {video_id}")

        second = MusicClient(
            cookies=cookies,
            extractor_factory=NetworkBlocked,
            cache=AudioCache(directory=cache_dir),
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    second.play_track,
                    PlaylistTrack(video_id=KNOWN_VIDEO, title="cached"),
                )
                deadline = time.monotonic() + 60
                while (
                    time.monotonic() < deadline
                    and second.player.position < 1.0
                    and not future.done()
                ):
                    second.player.pump()
                    time.sleep(0.1)
                future.result(timeout=15)
                while time.monotonic() < deadline and second.player.position < 1.0:
                    second.player.pump()
                    time.sleep(0.1)
                assert second.player.position >= 1.0, (
                    "cached replay did not play without network"
                )
            assert second.current is not None
            assert second.current.video_id == KNOWN_VIDEO
            assert second.current.stream_url == "", (
                "replay resolved a stream URL instead of using the cache"
            )
        finally:
            second.close()


class TestTuiIntegration:
    """The TUI wired to the real client: search, play and queue end to end."""

    def test_tui_searches_plays_and_queues(self, cookies):
        import asyncio

        from music_cli.client import MusicClient
        from music_cli.tui.app import MusicTUI
        from music_cli.tui.components import QueueList, ResultsTable
        from music_cli.tui.components.now_playing import NowPlaying

        async def scenario():
            client = MusicClient(cookies=cookies)
            app = MusicTUI(client)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.query_one("#search-input").value = "never gonna give you up"
                await pilot.pause(3.0)
                results = app.query_one(ResultsTable)
                assert results.row_count > 0
                result = results._results.get(KNOWN_VIDEO)
                assert result is not None, "known video missing from results"
                app.play_result(result)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline and client.player.position < 1.0:
                    await pilot.pause(1.0)
                assert client.player.position >= 1.0, "TUI playback did not start"
                now_playing = app.query_one(NowPlaying)
                assert (
                    str(now_playing.query_one("#np-title").content)
                    == client.current.title
                )
                await pilot.pause(8.0)
                assert len(client.queue) > 0, "autoplay queue was not loaded"
                assert len(app.query_one(QueueList).children) > 0
                client.player.close()

        asyncio.run(scenario())
