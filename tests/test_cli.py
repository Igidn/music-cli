from __future__ import annotations

import json

import pytest

import music_cli.cli as cli
from music_cli import build_parser, ipc
from music_cli.core.errors import PlayerError
from music_cli.storage.state import PlayedTrack, PlayHistoryStore
from music_cli.yt.extract import PlaylistTrack
from music_cli.yt.playlists import LibraryPlaylist
from music_cli.yt.search import SearchResult

STATUS = {
    "state": "playing",
    "track": {"video_id": "abc", "title": "Some Song", "artists": ["Some Artist"]},
    "position": 72.0,
    "duration": 201.0,
    "volume": 80,
    "muted": False,
    "loop": False,
    "auto_next": True,
    "queue": [
        {
            "video_id": "q1",
            "title": "Next One",
            "artists": ["Artist B"],
            "duration": "2:01",
        },
    ],
}


def parse(*argv):
    return build_parser().parse_args(list(argv))


def make_result(video_id="abc", title="Some Song"):
    return SearchResult(
        result_type="song",
        title=title,
        artists=["Some Artist"],
        album="An Album",
        duration="3:21",
        video_id=video_id,
        browse_id="",
        year="2024",
        raw={},
    )


class FakeDaemon:
    """Records IPC requests and answers with canned responses."""

    def __init__(self):
        self.requests = []
        self.data = dict(STATUS)
        self.ok = True
        self.error = "boom"

    def send(self, request, timeout=30.0):
        self.requests.append(request)
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {"ok": True, "data": self.data}

    def send_play(self, request, timeout=180.0, on_progress=None):
        return self.send(request)


@pytest.fixture
def daemon(monkeypatch):
    fake = FakeDaemon()
    monkeypatch.setattr(
        ipc, "ensure_daemon", lambda cookies=None, volume=None: None, raising=False
    )
    monkeypatch.setattr(ipc, "send_request", fake.send, raising=False)
    monkeypatch.setattr(ipc, "send_play_request", fake.send_play, raising=False)
    return fake


class FakeLibrary:
    authenticated = True

    def __init__(self):
        self.calls = []

    def playlists(self):
        return [LibraryPlaylist("PL1", "My Mix", "12")]

    def tracks(self, playlist_id):
        self.calls.append(("tracks", playlist_id))
        return [
            PlaylistTrack(
                video_id="t1",
                title="Track One",
                artists=["Artist A"],
                duration="3:00",
            )
        ]

    def create_playlist(self, title):
        self.calls.append(("create", title))
        return "PLNEW"

    def rename_playlist(self, playlist_id, title):
        self.calls.append(("rename", playlist_id, title))

    def add_tracks(self, playlist_id, video_ids):
        self.calls.append(("add", playlist_id, video_ids))

    def remove_track(self, playlist_id, video_id):
        self.calls.append(("remove", playlist_id, video_id))


class FakeClient:
    def __init__(self):
        self.library = FakeLibrary()
        self.search_calls = []
        self.results = [make_result()]

    def search(self, query, limit=20, filter=None):
        self.search_calls.append((query, limit, filter))
        return self.results


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli, "build_client", lambda args: client)
    return client


