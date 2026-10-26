"""macOS Now Playing integration (Control Center, media keys, AirPods).

The public MediaPlayer framework publishes the current track through
``MPNowPlayingInfoCenter`` and routes system transport commands through
``MPRemoteCommandCenter``. Only the playback daemon creates a controller: it
is the process that owns audio, and its serve loop already pumps the main run
loop that MediaPlayer uses to deliver callbacks.

Command handlers may arrive on any thread and must return immediately.
They therefore only enqueue a :class:`~.base.MediaCommand`; the daemon drains
the queue on its serve loop, where touching AVFoundation is safe.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from MediaPlayer import (
    MPMediaItemPropertyArtist,
    MPMediaItemPropertyPlaybackDuration,
    MPMediaItemPropertyTitle,
    MPNowPlayingInfoCenter,
    MPNowPlayingInfoMediaTypeAudio,
    MPNowPlayingInfoPropertyElapsedPlaybackTime,
    MPNowPlayingInfoPropertyExternalContentIdentifier,
    MPNowPlayingInfoPropertyMediaType,
    MPNowPlayingInfoPropertyPlaybackRate,
    MPNowPlayingPlaybackStatePaused,
    MPNowPlayingPlaybackStatePlaying,
    MPNowPlayingPlaybackStateStopped,
    MPNowPlayingPlaybackStateUnknown,
    MPRemoteCommandCenter,
    MPRemoteCommandHandlerStatusSuccess,
)

from .base import MediaCommand

logger = logging.getLogger(__name__)

#: How often the elapsed position is republished while playing. macOS
#: extrapolates from (elapsed, rate), so this only corrects drift.
POSITION_REFRESH = 5.0
#: A position further than this from where playback should be is a seek.
SEEK_TOLERANCE = 1.5
#: Paused position changes smaller than this are rounding noise, not a seek.
PAUSED_SEEK_TOLERANCE = 0.5

#: Commands macOS exposes but music-cli cannot honour; leaving them enabled
#: would show dead controls in Control Center.
_UNSUPPORTED_COMMANDS = (
    "changePlaybackRateCommand",
    "changeRepeatModeCommand",
    "changeShuffleModeCommand",
    "skipForwardCommand",
    "skipBackwardCommand",
    "ratingCommand",
    "likeCommand",
    "dislikeCommand",
    "bookmarkCommand",
    "enableLanguageOptionCommand",
    "disableLanguageOptionCommand",
)


class MediaControl:
    """Publishes Now Playing metadata and queues system transport commands.

    ``command_center``, ``info_center`` and ``clock`` are injectable so tests
    can exercise the bridge without touching the system's media services.
    """

    def __init__(
        self,
        *,
        command_center: Any | None = None,
        info_center: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._command_center = (
            command_center
            if command_center is not None
            else MPRemoteCommandCenter.sharedCommandCenter()
        )
        self._info_center = (
            info_center
            if info_center is not None
            else MPNowPlayingInfoCenter.defaultCenter()
        )
        self._clock = clock
        self._pending: deque[MediaCommand] = deque()
        self._lock = threading.Lock()
        # (command, target token, handler) for every registration. The token
        # returned by addTargetWithHandler: is what removeTarget: expects;
        # holding the handler as well keeps the block alive until close().
        self._registered: list[tuple[Any, Any, Any]] = []
        self._started = False
        self._last_key: tuple | None = None
        self._last_state = "stopped"
        self._last_position = 0.0
        self._last_publish = 0.0

    def start(self) -> None:
        """Register transport commands with the system. Idempotent."""
        if self._started:
            return
        self._started = True
        for name, command in (
            ("play", self._command_center.playCommand()),
            ("pause", self._command_center.pauseCommand()),
            ("toggle", self._command_center.togglePlayPauseCommand()),
            ("next", self._command_center.nextTrackCommand()),
            ("previous", self._command_center.previousTrackCommand()),
            ("stop", self._command_center.stopCommand()),
        ):
            self._bind(command, self._handler(name))
        self._bind(
            self._command_center.changePlaybackPositionCommand(), self._seek_handler
        )
        self._disable_unsupported()
        logger.debug("macOS Now Playing controls registered")

    def drain(self) -> list[MediaCommand]:
        """Return and clear the commands queued since the last drain."""
        with self._lock:
            if not self._pending:
                return []
            pending = list(self._pending)
            self._pending.clear()
        return pending

    def update(self, status: dict[str, Any]) -> None:
        """Publish ``status`` when the track, state or position changed enough.

        macOS advances the displayed position itself from the playback rate, so
        there is no need to push a new dictionary on every daemon iteration.
        """
        if not self._started:
            return
        track = status.get("track")
        state = status.get("state", "stopped")
        if track is None or state == "stopped":
            self._publish_stopped()
            return

        position = float(status.get("position") or 0.0)
        duration = status.get("duration")
        key = (
            track.get("video_id"),
            state,
            track.get("title", ""),
            tuple(track.get("artists") or ()),
            duration,
        )
        now = self._clock()
        if not self._should_publish(key, state, position, now):
            return

        self._last_key = key
        self._last_state = state
        self._last_position = position
        self._last_publish = now
        info: dict[Any, Any] = {
            MPMediaItemPropertyTitle: track.get("title") or track.get("video_id", ""),
            MPNowPlayingInfoPropertyElapsedPlaybackTime: position,
            MPNowPlayingInfoPropertyPlaybackRate: 1.0 if state == "playing" else 0.0,
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaTypeAudio,
        }
        artists = ", ".join(track.get("artists") or ())
        if artists:
            info[MPMediaItemPropertyArtist] = artists
        if duration is not None and float(duration) > 0:
            info[MPMediaItemPropertyPlaybackDuration] = float(duration)
        video_id = track.get("video_id")
        if video_id:
            info[MPNowPlayingInfoPropertyExternalContentIdentifier] = video_id
        self._info_center.setPlaybackState_(
            MPNowPlayingPlaybackStatePlaying
            if state == "playing"
            else MPNowPlayingPlaybackStatePaused
        )
        self._info_center.setNowPlayingInfo_(info)

    def close(self) -> None:
        """Disable the controls and withdraw the Now Playing entry. Idempotent."""
        if not self._started:
            return
        self._started = False
        for command, target, _handler in self._registered:
            try:
                command.setEnabled_(False)
                command.removeTarget_(target)
            except Exception:
                logger.debug("failed to unregister a media command", exc_info=True)
        self._registered.clear()
        try:
            self._info_center.setNowPlayingInfo_(None)
            self._info_center.setPlaybackState_(MPNowPlayingPlaybackStateUnknown)
        except Exception:
            logger.debug("failed to clear Now Playing info", exc_info=True)
        with self._lock:
            self._pending.clear()
        self._last_key = None
        self._last_state = "stopped"
        self._last_position = 0.0
        self._last_publish = 0.0

    def _bind(self, command: Any, handler: Callable[[Any], int]) -> None:
        command.setEnabled_(True)
        target = command.addTargetWithHandler_(handler)
        self._registered.append((command, target, handler))

    def _handler(self, name: str) -> Callable[[Any], int]:
        def handle(event: Any) -> int:
            self._enqueue(MediaCommand(name))
            return MPRemoteCommandHandlerStatusSuccess

        return handle

    def _seek_handler(self, event: Any) -> int:
        self._enqueue(MediaCommand("seek", position=float(event.positionTime())))
        return MPRemoteCommandHandlerStatusSuccess

    def _enqueue(self, command: MediaCommand) -> None:
        with self._lock:
            self._pending.append(command)

    def _disable_unsupported(self) -> None:
        for name in _UNSUPPORTED_COMMANDS:
            getter = getattr(self._command_center, name, None)
            if getter is None:
                continue
            try:
                getter().setEnabled_(False)
            except Exception:
                logger.debug("could not disable %s", name, exc_info=True)

    def _should_publish(
        self, key: tuple, state: str, position: float, now: float
    ) -> bool:
        if key != self._last_key or state != self._last_state:
            return True
        if state == "playing":
            expected = self._last_position + (now - self._last_publish)
            if abs(position - expected) > SEEK_TOLERANCE:
                return True
            return now - self._last_publish >= POSITION_REFRESH
        return abs(position - self._last_position) > PAUSED_SEEK_TOLERANCE

    def _publish_stopped(self) -> None:
        if self._last_key == ("stopped",):
            return
        self._last_key = ("stopped",)
        self._last_state = "stopped"
        self._last_position = 0.0
        self._last_publish = self._clock()
        self._info_center.setPlaybackState_(MPNowPlayingPlaybackStateStopped)
        self._info_center.setNowPlayingInfo_(None)
        logger.debug("macOS Now Playing info cleared")
