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
import shutil
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


def _stream_cmd() -> list[str]:
    """yt-dlp CLI invocation mirroring StreamExtractor: yt-dlp only
    auto-enables deno, so pass an explicit --js-runtimes override when
    deno is absent."""
    cmd = [".venv/bin/yt-dlp"]
    if not shutil.which("deno"):
        for runtime in ("node", "bun"):
            if shutil.which(runtime):
                cmd += ["--js-runtimes", runtime]
                break
    return [
        *cmd,
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
    return StreamExtractor()


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
            [*_stream_cmd(), f"https://www.youtube.com/watch?v={KNOWN_VIDEO}"],
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


@pytest.mark.skipif(
    _AVFoundationPlayer is None, reason="pyobjc-avfoundation not installed"
)
class TestAVFoundationPlayback:
    AVFoundationPlayer = _AVFoundationPlayer

    def test_plays_real_audio(self, cookies, stream, extractor):
        candidate = stream
        for _attempt in range(3):
            player = self.AVFoundationPlayer()
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
            player = self.AVFoundationPlayer()
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
    """The TUI wired to the daemon: search, play and queue end to end."""

    def test_tui_searches_plays_and_queues(self, cookies, tmp_path, monkeypatch):
        import asyncio

        from music_cli import ipc
        from music_cli.client import MusicClient
        from music_cli.tui.app import MusicTUI
        from music_cli.tui.components import QueueList, ResultsTable
        from music_cli.tui.components.now_playing import NowPlaying

        # The TUI resolves playback over IPC against the daemon, which owns
        # the player. Isolate that daemon's sockets and state under tmp_path
        # so the test never talks to (or spawns into) the user's live setup.
        monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path / "config"))

        async def scenario():
            client = MusicClient(cookies=cookies)
            app = MusicTUI(client)
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app.query_one("#search-input").value = "never gonna give you up"
                    await pilot.pause(3.0)
                    results = app.query_one(ResultsTable)
                    assert results.row_count > 0
                    result = results._results.get(KNOWN_VIDEO)
                    assert result is not None, "known video missing from results"
                    app.play_result(result)
                    # Playback happens in the daemon process; the TUI mirrors
                    # it through state pushes. Poll the daemon's status.
                    deadline = time.monotonic() + 180
                    status: dict = {"state": "stopped"}
                    while time.monotonic() < deadline:
                        await pilot.pause(1.0)
                        response = ipc.send_request({"cmd": "status"}, timeout=5.0)
                        assert response.get("ok"), response.get("error")
                        status = response["data"]
                        if (
                            status.get("state") == "playing"
                            and status.get("position", 0.0) >= 1.0
                        ):
                            break
                    assert status.get("state") == "playing", (
                        "TUI playback did not start"
                    )
                    assert status["position"] >= 1.0
                    assert status["track"]["video_id"] == KNOWN_VIDEO
                    now_playing = app.query_one(NowPlaying)
                    assert (
                        str(now_playing.query_one("#np-title").content)
                        == status["track"]["title"]
                    )
                    # Autoplay queue is built by the daemon after playback
                    # starts; the TUI renders it from pushed state events.
                    deadline = time.monotonic() + 30
                    queue: list = []
                    while time.monotonic() < deadline:
                        await pilot.pause(1.0)
                        response = ipc.send_request({"cmd": "queue"}, timeout=10.0)
                        assert response.get("ok"), response.get("error")
                        queue = response["data"]
                        if queue:
                            break
                    assert len(queue) > 0, "autoplay queue was not loaded"
                    assert len(app.query_one(QueueList).children) > 0
            finally:
                client.player.close()

        try:
            ipc.ensure_daemon(cookies=os.path.abspath(COOKIE_FILE))
            asyncio.run(scenario())
        finally:
            try:
                ipc.send_request({"cmd": "quit"}, timeout=5.0)
            except Exception:  # noqa: BLE001, S110 — daemon may already be gone
                pass