class TestArgParsing:
    def test_play_query(self):
        args = parse("play", "never gonna")
        assert args.command == "play"
        assert args.query == "never gonna"
        assert args.video_id is None
        assert args.playlist is None
        assert args.loop is False
        assert args.auto_next is None
        assert args.play_volume is None

    def test_play_video_id(self):
        args = parse("play", "--video-id", "abc")
        assert args.video_id == "abc"
        assert args.query is None

    def test_play_playlist_with_flags(self):
        args = parse("play", "--playlist", "PL1", "--no-auto-next", "--volume", "50")
        assert args.playlist == "PL1"
        assert args.auto_next is False
        assert args.play_volume == 50

    def test_play_loop(self):
        assert parse("play", "x", "--loop").loop is True

    def test_seek_values(self):
        assert parse("seek", "+5").value == ("offset", 5.0)
        assert parse("seek", "-5").value == ("offset", -5.0)
        assert parse("seek", "90").value == ("position", 90.0)

    def test_volume_values(self):
        assert parse("volume", "65").value == ("level", 65)
        assert parse("volume", "+5").value == ("delta", 5)
        assert parse("volume", "-5").value == ("delta", -5)

    def test_state_commands(self):
        assert parse("mute", "on").state == "on"
        assert parse("loop", "off").state == "off"
        args = parse("auto-next", "toggle")
        assert args.command == "auto-next"
        assert args.state == "toggle"

    def test_status_json(self):
        assert parse("status", "--json").json is True
        assert parse("status").json is False

    def test_search(self):
        args = parse("search", "a", "b", "--limit", "5", "--filter", "songs")
        assert args.query == ["a", "b"]
        assert args.limit == 5
        assert args.filter == "songs"
        assert args.json is False

    def test_playlists_subcommands(self):
        args = parse("playlists", "list", "--json")
        assert (args.command, args.playlists_command, args.json) == (
            "playlists",
            "list",
            True,
        )
        assert parse("playlists", "tracks", "PL1").id == "PL1"
        assert parse("playlists", "create", "My Mix").name == "My Mix"
        args = parse("playlists", "rename", "PL1", "New Name")
        assert (args.id, args.name) == ("PL1", "New Name")
        args = parse("playlists", "add", "PL1", "v1", "v2")
        assert args.video_ids == ["v1", "v2"]
        args = parse("playlists", "remove", "PL1", "v1")
        assert (args.id, args.video_id) == ("PL1", "v1")

    def test_history(self):
        args = parse("history", "--limit", "3")
        assert args.limit == 3
        assert args.json is False

    def test_download(self):
        args = parse("download", "dQw4w9WgXcQ")
        assert args.video_id == "dQw4w9WgXcQ"

    def test_playlists_downloaded(self):
        args = parse("playlists", "downloaded", "--json")
        assert args.playlists_command == "downloaded"
        assert args.json is True

    def test_play_requires_a_target(self):
        with pytest.raises(SystemExit) as error:
            parse("play")
        assert error.value.code == 2

    def test_play_rejects_multiple_targets(self):
        with pytest.raises(SystemExit) as error:
            parse("play", "query", "--video-id", "abc")
        assert error.value.code == 2

    def test_seek_rejects_junk(self):
        with pytest.raises(SystemExit) as error:
            parse("seek", "junk")
        assert error.value.code == 2


