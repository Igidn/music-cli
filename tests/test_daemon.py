"""Dispatch tests for the daemon: no sockets, no AVFoundation."""

from __future__ import annotations

from collections import deque

import pytest

from music_cli.core.errors import PlayerError
from music_cli.daemon import _download_hook, _is_async_play, handle_request


class FakePlayer:
    def __init__(self):
        self.volume = 80


class FakeClient:
    def __init__(self):
        self.current = None
        self.player = FakePlayer()


class FakeSession:
    """Records every call in order; canned status/queue in the frozen shape."""

    def __init__(self):
        self.calls = []
        self.client = FakeClient()
        self.fail_with = None
        self.resume_last_result = None
        self.muted = False
        self.loop = False
        self.auto_next = True
        self._status = {
            "state": "playing",
            "track": {"video_id": "abc", "title": "Some Song"},
            "position": 12.0,
            "duration": 200.0,
            "volume": 80,
            "muted": False,
            "loop": False,
            "auto_next": True,
            "queue": 3,
        }
        self._queue = [
            {"video_id": "t1", "title": "Track One"},
            {"video_id": "t2", "title": "Track Two"},
        ]

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail_with is not None:
            raise self.fail_with

    def names(self):
        return [name for name, _, _ in self.calls]

    def play_query(self, query):
        self._record("play_query", query)

    def play_video(self, video_id, title=""):
        self._record("play_video", video_id, title)

    def play_playlist(self, playlist_id, start_index=0):
        self._record("play_playlist", playlist_id, start_index)

    def play_album(self, album_id, start_index=0):
        self._record("play_album", album_id, start_index)

    def play_queue_track(self, index):
        self._record("play_queue_track", index)

    def stop(self):
        self._record("stop")

    def resume_last(self):
        self._record("resume_last")
        return self.resume_last_result

    def on_track_end(self):
        self._record("on_track_end")

    def next_track(self):
        self._record("next_track")
        return None

    def pause(self):
        self._record("pause")

    def resume(self):
        self._record("resume")

    def toggle(self):
        self._record("toggle")

    def seek(self, *, offset=None, position=None):
        self._record("seek", offset=offset, position=position)

    def set_volume(self, *, volume=None, delta=None):
        self._record("set_volume", volume=volume, delta=delta)
        if volume is not None:
            self.client.player.volume = volume
        return self.client.player.volume

    def _apply(self, current, state):
        return {"on": True, "off": False}.get(state, not current)

    def set_muted(self, state):
        self._record("set_muted", state)
        self.muted = self._apply(self.muted, state)
        return self.muted

    def set_loop(self, state):
        self._record("set_loop", state)
        self.loop = self._apply(self.loop, state)
        return self.loop

    def set_auto_next(self, state):
        self._record("set_auto_next", state)
        self.auto_next = self._apply(self.auto_next, state)
        return self.auto_next

    def status(self):
        return self._status

    def queue(self):
        return self._queue

    def close(self):
        self._record("close")

    def remove_download(self, video_id):
        self._record("remove_download", video_id)


@pytest.fixture
def session():
    return FakeSession()


def test_play_by_query(session):
    response = handle_request(session, {"cmd": "play", "query": "some song"})
    assert response == {"ok": True, "data": session._status}
    assert ("play_query", ("some song",), {}) in session.calls


def test_play_by_video_id(session):
    response = handle_request(session, {"cmd": "play", "video_id": "abc"})
    assert response["ok"] is True
    assert session.calls == [("play_video", ("abc", ""), {})]


def test_play_by_playlist_id(session):
    response = handle_request(session, {"cmd": "play", "playlist_id": "PL123"})
    assert response["ok"] is True
    assert session.calls == [("play_playlist", ("PL123", 0), {})]


def test_play_by_album_id(session):
    response = handle_request(
        session, {"cmd": "play", "album_id": "MPREb_123", "album_index": 2}
    )
    assert response["ok"] is True
    assert session.calls == [("play_album", ("MPREb_123", 2), {})]


