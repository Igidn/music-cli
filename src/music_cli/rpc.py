"""Discord Rich Presence, driven by the playback daemon's status.

The daemon owns playback, so it also owns the presence: the CLI only flips
persisted settings and asks a running daemon to reload them. Presence is off
by default and needs a user-created Discord application id (``rpc setup``),
so every install does not show up under the same generic app name.

pypresence talks to the local Discord client over its IPC socket. That socket
may be missing (Discord not running), close mid-session (Discord restarted),
or reject the application id; none of those may affect playback, so every
call is contained here and retried from the daemon's serve loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .storage.state import (
    SETTING_DISCORD_RPC,
    SETTING_DISCORD_RPC_APP_ID,
    SettingsStore,
)

logger = logging.getLogger(__name__)

#: Discord accepts at most 5 presence updates per 20 seconds; one every 5
#: seconds stays safely below that while keeping pause/seek feedback snappy.
MIN_UPDATE_INTERVAL = 5.0
#: How long to wait before retrying a failed or closed Discord connection.
RETRY_INTERVAL = 30.0
#: A position drift larger than this means the user seeked, so Discord's
#: elapsed timer must be re-anchored.
SEEK_TOLERANCE = 2.0
#: Discord's SET_ACTIVITY activity type for "Listening to …". pypresence
#: expects its ``ActivityType`` enum (it reads ``.value``), so the manager
#: passes the real enum once it connects; the plain value keeps ``activity``
#: free of pypresence imports and easy to test.
ACTIVITY_LISTENING = 2
#: Discord rejects local RPC calls slowly; the pipe is local, so keep the
#: worst-case stall on the daemon's serve loop short.
_RPC_TIMEOUT = 3
_TRACK_URL = "https://music.youtube.com/watch?v={video_id}"
_THUMBNAIL_URL = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _clip(text: str) -> str:
    """Trim Discord's state/details/large_text fields to their 128-char cap."""
    return text.strip()[:128] or "Unknown"


def activity(
    status: dict[str, Any],
    *,
    started: float | None = None,
    activity_type: Any = ACTIVITY_LISTENING,
) -> dict[str, Any] | None:
    """Build the ``Presence.update`` arguments for a daemon status snapshot.

    Returns ``None`` when nothing is loaded (the presence should be cleared).
    ``started`` is the wall-clock time at position zero, so Discord's elapsed
    timer stays correct across pauses and seeks; it is omitted while paused,
    because Discord would otherwise keep counting a stopped track.
    """
    track = status.get("track")
    if not track or not track.get("video_id"):
        return None
    video_id = str(track["video_id"])
    title = _clip(str(track.get("title") or "Unknown track"))
    artists = ", ".join(str(artist) for artist in track.get("artists") or ())
    artists = _clip(artists or "Unknown artist")
    paused = status.get("state") == "paused"
    payload: dict[str, Any] = {
        "activity_type": activity_type,
        "details": title,
        "state": _clip(f"Paused · {artists}" if paused else artists),
        "large_image": _THUMBNAIL_URL.format(video_id=video_id),
        "large_text": title,
        "buttons": [
            {
                "label": "Listen on YouTube Music",
                "url": _TRACK_URL.format(video_id=video_id),
            }
        ],
    }
    if not paused and started is not None:
        payload["start"] = int(started)
        duration = status.get("duration") or track.get("duration")
        if duration:
            payload["end"] = int(started + float(duration))
    return payload


def _signature(status: dict[str, Any]) -> tuple[Any, ...]:
    """The fields that make a new presence update necessary.

    Position is deliberately absent: it advances every second and would blow
    through Discord's rate limit. Seeks are detected separately by comparing
    the player's position with the elapsed time we told Discord about.
    """
    track = status.get("track") or {}
    return (
        track.get("video_id"),
        track.get("title"),
        tuple(track.get("artists") or ()),
        status.get("state"),
        track.get("duration"),
        status.get("duration"),
    )


def _error_text(error: Exception) -> str:
    return str(error).strip() or type(error).__name__


def _connect_error(error: Exception) -> str:
    """A human-readable reason a Discord connection failed.

    Discord answers an unknown application id with a generic ``4000`` error;
    naming the fix is more useful than echoing the raw code.
    """
    text = _error_text(error)
    if "client id" in text.lower():
        return (
            f"Discord rejected the application id ({text}) — set a valid one "
            "with 'music-cli rpc setup <application id>'"
        )
    return text


