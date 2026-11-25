"""Discord Rich Presence: payload shape and connection lifecycle.

No Discord client is needed: a fake ``pypresence`` module records every call,
and a fake clock keeps the throttle/retry logic deterministic.
"""

from __future__ import annotations

import enum
import sys
import types
from typing import ClassVar

import pytest

from music_cli import rpc
from music_cli.storage.state import (
    SETTING_DISCORD_RPC,
    SETTING_DISCORD_RPC_APP_ID,
    SettingsStore,
)

APP_ID = "123456789012345678"


class Clock:
    """A settable wall clock for the manager's throttle and retry gates."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeLoop:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePresence:
    """Stands in for ``pypresence.Presence``; class flags inject failures."""

    instances: ClassVar[list[FakePresence]] = []
    fail_connect: Exception | None = None
    fail_update: Exception | None = None
    fail_clear: Exception | None = None

    def __init__(self, client_id: str, **kwargs) -> None:
        self.client_id = client_id
        self.kwargs = kwargs
        self.loop = FakeLoop()
        self.connected = False
        self.closed = False
        self.updates: list[dict] = []
        self.clears = 0
        type(self).instances.append(self)

    def connect(self) -> None:
        if type(self).fail_connect is not None:
            raise type(self).fail_connect
        self.connected = True

    def update(self, **kwargs) -> None:
        if type(self).fail_update is not None:
            raise type(self).fail_update
        self.updates.append(kwargs)

    def clear(self) -> None:
        self.clears += 1
        if type(self).fail_clear is not None:
            raise type(self).fail_clear

    def close(self) -> None:
        self.closed = True


class FakeActivityType(enum.IntEnum):
    PLAYING = 0
    LISTENING = 2


@pytest.fixture
def fake_pypresence(monkeypatch):
    FakePresence.instances = []
    FakePresence.fail_connect = None
    FakePresence.fail_update = None
    FakePresence.fail_clear = None
    module = types.ModuleType("pypresence")
    module.Presence = FakePresence
    module.ActivityType = FakeActivityType
    monkeypatch.setitem(sys.modules, "pypresence", module)
    return FakePresence


@pytest.fixture
def store(tmp_path):
    settings = SettingsStore(tmp_path / "music-cli.db")
    yield settings
    settings.close()


@pytest.fixture
def clock():
    return Clock()


def status(**overrides):
    payload = {
        "state": "playing",
        "track": {
            "video_id": "abc",
            "title": "Some Song",
            "artists": ["Some Artist"],
            "duration": 200.0,
        },
        "position": 30.0,
        "duration": 200.0,
    }
    payload.update(overrides)
    return payload


def enabled(store):
    store.set(SETTING_DISCORD_RPC, True)
    store.set(SETTING_DISCORD_RPC_APP_ID, APP_ID)


class TestActivity:
    def test_playing_payload(self):
        payload = rpc.activity(status(), started=970.0)

        assert payload == {
            "activity_type": rpc.ACTIVITY_LISTENING,
            # Discord renders the activity as "Listening to <name>", so the
            # track title has to be the name, not the application's own.
            "name": "Some Song",
            "details": "Some Artist",
            "large_image": "https://i.ytimg.com/vi/abc/hqdefault.jpg",
            "large_text": "Some Song",
            "buttons": [
                {
                    "label": "Listen on YouTube Music",
                    "url": "https://music.youtube.com/watch?v=abc",
                }
            ],
            "start": 970,
            "end": 1170,
        }
        assert "state" not in payload

    def test_paused_omits_timestamps(self):
        payload = rpc.activity(status(state="paused"), started=970.0)

        assert "start" not in payload
        assert "end" not in payload
        assert payload["state"] == "Paused"
        assert payload["name"] == "Some Song"

    def test_nothing_loaded(self):
        assert rpc.activity({"state": "stopped", "track": None}) is None
        assert rpc.activity({"state": "stopped"}) is None

    def test_falls_back_to_track_duration(self):
        payload = rpc.activity(status(duration=None), started=970.0)

        assert payload["end"] == 1170

    def test_clips_discord_fields_and_defaults_artist(self):
        payload = rpc.activity(
            status(track={"video_id": "abc", "title": "x" * 200, "artists": []}),
            started=970.0,
        )

        assert len(payload["name"]) == 128
        assert len(payload["large_text"]) == 128
        assert payload["details"] == "Unknown artist"

    def test_multiple_artists_are_joined(self):
        payload = rpc.activity(
            status(track={"video_id": "abc", "title": "T", "artists": ["A", "B"]})
        )

        assert payload["details"] == "A, B"


class TestDisabled:
    def test_off_by_default(self, fake_pypresence, store, clock):
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())

        assert presence.state() == {
            "enabled": False,
            "application_id": "",
            "connected": False,
            "error": None,
        }
        assert fake_pypresence.instances == []

    def test_toggle_off_clears_and_closes(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())
        discord = fake_pypresence.instances[0]

        store.set(SETTING_DISCORD_RPC, False)
        state = presence.reload()

        assert state["enabled"] is False
        assert state["connected"] is False
        assert discord.clears == 1
        assert discord.closed is True

    def test_enabled_without_application_id(self, fake_pypresence, store, clock):
        store.set(SETTING_DISCORD_RPC, True)

        presence = rpc.DiscordPresence(store, clock=clock)

        assert presence.state()["error"] == "no application id configured"
        assert fake_pypresence.instances == []


class TestLifecycle:
    def test_connects_and_publishes_current_track(self, fake_pypresence, store, clock):
        enabled(store)

        presence = rpc.DiscordPresence(store, clock=clock)

        assert presence.state()["connected"] is True
        discord = fake_pypresence.instances[0]
        assert discord.client_id == APP_ID
        assert discord.kwargs == {"connection_timeout": 3, "response_timeout": 3}

        presence.tick(status())

        assert len(discord.updates) == 1
        update = discord.updates[0]
        assert update["name"] == "Some Song"
        assert update["activity_type"] is FakeActivityType.LISTENING
        # Clock 1000 minus the 30s already played.
        assert update["start"] == 970
        assert update["end"] == 1170

    def test_unchanged_status_is_not_republished(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)

        presence.tick(status())
        clock.advance(1)
        presence.tick(status())

        assert len(fake_pypresence.instances[0].updates) == 1

    def test_track_change_is_throttled_then_published(
        self, fake_pypresence, store, clock
    ):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())
        discord = fake_pypresence.instances[0]

        clock.advance(1)
        presence.tick(status(track={"video_id": "xyz", "title": "Next Song"}))
        assert len(discord.updates) == 1  # inside the 5s window

        clock.advance(4)
        presence.tick(status(track={"video_id": "xyz", "title": "Next Song"}))

        assert len(discord.updates) == 2
        assert discord.updates[1]["name"] == "Next Song"

    def test_seek_reanchors_the_elapsed_timer(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status(position=30.0))
        discord = fake_pypresence.instances[0]

        clock.advance(5)
        presence.tick(status(position=90.0))

        assert len(discord.updates) == 2
        # New anchor: 1005 - 90.
        assert discord.updates[1]["start"] == 915
        assert discord.updates[1]["end"] == 1115

    def test_pause_and_resume(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status(position=30.0))
        discord = fake_pypresence.instances[0]

        clock.advance(5)
        presence.tick(status(state="paused", position=35.0))
        assert "start" not in discord.updates[1]
        assert discord.updates[1]["state"] == "Paused"

        clock.advance(5)
        presence.tick(status(state="playing", position=35.0))
        assert discord.updates[2]["start"] == 975  # 1010 - 35

    def test_pause_is_not_throttled(self, fake_pypresence, store, clock):
        """A pause must drop the timestamps at once, not after 5s.

        Discord counts elapsed time from ``start``, so a throttled pause
        leaves the counter running on a track that is already silent.
        """
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status(position=30.0))
        discord = fake_pypresence.instances[0]

        clock.advance(1)  # inside the throttle window
        presence.tick(status(state="paused", position=31.0))

        assert len(discord.updates) == 2
        assert "start" not in discord.updates[1]

    def test_resume_is_not_throttled(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status(position=30.0))
        discord = fake_pypresence.instances[0]

        clock.advance(1)
        presence.tick(status(state="paused", position=31.0))
        clock.advance(1)
        presence.tick(status(state="playing", position=31.0))

        assert len(discord.updates) == 3
        assert discord.updates[2]["start"] == 971  # 1002 - 31

    def test_stop_clears_presence(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())
        discord = fake_pypresence.instances[0]

        clock.advance(5)
        presence.tick({"state": "stopped", "track": None})

        assert discord.clears == 1
        assert presence.state()["connected"] is True

    def test_close_clears_and_closes(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())
        discord = fake_pypresence.instances[0]

        presence.close()

        assert discord.clears == 1
        assert discord.closed is True
        assert presence.state()["enabled"] is False

    def test_changing_the_application_id_reconnects(
        self, fake_pypresence, store, clock
    ):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        first = fake_pypresence.instances[0]

        store.set(SETTING_DISCORD_RPC_APP_ID, "987654321098765432")
        presence.reload()

        assert first.closed is True
        assert len(fake_pypresence.instances) == 2
        assert fake_pypresence.instances[1].client_id == "987654321098765432"


class TestFailures:
    def test_connect_failure_is_contained_and_retried(
        self, fake_pypresence, store, clock
    ):
        enabled(store)
        fake_pypresence.fail_connect = RuntimeError("Discord is not running")

        presence = rpc.DiscordPresence(store, clock=clock)

        assert presence.state()["connected"] is False
        assert "Discord is not running" in presence.state()["error"]
        assert fake_pypresence.instances[0].loop.closed is True

        clock.advance(29)
        presence.tick(status())
        assert len(fake_pypresence.instances) == 1  # retry gate

        clock.advance(1)
        fake_pypresence.fail_connect = None
        presence.tick(status())

        assert len(fake_pypresence.instances) == 2
        assert presence.state()["connected"] is True

    def test_update_failure_disconnects_and_retries(
        self, fake_pypresence, store, clock
    ):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        fake_pypresence.fail_update = RuntimeError("pipe closed")

        presence.tick(status())

        discord = fake_pypresence.instances[0]
        assert discord.closed is True
        assert presence.state()["connected"] is False
        assert "pipe closed" in presence.state()["error"]

        clock.advance(30)
        fake_pypresence.fail_update = None
        presence.tick(status())

        assert len(fake_pypresence.instances) == 2
        assert presence.state()["connected"] is True

    def test_clear_failure_disconnects(self, fake_pypresence, store, clock):
        enabled(store)
        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())
        fake_pypresence.fail_clear = RuntimeError("pipe closed")

        clock.advance(5)
        presence.tick({"state": "stopped", "track": None})

        assert presence.state()["connected"] is False
        assert "pipe closed" in presence.state()["error"]

    def test_invalid_application_id_points_at_the_fix(
        self, fake_pypresence, store, clock
    ):
        enabled(store)
        fake_pypresence.fail_connect = RuntimeError(
            "Error Code: 4000 Message: Client ID is Invalid"
        )

        presence = rpc.DiscordPresence(store, clock=clock)

        error = presence.state()["error"]
        assert "Discord rejected the application id" in error
        assert "rpc setup" in error

    def test_missing_pypresence_reports_an_error(self, monkeypatch, store, clock):
        enabled(store)
        monkeypatch.setitem(sys.modules, "pypresence", None)

        presence = rpc.DiscordPresence(store, clock=clock)
        presence.tick(status())  # must not raise

        assert presence.state()["error"] == "pypresence is not installed"
        assert presence.state()["connected"] is False

    def test_reload_retries_a_failed_connection_immediately(
        self, fake_pypresence, store, clock
    ):
        enabled(store)
        fake_pypresence.fail_connect = RuntimeError("Discord is not running")
        presence = rpc.DiscordPresence(store, clock=clock)

        fake_pypresence.fail_connect = None
        state = presence.reload()

        assert state["connected"] is True
        assert len(fake_pypresence.instances) == 2