def test_play_flags_applied_before_play(session):
    response = handle_request(
        session,
        {"cmd": "play", "query": "x", "volume": 50, "loop": True, "auto_next": False},
    )
    assert response["ok"] is True
    assert session.names() == ["set_volume", "set_loop", "set_auto_next", "play_query"]
    assert session.calls[0] == ("set_volume", (), {"volume": 50, "delta": None})
    assert session.calls[1] == ("set_loop", ("on",), {})
    assert session.calls[2] == ("set_auto_next", ("off",), {})


def test_play_without_target_fails(session):
    response = handle_request(session, {"cmd": "play"})
    assert response["ok"] is False
    assert session.calls == []


def test_play_with_multiple_targets_fails(session):
    response = handle_request(session, {"cmd": "play", "query": "a", "video_id": "b"})
    assert response["ok"] is False
    assert session.calls == []


def test_play_by_queue_index(session):
    response = handle_request(session, {"cmd": "play", "queue_index": 2})
    assert response["ok"] is True
    assert session.calls == [("play_queue_track", (2,), {})]


def test_play_by_queue_index_zero(session):
    response = handle_request(session, {"cmd": "play", "queue_index": 0})
    assert response["ok"] is True
    assert session.calls == [("play_queue_track", (0,), {})]


def test_play_queue_index_with_another_target_fails(session):
    response = handle_request(
        session, {"cmd": "play", "queue_index": 0, "playlist_id": "PL1"}
    )
    assert response["ok"] is False
    assert session.calls == []


def test_play_playlist_with_start_index(session):
    response = handle_request(
        session, {"cmd": "play", "playlist_id": "PL1", "playlist_index": 3}
    )
    assert response["ok"] is True
    assert session.calls == [("play_playlist", ("PL1", 3), {})]


def test_play_video_with_title(session):
    response = handle_request(
        session, {"cmd": "play", "video_id": "abc", "title": "Song"}
    )
    assert response["ok"] is True
    assert session.calls == [("play_video", ("abc", "Song"), {})]


@pytest.mark.parametrize("cmd", ["pause", "toggle", "next"])
def test_simple_transport_dispatch(session, cmd):
    response = handle_request(session, {"cmd": cmd})
    assert response == {"ok": True, "data": session._status}
    method = "next_track" if cmd == "next" else cmd
    assert session.calls == [(method, (), {})]


def test_resume_with_current_track(session):
    session.client.current = object()
    response = handle_request(session, {"cmd": "resume"})
    assert response == {"ok": True, "data": session._status}
    assert session.calls == [("resume", (), {})]


def test_resume_idle_with_history(session):
    session.resume_last_result = object()
    response = handle_request(session, {"cmd": "resume"})
    assert response["ok"] is True
    assert session.calls == [("resume_last", (), {})]


def test_resume_idle_with_empty_history(session):
    response = handle_request(session, {"cmd": "resume"})
    assert response["ok"] is False
    assert "nothing to resume" in response["error"]


def test_seek_offset(session):
    response = handle_request(session, {"cmd": "seek", "offset": -10.0})
    assert response["ok"] is True
    assert session.calls == [("seek", (), {"offset": -10.0, "position": None})]


def test_seek_position(session):
    response = handle_request(session, {"cmd": "seek", "position": 42.5})
    assert response["ok"] is True
    assert session.calls == [("seek", (), {"offset": None, "position": 42.5})]


def test_seek_without_args_fails(session):
    response = handle_request(session, {"cmd": "seek"})
    assert response["ok"] is False
    assert session.calls == []


def test_volume_level(session):
    response = handle_request(session, {"cmd": "volume", "level": 50})
    assert response == {"ok": True, "data": {"volume": 50}}
    assert session.calls == [("set_volume", (), {"volume": 50, "delta": None})]


def test_volume_delta(session):
    response = handle_request(session, {"cmd": "volume", "delta": -5})
    assert response == {"ok": True, "data": {"volume": 80}}
    assert session.calls == [("set_volume", (), {"volume": None, "delta": -5})]


def test_volume_without_args_fails(session):
    response = handle_request(session, {"cmd": "volume"})
    assert response["ok"] is False
    assert session.calls == []