class TestDaemonCommands:
    def test_play_with_video_id_and_loop(self, daemon, capsys):
        assert cli.run(parse("play", "--video-id", "abc", "--loop")) == 0
        assert daemon.requests == [{"cmd": "play", "video_id": "abc", "loop": True}]
        out = capsys.readouterr().out
        assert "Playing" in out
        assert "Some Song" in out

    def test_play_query(self, daemon):
        assert cli.run(parse("play", "never gonna")) == 0
        assert daemon.requests == [{"cmd": "play", "query": "never gonna"}]

    def test_play_omits_unset_flags(self, daemon):
        assert cli.run(parse("play", "x")) == 0
        assert daemon.requests == [{"cmd": "play", "query": "x"}]

    def test_play_volume(self, daemon):
        assert cli.run(parse("play", "x", "--volume", "50")) == 0
        assert daemon.requests == [{"cmd": "play", "query": "x", "volume": 50}]

    def test_play_starts_the_daemon(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ipc,
            "ensure_daemon",
            lambda cookies=None, volume=None: calls.append(cookies),
            raising=False,
        )
        fake = FakeDaemon()
        monkeypatch.setattr(ipc, "send_request", fake.send, raising=False)
        monkeypatch.setattr(ipc, "send_play_request", fake.send_play, raising=False)
        assert cli.run(parse("play", "x")) == 0
        assert len(calls) == 1

    def test_pause(self, daemon, capsys):
        daemon.data = {"state": "paused", "track": STATUS["track"]}
        assert cli.run(parse("pause")) == 0
        assert daemon.requests == [{"cmd": "pause"}]
        out = capsys.readouterr().out
        assert "Paused" in out
        assert "Some Song" in out

    def test_transport_commands(self, daemon, capsys):
        for command in ("resume", "toggle", "next"):
            assert cli.run(parse(command)) == 0
            assert daemon.requests[-1] == {"cmd": command}
            assert "Some Song" in capsys.readouterr().out

    def test_nothing_playing(self, daemon, capsys):
        daemon.data = {"state": "stopped", "track": None}
        assert cli.run(parse("next")) == 0
        assert "Nothing is playing" in capsys.readouterr().out

    def test_stop(self, daemon, capsys):
        daemon.data = {"state": "stopped", "track": None}
        assert cli.run(parse("stop")) == 0
        assert daemon.requests == [{"cmd": "stop"}]
        assert "Stopped" in capsys.readouterr().out

    def test_seek_offset(self, daemon, capsys):
        daemon.data = {"position": 102.0}
        assert cli.run(parse("seek", "+5")) == 0
        assert daemon.requests == [{"cmd": "seek", "offset": 5.0}]
        assert "Position 1:42" in capsys.readouterr().out

    def test_seek_absolute(self, daemon):
        daemon.data = {"position": 90.0}
        assert cli.run(parse("seek", "90")) == 0
        assert daemon.requests == [{"cmd": "seek", "position": 90.0}]

    def test_volume_absolute(self, daemon, capsys):
        daemon.data = {"volume": 65}
        assert cli.run(parse("volume", "65")) == 0
        assert daemon.requests == [{"cmd": "volume", "level": 65}]
        assert "Volume 65" in capsys.readouterr().out

    def test_volume_relative(self, daemon):
        assert cli.run(parse("volume", "-5")) == 0
        assert daemon.requests == [{"cmd": "volume", "delta": -5}]

    def test_mute_toggle(self, daemon, capsys):
        daemon.data = {"muted": True}
        assert cli.run(parse("mute", "toggle")) == 0
        assert daemon.requests == [{"cmd": "mute", "state": "toggle"}]
        assert "Muted" in capsys.readouterr().out

    def test_loop_on(self, daemon, capsys):
        assert cli.run(parse("loop", "on")) == 0
        assert daemon.requests == [{"cmd": "loop", "state": "on"}]
        assert "Loop on" in capsys.readouterr().out

    def test_auto_next_off(self, daemon, capsys):
        assert cli.run(parse("auto-next", "off")) == 0
        assert daemon.requests == [{"cmd": "auto_next", "state": "off"}]
        assert "Auto-next off" in capsys.readouterr().out

    def test_status_human(self, daemon, capsys):
        assert cli.run(parse("status")) == 0
        assert daemon.requests == [{"cmd": "status"}]
        out = capsys.readouterr().out
        assert "Playing" in out
        assert "Some Song" in out
        assert "1:12 / 3:21" in out
        assert "volume 80" in out
        assert "auto-next on" in out
        assert "1 queued" in out

    def test_status_stopped(self, daemon, capsys):
        daemon.data = {"state": "stopped", "track": None, "volume": 80, "queue": []}
        assert cli.run(parse("status")) == 0
        out = capsys.readouterr().out
        assert "Nothing is playing" in out
        assert "volume 80" in out

    def test_status_json(self, daemon, capsys):
        assert cli.run(parse("status", "--json")) == 0
        assert json.loads(capsys.readouterr().out) == daemon.data

    def test_queue_human(self, daemon, capsys):
        daemon.data = STATUS["queue"]
        assert cli.run(parse("queue")) == 0
        assert daemon.requests == [{"cmd": "queue"}]
        out = capsys.readouterr().out
        assert "Up next — 1 tracks" in out
        assert "Next One" in out

    def test_queue_json(self, daemon, capsys):
        daemon.data = STATUS["queue"]
        assert cli.run(parse("queue", "--json")) == 0
        assert json.loads(capsys.readouterr().out) == STATUS["queue"]

    def test_error_response(self, daemon, capsys):
        daemon.ok = False
        daemon.error = "boom"
        assert cli.run(parse("status")) == 1
        assert "boom" in capsys.readouterr().err

    def test_download_sends_to_daemon(self, daemon, capsys):
        assert cli.run(parse("download", "dQw4w9WgXcQ")) == 0
        assert daemon.requests == [{"cmd": "download", "video_id": "dQw4w9WgXcQ"}]
        assert "Downloaded dQw4w9WgXcQ" in capsys.readouterr().out

    def test_download_error_response(self, daemon, capsys):
        daemon.ok = False
        daemon.error = "no streams found"
        assert cli.run(parse("download", "abc")) == 1
        assert "no streams found" in capsys.readouterr().err

    def test_download_not_running(self, monkeypatch, capsys):
        def dead(request, timeout=1200.0, on_progress=None):
            raise PlayerError("the daemon is not running")

        monkeypatch.setattr(
            ipc, "ensure_daemon", lambda cookies=None, volume=None: None, raising=False
        )
        monkeypatch.setattr(ipc, "send_play_request", dead)
        assert cli.run(parse("download", "abc")) == 1
        assert "not running" in capsys.readouterr().err

    def test_daemon_not_running(self, monkeypatch, capsys):
        def dead(request, timeout=30.0):
            raise PlayerError("the daemon is not running")

        monkeypatch.setattr(
            ipc, "ensure_daemon", lambda cookies=None, volume=None: None, raising=False
        )
        monkeypatch.setattr(ipc, "send_request", dead)
        assert cli.run(parse("status")) == 1
        assert "not running" in capsys.readouterr().err


