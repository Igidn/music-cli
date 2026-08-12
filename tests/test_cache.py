from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from music_cli.cache import (
    AudioCache,
    CachedTrack,
    DownloadResult,
    TrackMeta,
    default_cache_dir,
)


def seed(
    cache: AudioCache,
    video_id: str = "abc",
    *,
    title: str = "Some Song",
    ext: str = "m4a",
    content: bytes = b"audio-bytes",
) -> Path:
    target = cache.tmp_path(video_id)
    src = Path(f"{target}.{ext}")
    src.write_bytes(content)
    return cache.commit(
        video_id,
        TrackMeta(title=title, artists=("Artist",), duration=213.0, ext=ext),
        src=src,
    )


@pytest.fixture
def cache(tmp_path):
    return AudioCache(directory=tmp_path / "cache")


class TestAudioCache:
    def test_commit_then_lookup(self, cache):
        seed(cache)
        track = cache.lookup("abc")
        assert isinstance(track, CachedTrack)
        assert track.video_id == "abc"
        assert track.title == "Some Song"
        assert track.artists == ("Artist",)
        assert track.duration == 213.0
        assert track.ext == "m4a"
        assert track.size == len(b"audio-bytes")
        assert cache.path_for("abc") == cache.directory / "tracks" / "abc.m4a"

    def test_lookup_unknown_returns_none(self, cache):
        assert cache.lookup("nope") is None
        assert cache.path_for("nope") is None

    def test_lookup_drops_entry_when_file_missing(self, cache):
        path = seed(cache)
        path.unlink()
        assert cache.lookup("abc") is None
        assert cache.path_for("abc") is None

    def test_commit_moves_file_atomically(self, cache):
        path = seed(cache)
        assert path.is_file()
        assert path.name == "abc.m4a"
        assert not list(cache._tracks_dir.glob(".abc-*"))

    def test_evict_by_count_lru(self, cache):
        cache.max_entries = 2
        for video_id in ("a", "b", "c"):
            seed(cache, video_id)
        assert cache.lookup("a") is None
        assert cache.lookup("b") is not None
        assert cache.lookup("c") is not None

    def test_evict_by_size(self, cache):
        cache.max_entries = 100
        cache.max_size = 10
        seed(cache, "a", content=b"123456")
        seed(cache, "b", content=b"123456")
        assert cache.lookup("a") is None
        assert cache.lookup("b") is not None

    def test_evict_by_ttl(self, cache):
        cache.ttl = timedelta(seconds=3600)
        seed(cache, "a")
        cache._entries["a"]["last_used"] = time.time() - 7200
        cache.evict()
        assert cache.lookup("a") is None

    def test_discard(self, cache):
        path = seed(cache, "a")
        cache.discard("a")
        assert cache.lookup("a") is None
        assert not path.exists()
        cache.discard("a")

    def test_persists_across_instances(self, tmp_path):
        directory = tmp_path / "cache"
        first = AudioCache(directory=directory)
        seed(first, "abc")
        first.close()
        second = AudioCache(directory=directory)
        track = second.lookup("abc")
        assert track is not None
        assert track.title == "Some Song"
        assert second.path_for("abc").is_file()

    def test_adopts_orphan_files(self, tmp_path):
        directory = tmp_path / "cache"
        AudioCache(directory=directory).close()
        orphan = directory / "tracks" / "orphan.m4a"
        orphan.write_bytes(b"audio")
        cache = AudioCache(directory=directory)
        track = cache.lookup("orphan")
        assert track is not None
        assert track.ext == "m4a"

    def test_corrupt_index_recovers(self, tmp_path):
        directory = tmp_path / "cache"
        index = directory / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("{not json")
        cache = AudioCache(directory=directory)
        seed(cache, "abc")
        assert cache.lookup("abc") is not None

    def test_clear(self, cache):
        seed(cache, "a")
        seed(cache, "b")
        cache.clear()
        assert cache.lookup("a") is None
        assert cache.lookup("b") is None
        assert not list(cache._tracks_dir.iterdir())

    def test_get_or_download_returns_cached(self, cache):
        path = seed(cache, "abc")
        calls = []

        def downloader(target):
            calls.append(target)
            src = Path(f"{target}.m4a")
            src.write_bytes(b"x")
            return DownloadResult(str(src), TrackMeta(title="T", ext="m4a"))

        assert cache.get_or_download("abc", downloader) == path
        assert calls == []

    def test_single_flight_download(self, cache):
        started = threading.Event()
        release = threading.Event()
        calls = []
        results = []

        def downloader(target):
            calls.append(target)
            started.set()
            release.wait(timeout=5)
            src = Path(f"{target}.m4a")
            src.write_bytes(b"audio")
            return DownloadResult(str(src), TrackMeta(title="T", ext="m4a"))

        def run():
            results.append(cache.get_or_download("v1", downloader))

        first = threading.Thread(target=run)
        first.start()
        assert started.wait(5)
        second = threading.Thread(target=run)
        second.start()
        time.sleep(0.2)
        assert len(calls) == 1
        release.set()
        first.join(10)
        second.join(10)
        assert len(calls) == 1
        assert results[0] == results[1]
        assert results[0].is_file()
        assert cache.lookup("v1") is not None

    def test_download_failure_frees_slot_and_cleans_up(self, cache):
        attempts = []

        def downloader(target):
            attempts.append(target)
            src = Path(f"{target}.m4a")
            src.write_bytes(b"audio")
            if len(attempts) == 1:
                raise OSError("boom")
            return DownloadResult(str(src), TrackMeta(title="T", ext="m4a"))

        with pytest.raises(OSError, match="boom"):
            cache.get_or_download("v1", downloader)
        assert cache._inflight == {}
        assert not list(cache._tracks_dir.glob(".v1-*"))
        assert cache.get_or_download("v1", downloader) is not None
        assert len(attempts) == 2


def test_default_cache_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_CLI_CACHE_DIR", str(tmp_path / "override"))
    assert default_cache_dir() == tmp_path / "override"
