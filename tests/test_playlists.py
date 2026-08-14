"""Tests for the library playlists wrapper."""

from __future__ import annotations

import re

import pytest
import requests

from music_cli.core.errors import PlayerError
from music_cli.yt.cookies import Cookies
from music_cli.yt.playlists import (
    Library,
    LibraryPlaylist,
    _browser_auth,
    parse_library_playlist,
)

SAPISIDHASH = re.compile(r"SAPISIDHASH \d+_[0-9a-f]{40}")


class FakeApi:
    def __init__(self, playlists, tracks):
        self._playlists = playlists
        self._tracks = tracks
        self.playlist_calls = []
        self.track_calls = []
        self.create_calls = []
        self.edit_calls = []
        self.add_calls = []
        self.remove_calls = []

    def get_library_playlists(self, limit=None):
        self.playlist_calls.append(limit)
        return self._playlists

    def get_playlist(self, playlistId, limit=None, related=False, suggestions_limit=0):
        self.track_calls.append(playlistId)
        return {"tracks": self._tracks}

    def create_playlist(
        self,
        title,
        description,
        privacy_status="PRIVATE",
        video_ids=None,
        source_playlist=None,
    ):
        self.create_calls.append((title, description, privacy_status))
        return "PLNEW"

    def edit_playlist(self, playlistId, title=None, **kwargs):
        self.edit_calls.append((playlistId, title))

    def add_playlist_items(
        self, playlistId, videoIds=None, source_playlist=None, duplicates=False
    ):
        self.add_calls.append((playlistId, videoIds))

    def remove_playlist_items(self, playlistId, videos):
        self.remove_calls.append((playlistId, videos))


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


def test_library_create_playlist_defaults_private():
    api = FakeApi([], [])
    library = Library(api=api)
    assert library.create_playlist("New Mix") == "PLNEW"
    assert api.create_calls == [("New Mix", "", "PRIVATE")]


def test_library_create_playlist_handles_dict_response():
    class DictApi(FakeApi):
        def create_playlist(
            self,
            title,
            description,
            privacy_status="PRIVATE",
            video_ids=None,
            source_playlist=None,
        ):
            return {"playlistId": "PLDICT", "status": "ok"}

    assert Library(api=DictApi([], [])).create_playlist("X") == "PLDICT"


def test_library_rename_playlist():
    api = FakeApi([], [])
    Library(api=api).rename_playlist("PL1", "Renamed")
    assert api.edit_calls == [("PL1", "Renamed")]


def test_library_add_tracks():
    api = FakeApi([], [])
    Library(api=api).add_tracks("PL1", ["t1", "t2"])
    assert api.add_calls == [("PL1", ["t1", "t2"])]


def test_library_remove_track_removes_every_occurrence():
    api = FakeApi(
        [],
        [
            {"videoId": "t1", "setVideoId": "s1", "title": "One"},
            {"videoId": "t2", "setVideoId": "s2", "title": "Two"},
            {"videoId": "t1", "setVideoId": "s3", "title": "One again"},
        ],
    )
    Library(api=api).remove_track("PL1", "t1")
    assert api.remove_calls == [
        (
            "PL1",
            [
                {"videoId": "t1", "setVideoId": "s1"},
                {"videoId": "t1", "setVideoId": "s3"},
            ],
        )
    ]


def test_library_remove_track_missing_raises():
    api = FakeApi([], [{"videoId": "t1", "setVideoId": "s1"}])
    with pytest.raises(PlayerError):
        Library(api=api).remove_track("PL1", "nope")
    assert api.remove_calls == []


def browser_session():
    session = requests.Session()
    session.cookies.set("SAPISID", "yt-value", domain=".youtube.com")
    session.cookies.set("SID", "yt-sid", domain=".youtube.com")
    session.cookies.set("__Secure-3PSID", "3psid", domain=".youtube.com")
    session.cookies.set("SAPISID", "google-value", domain=".google.com")
    session.cookies.set("SID", "google-sid", domain=".google.com")
    return session


def test_browser_auth_scopes_cookies_and_signs_sapisid():
    auth = _browser_auth(browser_session())
    assert auth["origin"] == "https://music.youtube.com"
    assert auth["x-origin"] == "https://music.youtube.com"
    assert auth["x-goog-authuser"] == "0"
    assert "SAPISID=yt-value" in auth["cookie"]
    assert "google-value" not in auth["cookie"]
    assert "google-sid" not in auth["cookie"]
    assert SAPISIDHASH.fullmatch(auth["authorization"])
    assert auth["authorization"] != "SAPISIDHASH 0_0"


def test_browser_auth_signs_with_secure_papisid_fallback():
    session = requests.Session()
    session.cookies.set("__Secure-3PAPISID", "secure-value", domain=".youtube.com")
    auth = _browser_auth(session)
    assert "__Secure-3PAPISID=secure-value" in auth["cookie"]
    assert SAPISIDHASH.fullmatch(auth["authorization"])
