"""Tests for the macOS Now Playing bridge.

These use injected fakes, so they never touch the system's media services or
require Control Center. The MediaPlayer framework itself is macOS-only.
"""

from __future__ import annotations

import pytest

pytest.importorskip("MediaPlayer", reason="MediaPlayer is macOS-only")

from MediaPlayer import (
    MPMediaItemPropertyArtist,
    MPMediaItemPropertyPlaybackDuration,
    MPMediaItemPropertyTitle,
    MPNowPlayingInfoPropertyElapsedPlaybackTime,
    MPNowPlayingInfoPropertyExternalContentIdentifier,
    MPNowPlayingInfoPropertyPlaybackRate,
    MPNowPlayingPlaybackStatePaused,
    MPNowPlayingPlaybackStatePlaying,
    MPNowPlayingPlaybackStateStopped,
    MPNowPlayingPlaybackStateUnknown,
    MPRemoteCommandHandlerStatusSuccess,
)

from music_cli.player.base import MediaCommand
from music_cli.player.media_control import POSITION_REFRESH, MediaControl


class FakeCommand:
    """Stand-in for ``MPRemoteCommand``; records registration and can fire."""

    def __init__(self) -> None:
        self.enabled = False
        self.handler = None
        self.add_calls = 0
        self.removed = False
        self.removed_target = None
        self.registered_target = None

    def setEnabled_(self, value) -> None:
        self.enabled = bool(value)

    def addTargetWithHandler_(self, handler):
        self.handler = handler
        self.add_calls += 1
        target = object()
        self.registered_target = target
        return target

    def removeTarget_(self, target) -> None:
        self.removed = True
        self.removed_target = target
        self.handler = None

    def fire(self, event=None):
        return self.handler(event)


class FakeCommandCenter:
    """The subset of ``MPRemoteCommandCenter`` that MediaControl uses."""

    def __init__(self) -> None:
        self.commands = {
            "play": FakeCommand(),
            "pause": FakeCommand(),
            "toggle": FakeCommand(),
            "next": FakeCommand(),
            "previous": FakeCommand(),
            "stop": FakeCommand(),
            "seek": FakeCommand(),
            "unsupported": FakeCommand(),
        }

    def playCommand(self):
        return self.commands["play"]

    def pauseCommand(self):
        return self.commands["pause"]

    def togglePlayPauseCommand(self):
        return self.commands["toggle"]

    def nextTrackCommand(self):
        return self.commands["next"]

    def previousTrackCommand(self):
        return self.commands["previous"]

    def stopCommand(self):
        return self.commands["stop"]

    def changePlaybackPositionCommand(self):
        return self.commands["seek"]

    def skipForwardCommand(self):
        return self.commands["unsupported"]


class FakeInfoCenter:
    def __init__(self) -> None:
        self.info = None
        self.state = None
        self.publish_count = 0

    def setNowPlayingInfo_(self, info) -> None:
        self.info = info
        self.publish_count += 1

    def setPlaybackState_(self, state) -> None:
        self.state = state


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def controls():
    commands = FakeCommandCenter()
    info = FakeInfoCenter()
    clock = FakeClock()
    media = MediaControl(command_center=commands, info_center=info, clock=clock)
    media.start()
    try:
        yield media, commands, info, clock
    finally:
        media.close()


def playing_status(**overrides):
    status = {
        "state": "playing",
        "track": {
            "video_id": "abc",
            "title": "Some Song",
            "artists": ["Artist One", "Artist Two"],
        },
        "position": 10.0,
        "duration": 200.0,
    }
    status.update(overrides)
    return status


def test_start_registers_transport_commands(controls):
    _, commands, _, _ = controls
    for name in ("play", "pause", "toggle", "next", "previous", "stop", "seek"):
        assert commands.commands[name].enabled
        assert commands.commands[name].add_calls == 1
    assert not commands.commands["unsupported"].enabled


def test_start_is_idempotent(controls):
    media, commands, _, _ = controls
    media.start()
    assert commands.commands["play"].add_calls == 1


@pytest.mark.parametrize(
    "name", ["play", "pause", "toggle", "next", "previous", "stop"]
)
def test_handler_queues_command_until_drain(controls, name):
    media, commands, _, _ = controls
    assert commands.commands[name].fire(None) == MPRemoteCommandHandlerStatusSuccess
    # The handler must not run playback itself; the daemon drains on its loop.
    assert media.drain() == [MediaCommand(name)]
    assert media.drain() == []