@pytest.mark.parametrize(
    "cmd,state,expected",
    [
        ("mute", "on", True),
        ("mute", "off", False),
        ("mute", "toggle", True),
        ("loop", "on", True),
        ("loop", "toggle", True),
        ("auto_next", "off", False),
        ("auto_next", "toggle", False),
    ],
)
def test_state_toggles(session, cmd, state, expected):
    response = handle_request(session, {"cmd": cmd, "state": state})
    assert response == {"ok": True, "data": {cmd: expected}}
    method = "set_muted" if cmd == "mute" else f"set_{cmd}"
    assert session.calls == [(method, (state,), {})]


@pytest.mark.parametrize("cmd", ["mute", "loop", "auto_next"])
def test_state_bad_value_fails(session, cmd):
    response = handle_request(session, {"cmd": cmd, "state": "maybe"})
    assert response["ok"] is False
    assert session.calls == []


def test_status_passthrough(session):
    response = handle_request(session, {"cmd": "status"})
    assert response["ok"] is True
    assert response["data"] is session._status


def test_queue_passthrough(session):
    response = handle_request(session, {"cmd": "queue"})
    assert response["ok"] is True
    assert response["data"] is session._queue


def test_stop_keeps_daemon_alive_and_returns_status(session):
    response = handle_request(session, {"cmd": "stop"})
    assert response == {"ok": True, "data": session._status}
    assert session.names() == ["stop"]


def test_quit_returns_null_payload(session):
    assert handle_request(session, {"cmd": "quit"}) == {"ok": True, "data": None}
    assert session.calls == []


def test_unknown_command(session):
    response = handle_request(session, {"cmd": "explode"})
    assert response["ok"] is False
    assert "explode" in response["error"]


def test_remove_download_dispatch(session):
    response = handle_request(session, {"cmd": "remove_download", "video_id": "abc"})
    assert response == {"ok": True, "data": None}
    assert ("remove_download", ("abc",), {}) in session.calls


def test_player_error_becomes_error_response(session):
    session.fail_with = PlayerError("no streams found")
    response = handle_request(session, {"cmd": "play", "query": "x"})
    assert response == {"ok": False, "error": "no streams found"}


def test_unexpected_error_never_escapes(session):
    session.fail_with = RuntimeError("a bug")
    response = handle_request(session, {"cmd": "pause"})
    assert response["ok"] is False
    assert "a bug" in response["error"]


class _FakeSub:
    """Writable fake subscriber: collects pushed lines, no blocking."""

    def __init__(self):
        self.lines = []

    def send(self, chunk: bytes) -> int:
        self.lines.append(chunk)
        return len(chunk)


def test_download_hook_pushes_percent():
    sub = _FakeSub()
    subscribers = {sub: deque()}
    hook = _download_hook(subscribers)
    hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000})
    assert sub.lines == [b'{"event": "download", "percent": 50, "downloaded": 500}\n']


def test_is_async_play_routes_single_track_targets_only():
    assert _is_async_play({"cmd": "play", "video_id": "v"}) is True
    assert _is_async_play({"cmd": "play", "query": "q"}) is True
    assert _is_async_play({"cmd": "play", "playlist_id": "PL"}) is False
    assert _is_async_play({"cmd": "play", "album_id": "MPREb_x"}) is False
    assert _is_async_play({"cmd": "play", "queue_index": 0}) is False
    assert _is_async_play({"cmd": "play", "video_id": "v", "playlist_id": "PL"}) is False


def test_download_hook_skips_unknown_total_and_other_statuses():
    sub = _FakeSub()
    subscribers = {sub: deque()}
    hook = _download_hook(subscribers)
    hook({"status": "downloading", "downloaded_bytes": 500})
    assert sub.lines == [b'{"event": "download", "percent": null, "downloaded": 500}\n']
    # finished is pushed so the TUI can clear the download status line.
    hook({"status": "finished"})
    assert len(sub.lines) == 2
    assert b'"finished": true' in sub.lines[1]
