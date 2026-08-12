"""End-to-end tests against the live YouTube API.

These require a YouTube account cookie file (defaults to ``cookie.txt`` in the
project root, overridable via ``MUSIC_CLI_COOKIE_FILE``). They verify that a
real audio stream is extracted, that real audio bytes are delivered, that the
watch playlist (autoplay queue) comes back, and that mpv actually decodes and
plays the audio.

Run with:  uv run pytest -m e2e tests/test_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from music_cli.player import Cookies, MpvPlayer, StreamExtractor, WatchPlaylist

KNOWN_VIDEO = "dQw4w9WgXcQ"  # Rick Astley - Never Gonna Give You Up

pytestmark = [pytest.mark.e2e]

COOKIE_FILE = os.environ.get("MUSIC_CLI_COOKIE_FILE", "cookie.txt")

STREAM_CMD = [
    ".venv/bin/yt-dlp",
    "--cookies", COOKIE_FILE,
    "--extractor-args", "youtube:player_client=web_embedded",
    "-f", "bestaudio",
    "-o", "-",
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
        assert data[:4] == b"\x1aE\xdf\xa3", (
            f"expected EBML (WebM/Opus) header, got {data[:4].hex()}"
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


class TestMpvPlayback:
    def test_plays_real_audio(self, stream, extractor):
        audio_output = os.environ.get("MUSIC_CLI_E2E_AO", "null")
        candidate = stream
        for attempt in range(3):
            player = MpvPlayer(audio_output=audio_output)
            try:
                player.play(candidate)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and player.position < 1.0:
                    time.sleep(0.2)
                if player.position >= 1.0:
                    assert player.audio_codec, "mpv reported no active audio codec"
                    assert player.media_title == candidate.title
                    return
            finally:
                player.close()
            candidate = extractor.resolve(stream.video_id)  # throttle: try a fresh URL
        raise AssertionError("mpv failed to decode audio after 3 attempts")


class TestTuiIntegration:
    """The TUI wired to the real client: search, play and queue end to end."""

    def test_tui_searches_plays_and_queues(self, cookies):
        import asyncio

        from music_cli.client import MusicClient
        from music_cli.tui.app import MusicTUI, QueueList, ResultsTable
        from music_cli.tui.now_playing import NowPlaying

        async def scenario():
            audio_output = os.environ.get("MUSIC_CLI_E2E_AO", "null")
            client = MusicClient(cookies=cookies, audio_output=audio_output)
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
                assert client.player.audio_codec
                now_playing = app.query_one(NowPlaying)
                assert str(now_playing.query_one("#np-title").content) == client.current.title
                await pilot.pause(8.0)
                assert len(client.queue) > 0, "autoplay queue was not loaded"
                assert len(app.query_one(QueueList).children) > 0
                client.player.close()

        asyncio.run(scenario())