def test_seek_event_carries_absolute_position(controls):
    media, commands, _, _ = controls

    class Event:
        @staticmethod
        def positionTime():
            return 42.5

    assert (
        commands.commands["seek"].fire(Event()) == MPRemoteCommandHandlerStatusSuccess
    )
    assert media.drain() == [MediaCommand("seek", position=42.5)]


def test_publishes_track_metadata_while_playing(controls):
    media, _, info, _ = controls
    media.update(playing_status())
    assert info.state == MPNowPlayingPlaybackStatePlaying
    assert info.info[MPMediaItemPropertyTitle] == "Some Song"
    assert info.info[MPMediaItemPropertyArtist] == "Artist One, Artist Two"
    assert info.info[MPMediaItemPropertyPlaybackDuration] == 200.0
    assert info.info[MPNowPlayingInfoPropertyElapsedPlaybackTime] == 10.0
    assert info.info[MPNowPlayingInfoPropertyPlaybackRate] == 1.0
    assert info.info[MPNowPlayingInfoPropertyExternalContentIdentifier] == "abc"


def test_pause_publishes_paused_state_and_zero_rate(controls):
    media, _, info, clock = controls
    media.update(playing_status())
    clock.advance(2.0)
    media.update(playing_status(state="paused", position=12.0))
    assert info.state == MPNowPlayingPlaybackStatePaused
    assert info.info[MPNowPlayingInfoPropertyPlaybackRate] == 0.0
    assert info.info[MPNowPlayingInfoPropertyElapsedPlaybackTime] == 12.0


def test_stopped_clears_now_playing_info(controls):
    media, _, info, _ = controls
    media.update(playing_status())
    media.update({"state": "stopped", "track": None, "position": 0.0})
    assert info.info is None
    assert info.state == MPNowPlayingPlaybackStateStopped
    # A second idle status must not republish the clear.
    publishes = info.publish_count
    media.update({"state": "stopped", "track": None, "position": 0.0})
    assert info.publish_count == publishes


def test_position_updates_are_throttled_while_playing(controls):
    media, _, info, clock = controls
    media.update(playing_status())
    publishes = info.publish_count

    clock.advance(1.0)
    media.update(playing_status(position=11.0))
    assert info.publish_count == publishes  # macOS extrapolates on its own

    clock.advance(POSITION_REFRESH)
    media.update(playing_status(position=16.0))
    assert info.publish_count == publishes + 1
    assert info.info[MPNowPlayingInfoPropertyElapsedPlaybackTime] == 16.0


def test_playing_seek_publishes_immediately(controls):
    media, _, info, clock = controls
    media.update(playing_status())
    publishes = info.publish_count
    clock.advance(1.0)
    media.update(playing_status(position=120.0))
    assert info.publish_count == publishes + 1
    assert info.info[MPNowPlayingInfoPropertyElapsedPlaybackTime] == 120.0


def test_paused_seek_publishes_immediately(controls):
    media, _, info, _ = controls
    media.update(playing_status(state="paused", position=10.0))
    publishes = info.publish_count
    media.update(playing_status(state="paused", position=60.0))
    assert info.publish_count == publishes + 1
    assert info.info[MPNowPlayingInfoPropertyElapsedPlaybackTime] == 60.0


def test_missing_duration_and_artists_are_omitted(controls):
    media, _, info, _ = controls
    media.update(
        playing_status(
            track={"video_id": "abc", "title": "Untitled", "artists": []},
            duration=None,
        )
    )
    assert MPMediaItemPropertyArtist not in info.info
    assert MPMediaItemPropertyPlaybackDuration not in info.info


def test_close_unregisters_and_withdraws(controls):
    media, commands, info, _ = controls
    media.update(playing_status())
    media.close()
    for name in ("play", "pause", "toggle", "next", "previous", "stop", "seek"):
        assert not commands.commands[name].enabled
        assert commands.commands[name].removed
        # removeTarget: needs the exact token returned when registering.
        assert (
            commands.commands[name].removed_target
            is commands.commands[name].registered_target
        )
    assert info.info is None
    assert info.state == MPNowPlayingPlaybackStateUnknown


def test_close_is_idempotent(controls):
    media, _, info, _ = controls
    media.close()
    media.close()
    assert info.info is None