class TestEnsureBeforeCommands:
    """Every daemon-backed command respawns the daemon before sending."""

    @pytest.mark.parametrize(
        "argv",
        [
            ("pause",),
            ("toggle",),
            ("next",),
            ("stop",),
            ("seek", "90"),
            ("volume", "65"),
            ("mute", "on"),
            ("loop", "off"),
            ("auto-next", "on"),
            ("status",),
            ("queue",),
        ],
    )
    def test_ensures_then_sends(self, monkeypatch, argv):
        fake = FakeDaemon()
        if argv[0] == "queue":
            fake.data = STATUS["queue"]
        calls = []
        monkeypatch.setattr(
            ipc,
            "ensure_daemon",
            lambda cookies=None, volume=None: calls.append(cookies),
            raising=False,
        )
        monkeypatch.setattr(ipc, "send_request", fake.send)
        assert cli.run(parse(*argv)) == 0
        assert len(calls) == 1

    def test_resume_ensures(self, monkeypatch):
        fake = FakeDaemon()
        calls = []
        monkeypatch.setattr(
            ipc,
            "ensure_daemon",
            lambda cookies=None, volume=None: calls.append(cookies),
            raising=False,
        )
        monkeypatch.setattr(ipc, "send_request", fake.send)
        assert cli.run(parse("resume")) == 0
        assert len(calls) == 1


class TestSearch:
    def test_human_table(self, fake_client, capsys):
        assert cli.run(parse("search", "a", "b", "--limit", "5")) == 0
        assert fake_client.search_calls == [("a b", 5, None)]
        out = capsys.readouterr().out
        assert "Some Song" in out
        assert "Title" in out

    def test_filter_passed_through(self, fake_client):
        assert cli.run(parse("search", "a", "--filter", "songs")) == 0
        assert fake_client.search_calls == [("a", 10, "songs")]

    def test_json_lines(self, fake_client, capsys):
        fake_client.results = [make_result("v1", "One"), make_result("v2", "Two")]
        assert cli.run(parse("search", "a", "--json")) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert set(obj) == {
                "video_id",
                "title",
                "artists",
                "album",
                "duration",
                "type",
            }
        assert json.loads(lines[0])["title"] == "One"

    def test_no_results(self, fake_client, capsys):
        fake_client.results = []
        assert cli.run(parse("search", "a")) == 0
        assert "No results" in capsys.readouterr().out


