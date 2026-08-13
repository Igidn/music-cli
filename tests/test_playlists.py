"""Tests for the library playlists wrapper."""

from __future__ import annotations

import json

import pytest

from music_cli.player import Cookies, PlayerError
from music_cli.playlists import Library, LibraryPlaylist, parse_library_playlist


class FakeApi:
    def __init__(self, playlists, tracks):
        self._playlists = playlists
        self._tracks = tracks
        self.playlist_calls = []
        self.track_calls = []

    def get_library_playlists(self, limit=None):
        self.playlist_calls.append(limit)
        return self._playlists

    def get_playlist(self, playlistId, limit=None, related=False, suggestions_limit=0):
        self.track_calls.append(playlistId)
        return {"tracks": self._tracks}


def raw_playlist(playlist_id="PL1", title="My Mix", count="25"):
    return {"playlistId": playlist_id, "title": title, "count": count}


def raw_track(video_id="t1", title="Track One", artists=("Artist A",)):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist} for artist in artists],
        "length": "3:00",
        "videoType": "MUSIC_VIDEO_TYPE_ATV",
    }


def test_parse_library_playlist_maps_fields():
    parsed = parse_library_playlist(raw_playlist())
    assert parsed == LibraryPlaylist(
        playlist_id="PL1", title="My Mix", track_count="25"
    )


def test_parse_library_playlist_defaults():
    parsed = parse_library_playlist({"playlistId": "PL2"})
    assert parsed == LibraryPlaylist(
        playlist_id="PL2", title="Untitled playlist", track_count=""
    )


def test_library_playlists_parses_and_skips_missing_ids():
    api = FakeApi(
        [raw_playlist(), {"playlistId": "", "title": "Broken", "count": "1"}], []
    )
    library = Library(api=api, cookies=Cookies.from_file("cookie.txt"))
    playlists = library.playlists()
    assert playlists == [
        LibraryPlaylist(playlist_id="PL1", title="My Mix", track_count="25")
    ]
    assert api.playlist_calls == [25]


def test_library_playlists_honors_limit():
    api = FakeApi([raw_playlist()], [])
    Library(api=api).playlists(limit=10)
    assert api.playlist_calls == [10]


def test_library_tracks_parses_and_skips_unavailable():
    api = FakeApi(
        [],
        [
            raw_track(video_id="t1", title="One"),
            {"videoId": None, "title": "Unavailable"},
            raw_track(video_id="t2", title="Two", artists=("A", "B")),
        ],
    )
    library = Library(api=api)
    tracks = library.tracks("PL1")
    assert [track.video_id for track in tracks] == ["t1", "t2"]
    assert tracks[0].title == "One"
    assert tracks[1].artists == ["A", "B"]
    assert tracks[1].duration == "3:00"
    assert api.track_calls == ["PL1"]


def test_library_authenticated_reflects_cookies():
    assert not Library(api=FakeApi([], [])).authenticated
    assert Library(
        api=FakeApi([], []), cookies=Cookies.from_file("c.txt")
    ).authenticated


def oauth_json(tmp_path, **overrides):
    data = {
        "access_token": "token",
        "refresh_token": "refresh",
        "scope": "https://www.googleapis.com/auth/youtube",
        "token_type": "Bearer",
        "expires_at": 0,
        "expires_in": 3600,
        "client_id": "client-id",
        "client_secret": "client-secret",
    }
    data.update(overrides)
    path = tmp_path / "oauth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_library_authenticated_reflects_oauth_file(tmp_path):
    assert Library(api=FakeApi([], []), oauth_file=oauth_json(tmp_path)).authenticated


def test_library_oauth_credentials_loads_from_token_file(tmp_path):
    library = Library(oauth_file=oauth_json(tmp_path))
    credentials = library._oauth_credentials()
    assert credentials.client_id == "client-id"
    assert credentials.client_secret == "client-secret"  # noqa: S105


def test_library_oauth_credentials_missing_file_raises(tmp_path):
    library = Library(oauth_file=str(tmp_path / "nope.json"))
    with pytest.raises(PlayerError, match="not found"):
        library._oauth_credentials()


def test_library_oauth_credentials_missing_ids_raises(tmp_path):
    library = Library(
        oauth_file=oauth_json(tmp_path, client_id=None, client_secret=None)
    )
    with pytest.raises(PlayerError, match="missing client_id"):
        library._oauth_credentials()
