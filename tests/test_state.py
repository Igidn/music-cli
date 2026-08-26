"""Tests for play history and settings persistence."""

from __future__ import annotations

from music_cli.storage.state import (
    STATE_DB_FILENAME,
    DownloadsStore,
    PlayedTrack,
    PlayHistoryStore,
    SettingsStore,
)


class TestPlayHistoryStore:
    def test_defaults_to_config_dir(self):
        from music_cli.core.paths import config_dir

        assert PlayHistoryStore().path == config_dir() / STATE_DB_FILENAME

    def test_round_trip(self, tmp_path):
        store = PlayHistoryStore(tmp_path / "music-cli.db")
        assert store.most_recent() is None

        store.record(
            PlayedTrack(video_id="v1", title="Song", artists=("A", "B"), duration=213.0)
        )
        loaded = store.most_recent()
        assert loaded.video_id == "v1"
        assert loaded.title == "Song"
        assert loaded.artists == ("A", "B")
        assert loaded.duration == 213.0
        assert loaded.played == 1
        assert loaded.last_played is not None

    def test_record_increments_play_count(self, tmp_path):
        store = PlayHistoryStore(tmp_path / "music-cli.db")
        store.record(PlayedTrack(video_id="v1", title="Song"))
        store.record(PlayedTrack(video_id="v1", title="Song"))
        store.record(PlayedTrack(video_id="v1", title="Song"))
        assert store.most_recent().played == 3

    def test_record_refreshes_title_and_duration(self, tmp_path):
        store = PlayHistoryStore(tmp_path / "music-cli.db")
        store.record(PlayedTrack(video_id="v1", title="Old Title", duration=100.0))
        store.record(PlayedTrack(video_id="v1", title="New Title", duration=200.0))
        track = store.most_recent()
        assert track.title == "New Title"
        assert track.duration == 200.0
        assert track.played == 2

    def test_most_recent_orders_by_last_played(self, tmp_path):
        store = PlayHistoryStore(tmp_path / "music-cli.db")
        store.record(PlayedTrack(video_id="v1", title="First"))
        store.record(PlayedTrack(video_id="v2", title="Second"))
        assert store.most_recent().video_id == "v2"
        assert store.most_recent().played == 1

    def test_top_orders_by_play_count(self, tmp_path):
        store = PlayHistoryStore(tmp_path / "music-cli.db")
        store.record(PlayedTrack(video_id="a", title="A"))
        store.record(PlayedTrack(video_id="b", title="B"))
        store.record(PlayedTrack(video_id="b", title="B"))
        store.record(PlayedTrack(video_id="c", title="C"))
        top = store.top(10)
        assert [track.video_id for track in top] == ["b", "c", "a"]
        assert top[0].played == 2

    def test_top_respects_limit(self, tmp_path):
        store = PlayHistoryStore(tmp_path / "music-cli.db")
        for video_id in ("a", "b", "c"):
            store.record(PlayedTrack(video_id=video_id, title=video_id))
        assert [track.video_id for track in store.top(2)] == ["c", "b"]

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "music-cli.db"
        PlayHistoryStore(path).record(PlayedTrack(video_id="v1", title="Song"))
        assert PlayHistoryStore(path).most_recent().video_id == "v1"

    def test_recovers_from_corrupt_database(self, tmp_path):
        path = tmp_path / "music-cli.db"
        path.write_bytes(b"not a database")
        store = PlayHistoryStore(path)
        store.record(PlayedTrack(video_id="v1", title="Song"))
        assert store.most_recent().video_id == "v1"


class TestSettingsStore:
    def test_defaults_to_config_dir(self):
        from music_cli.core.paths import config_dir

        assert SettingsStore().path == config_dir() / STATE_DB_FILENAME

    def test_round_trip(self, tmp_path):
        store = SettingsStore(tmp_path / "music-cli.db")
        assert store.get("volume") is None
        store.set("volume", 85)
        store.set("muted", True)
        store.set("loop", False)
        assert store.get("volume") == "85"
        assert store.get_int("volume") == 85
        assert store.get_bool("muted") is True
        assert store.get_bool("loop") is False

    def test_get_defaults(self, tmp_path):
        store = SettingsStore(tmp_path / "music-cli.db")
        assert store.get("missing") is None
        assert store.get("missing", "fallback") == "fallback"
        assert store.get_int("missing", 42) == 42
        assert store.get_bool("missing", True) is True

    def test_delete(self, tmp_path):
        store = SettingsStore(tmp_path / "music-cli.db")
        store.set("volume", 85)
        store.delete("volume")
        assert store.get("volume") is None

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "music-cli.db"
        SettingsStore(path).set("volume", 70)
        assert SettingsStore(path).get_int("volume") == 70


class TestDownloadsStore:
    def test_record_then_recent(self, tmp_path):
        store = DownloadsStore(tmp_path / "music-cli.db")
        assert store.recent() == []
        store.record("v1", "Song", ("A",), 213.0)
        tracks = store.recent()
        assert len(tracks) == 1
        assert tracks[0].video_id == "v1"
        assert tracks[0].title == "Song"
        assert tracks[0].artists == ("A",)
        assert tracks[0].duration == 213.0
        assert tracks[0].downloaded_at is not None

    def test_recent_orders_newest_first(self, tmp_path):
        store = DownloadsStore(tmp_path / "music-cli.db")
        store.record("a", "A")
        store.record("b", "B")
        assert [t.video_id for t in store.recent()] == ["b", "a"]

    def test_record_refreshes_and_moves_to_top(self, tmp_path):
        store = DownloadsStore(tmp_path / "music-cli.db")
        store.record("a", "Old", ("X",), 100.0)
        store.record("b", "B")
        store.record("a", "New", ("Y",), 200.0)
        tracks = store.recent()
        assert [t.video_id for t in tracks] == ["a", "b"]
        a = tracks[0]
        assert a.title == "New" and a.artists == ("Y",) and a.duration == 200.0

    def test_remove(self, tmp_path):
        store = DownloadsStore(tmp_path / "music-cli.db")
        store.record("a", "A")
        store.record("b", "B")
        store.remove("a")
        assert [t.video_id for t in store.recent()] == ["b"]

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "music-cli.db"
        DownloadsStore(path).record("v1", "Song")
        assert DownloadsStore(path).recent()[0].video_id == "v1"
