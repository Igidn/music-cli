"""Tests for last-played-track persistence."""

from __future__ import annotations

import json

from music_cli.state import STATE_FILENAME, LastTrack, LastTrackStore


def test_store_defaults_to_config_dir(tmp_path):
    from music_cli.login import config_dir

    store = LastTrackStore()
    assert store.path == config_dir() / STATE_FILENAME


def test_store_round_trip(tmp_path):
    store = LastTrackStore(tmp_path / "last-track.json")
    assert store.load() is None

    store.save(
        LastTrack(video_id="v1", title="Song", artists=("A", "B"), duration=213.0)
    )
    loaded = store.load()
    assert loaded == LastTrack(
        video_id="v1", title="Song", artists=("A", "B"), duration=213.0
    )


def test_store_round_trip_minimal_track(tmp_path):
    path = tmp_path / "last-track.json"
    LastTrackStore(path).save(LastTrack(video_id="v2", title=""))
    assert LastTrackStore(path).load() == LastTrack(video_id="v2", title="v2")


def test_store_ignores_corrupt_file(tmp_path):
    path = tmp_path / "last-track.json"
    path.write_text("{not json")
    assert LastTrackStore(path).load() is None


def test_store_rejects_missing_video_id(tmp_path):
    path = tmp_path / "last-track.json"
    path.write_text(json.dumps({"title": "No id"}))
    assert LastTrackStore(path).load() is None


def test_store_save_overwrites(tmp_path):
    path = tmp_path / "last-track.json"
    store = LastTrackStore(path)
    store.save(LastTrack(video_id="v1", title="First"))
    store.save(LastTrack(video_id="v2", title="Second"))
    assert store.load() == LastTrack(video_id="v2", title="Second")