class DiscordPresence:
    """Owns one pypresence connection and mirrors playback into Discord.

    Every method runs on the daemon's serve loop. :meth:`tick` is cheap when
    nothing relevant changed, so calling it on every loop iteration is fine;
    actual RPC writes are throttled and skipped while the feature is disabled.
    """

    def __init__(
        self,
        settings: SettingsStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._presence: Any = None
        self._enabled = False
        self._app_id = ""
        self._connected = False
        self._error: str | None = None
        self._retry_at = 0.0
        self._last_update = 0.0
        self._signature: tuple[Any, ...] | None = None
        self._pending: dict[str, Any] | None = None
        self._cleared = True
        self._started: float | None = None
        self._activity_type: Any = ACTIVITY_LISTENING
        self.reload()

    def reload(self) -> dict[str, Any]:
        """Re-read the persisted settings and connect/disconnect accordingly.

        Called by the daemon on startup and when the CLI asks it to apply a
        fresh ``rpc setup`` / ``rpc toggle``. Returns the current state so the
        CLI can report whether Discord was reachable.
        """
        enabled = self._settings.get_bool(SETTING_DISCORD_RPC)
        app_id = (self._settings.get(SETTING_DISCORD_RPC_APP_ID) or "").strip()
        changed = (enabled, app_id) != (self._enabled, self._app_id)
        self._enabled, self._app_id = enabled, app_id
        if not enabled or not app_id:
            self._disconnect(clear=True)
            self._error = None if not enabled else "no application id configured"
            return self.state()
        if changed or not self._connected:
            self._disconnect(clear=True)
            self._connect()
        return self.state()

    def tick(self, status: dict[str, Any]) -> None:
        """Mirror ``status`` into Discord, throttled and rate-limit safe."""
        if not self._enabled or not self._app_id:
            return
        now = self._clock()
        if not self._connected:
            if now < self._retry_at:
                return
            if not self._connect():
                return
        if self._needs_update(status, now):
            self._pending = status
        if self._pending is None:
            return
        if now - self._last_update < MIN_UPDATE_INTERVAL:
            return
        self._flush(now)

    def state(self) -> dict[str, Any]:
        """A JSON-serializable snapshot for the ``rpc_reload`` response."""
        return {
            "enabled": self._enabled,
            "application_id": self._app_id,
            "connected": self._connected,
            "error": self._error,
        }

    def close(self) -> None:
        """Clear the presence and close the Discord pipe."""
        self._enabled = False
        self._disconnect(clear=True)

    def _needs_update(self, status: dict[str, Any], now: float) -> bool:
        track = status.get("track")
        if track is None:
            return not self._cleared
        if _signature(status) != self._signature:
            return True
        if status.get("state") != "playing" or self._started is None:
            return False
        expected = now - self._started
        actual = float(status.get("position") or 0.0)
        return abs(expected - actual) > SEEK_TOLERANCE

    def _flush(self, now: float) -> None:
        status = self._pending or {}
        self._pending = None
        if status.get("track") is None:
            self._clear()
            return
        if status.get("state") == "playing":
            position = float(status.get("position") or 0.0)
            self._started = now - position
        else:
            self._started = None
        payload = activity(
            status, started=self._started, activity_type=self._activity_type
        )
        if payload is None:
            self._clear()
            return
        try:
            self._presence.update(**payload)
        except Exception as error:  # noqa: BLE001 — Discord must never break playback
            self._error = _error_text(error)
            logger.info("Discord RPC update failed: %s", self._error)
            self._disconnect(clear=False)
            self._retry_at = now + RETRY_INTERVAL
            return
        self._signature = _signature(status)
        self._last_update = now
        self._cleared = False
        self._error = None

    def _clear(self) -> None:
        try:
            self._presence.clear()
        except Exception as error:  # noqa: BLE001 — Discord must never break playback
            self._error = _error_text(error)
            logger.info("Discord RPC clear failed: %s", self._error)
            self._disconnect(clear=False)
            self._retry_at = self._clock() + RETRY_INTERVAL
            return
        self._cleared = True
        self._signature = None
        self._last_update = self._clock()
        self._error = None

    def _connect(self) -> bool:
        try:
            from pypresence import ActivityType, Presence
        except ImportError:
            self._error = "pypresence is not installed"
            self._retry_at = self._clock() + RETRY_INTERVAL
            return False
        presence = None
        try:
            presence = Presence(
                self._app_id,
                connection_timeout=_RPC_TIMEOUT,
                response_timeout=_RPC_TIMEOUT,
            )
            presence.connect()
        except Exception as error:  # noqa: BLE001 — Discord may not be running
            self._error = _connect_error(error)
            self._retry_at = self._clock() + RETRY_INTERVAL
            logger.info("Discord RPC unavailable: %s", self._error)
            # A failed handshake leaves pypresence's fresh event loop open.
            if presence is not None:
                try:
                    presence.loop.close()
                except Exception:  # noqa: BLE001, S110 — best effort cleanup
                    pass
            return False
        self._presence = presence
        self._connected = True
        self._activity_type = ActivityType.LISTENING
        self._error = None
        self._retry_at = 0.0
        self._last_update = 0.0
        self._signature = None
        self._cleared = True
        logger.info("connected to Discord RPC as %s", self._app_id)
        return True

    def _disconnect(self, *, clear: bool) -> None:
        presence, self._presence = self._presence, None
        was_showing = not self._cleared
        self._connected = False
        self._signature = None
        self._pending = None
        self._cleared = True
        self._started = None
        if presence is None:
            return
        if clear and was_showing:
            try:
                presence.clear()
            except Exception:  # noqa: BLE001, S110 — best effort on shutdown
                pass
        try:
            presence.close()
        except Exception:  # the pipe may already be gone
            logger.debug("could not close the Discord RPC connection", exc_info=True)