class TestPlaylists:
    def test_list_human(self, fake_client, capsys):
        assert cli.run(parse("playlists", "list")) == 0
        out = capsys.readouterr().out
        assert "My Mix" in out
        assert "PL1" in out

    def test_list_json(self, fake_client, capsys):
        assert cli.run(parse("playlists", "list", "--json")) == 0
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj == {"playlist_id": "PL1", "title": "My Mix", "track_count": "12"}

    def test_tracks_human(self, fake_client, capsys):
        assert cli.run(parse("playlists", "tracks", "PL1")) == 0
        assert fake_client.library.calls == [("tracks", "PL1")]
        assert "Track One" in capsys.readouterr().out

    def test_tracks_json(self, fake_client, capsys):
        assert cli.run(parse("playlists", "tracks", "PL1", "--json")) == 0
        obj = json.loads(capsys.readouterr().out.strip())
        assert set(obj) == {"video_id", "title", "artists", "duration"}
        assert obj["video_id"] == "t1"

    def test_create(self, fake_client, capsys):
        assert cli.run(parse("playlists", "create", "My Mix")) == 0
        assert fake_client.library.calls == [("create", "My Mix")]
        assert "Created playlist “My Mix” (PLNEW)" in capsys.readouterr().out

    def test_rename(self, fake_client, capsys):
        assert cli.run(parse("playlists", "rename", "PL1", "New Name")) == 0
        assert fake_client.library.calls == [("rename", "PL1", "New Name")]
        assert "Renamed playlist to “New Name”" in capsys.readouterr().out

    def test_add(self, fake_client, capsys):
        assert cli.run(parse("playlists", "add", "PL1", "v1", "v2")) == 0
        assert fake_client.library.calls == [("add", "PL1", ["v1", "v2"])]
        assert "Added 2 track(s) to playlist PL1" in capsys.readouterr().out

    def test_remove(self, fake_client, capsys):
        assert cli.run(parse("playlists", "remove", "PL1", "v1")) == 0
        assert fake_client.library.calls == [("remove", "PL1", "v1")]
        assert "Removed v1 from playlist PL1" in capsys.readouterr().out

    def test_requires_sign_in(self, fake_client, capsys):
        fake_client.library.authenticated = False
        assert cli.run(parse("playlists", "list")) == 1
        assert "music-cli login" in capsys.readouterr().err


class TestHistory:
    def seed(self):
        store = PlayHistoryStore()
        store.record(
            PlayedTrack(
                video_id="h1",
                title="Old Song",
                artists=("Old Artist",),
                duration=100.0,
            )
        )
        store.record(
            PlayedTrack(
                video_id="h2",
                title="New Song",
                artists=("New Artist",),
                duration=200.0,
            )
        )
        store.close()

    def test_human_table(self, capsys):
        self.seed()
        assert cli.run(parse("history")) == 0
        out = capsys.readouterr().out
        assert "New Song" in out
        assert "Old Song" in out

    def test_limit(self, capsys):
        self.seed()
        assert cli.run(parse("history", "--limit", "1")) == 0
        out = capsys.readouterr().out
        assert "New Song" in out
        assert "Old Song" not in out

    def test_json(self, capsys):
        self.seed()
        assert cli.run(parse("history", "--json")) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["video_id"] == "h2"
        assert first["played"] == 1
        assert set(first) == {"video_id", "title", "artists", "duration", "played"}

    def test_empty(self, capsys):
        assert cli.run(parse("history")) == 0
        assert "No history" in capsys.readouterr().out


class TestDownloads:
    def seed(self):
        from music_cli.storage.state import DownloadsStore

        store = DownloadsStore()
        store.record("d1", "Old Download", ("Artist A",), 100.0)
        store.record("d2", "New Download", ("Artist B",), 200.0)
        store.close()

    def test_human_table(self, capsys):
        self.seed()
        assert cli.run(parse("playlists", "downloaded")) == 0
        out = capsys.readouterr().out
        assert "New Download" in out
        assert "Old Download" in out

    def test_json(self, capsys):
        self.seed()
        assert cli.run(parse("playlists", "downloaded", "--json")) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        first = json.loads(lines[0])
        assert first["video_id"] == "d2"
        assert set(first) == {"video_id", "title", "artists", "duration"}

    def test_empty(self, capsys):
        assert cli.run(parse("playlists", "downloaded")) == 0
        assert "No downloads yet" in capsys.readouterr().out
