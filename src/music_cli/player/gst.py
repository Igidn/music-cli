"""Audio playback through GStreamer (cross-platform, primary Linux).

Streams audio through ``playbin3`` (or ``playbin`` as a fallback). Each
track is downloaded to a local file (cache-managed or temporary) before
playback — the same as the AVFoundation backend.

GStreamer pipelines run in their own threads so the main thread never
needs to "pump" an event loop for audio to advance; :meth:`pump` only
processes bus messages (EOS, errors).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from ..core.errors import PlayerError  # noqa: E402
from ..storage.cache import AudioCache  # noqa: E402
from ..yt.cookies import Cookies  # noqa: E402
from ..yt.extract import StreamInfo  # noqa: E402
from .base import LocalFile, default_fetch_stream  # noqa: E402

# Initialise GStreamer once at module load time.
Gst.init(None)

#: Which playbin variant to use. ``playbin3`` is the newer, faster pipeline
#: but some GStreamer installations only ship ``playbin``.
_PLAYBIN_ELEMENT = "playbin3"


def _make_pipeline() -> Gst.Element:
    """Create a ``playbin3`` (fallback ``playbin``) pipeline element."""
    pipeline = Gst.ElementFactory.make(_PLAYBIN_ELEMENT, "player")
    if pipeline is not None:
        return pipeline
    # Fallback to playbin
    pipeline = Gst.ElementFactory.make("playbin", "player")
    if pipeline is not None:
        return pipeline
    raise RuntimeError(
        "GStreamer: neither playbin3 nor playbin element is available. "
        "Install gst-plugins-base."
    )


def _state_name(state: Gst.State) -> str:
    """Human-readable GStreamer state name."""
    names = {
        Gst.State.NULL: "NULL",
        Gst.State.READY: "READY",
        Gst.State.PAUSED: "PAUSED",
        Gst.State.PLAYING: "PLAYING",
    }
    return names.get(state, str(state))


class GStreamerPlayer:
    """Plays audio streams through GStreamer (playbin3/playbin).

    The pipeline runs in its own threads, so :meth:`pump` only needs to
    process bus messages (EOS, errors, state changes). All properties
    (position, duration, volume, …) query the pipeline directly.
    """

    def __init__(
        self,
        *,
        volume: int = 80,
        loop: bool = False,
        on_track_end: Callable[[], None] | None = None,
        cookies: Cookies | None = None,
        fetch_stream: Callable[[StreamInfo], LocalFile] | None = None,
        cache: AudioCache | None = None,
        download_progress: Callable[
            [], Callable[[dict[str, Any]], None] | None
        ] | None = None,
    ) -> None:
        self.on_track_end = on_track_end
        self._loop = loop
        self._ended = threading.Event()
        self._title = ""
        self._pipeline = _make_pipeline()
        self._bus = self._pipeline.get_bus()
        self._fetch_stream = fetch_stream or default_fetch_stream(
            cookies, cache=cache, download_progress=download_progress
        )
        self._volume = max(0, min(100, int(volume)))
        self._muted = False
        self._local_path: str | None = None
        self._eos_lock = threading.Lock()
        self._eos_received = False
        # Apply initial volume — applied via property setter below.
        self._apply_volume()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pump(self) -> None:
        """Process pending GStreamer bus messages without blocking.

        GStreamer pipelines advance in their own threads, so this is
        primarily about dispatching EOS / error messages. Call from the
        main loop (daemon's serve loop) periodically.
        """
        while True:
            msg = self._bus.pop()
            if msg is None:
                break
            self._handle_message(msg)

    def play(self, stream: StreamInfo) -> None:
        """Fetch the stream locally and start playing it."""
        self._ended.clear()
        self._remove_local_file()
        self._stop_pipeline()
        try:
            local = self._fetch_stream(stream)
        except PlayerError:
            self._title = ""
            return
        self._title = stream.title
        uri = (
            local.path
            if local.path.startswith("file://")
            else f"file://{local.path}"
        )
        self._pipeline.set_property("uri", uri)
        if local.owned:
            self._local_path = uri.removeprefix("file://")
        self._apply_volume()
        self._set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        self._stop_pipeline()
        self._remove_local_file()

    def pause(self) -> None:
        self._set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        self._set_state(Gst.State.PLAYING)

    def toggle(self) -> None:
        if self.paused:
            self.resume()
        else:
            self.pause()

    def seek(self, seconds: float) -> None:
        """Seek to an absolute position (seconds)."""
        ns = int(seconds * Gst.SECOND)
        self._pipeline.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, ns
        )

    def seek_relative(self, delta: float) -> None:
        self.seek(self.position + delta)

    def close(self) -> None:
        self.stop()
        self._set_state(Gst.State.NULL)

    def wait_for_end(self, timeout: float | None = None) -> bool:
        return self._ended.wait(timeout)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def volume(self) -> int:
        return self._volume

    @volume.setter
    def volume(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._apply_volume()

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = bool(value)
        self._apply_volume()

    @property
    def loop(self) -> bool:
        return self._loop

    @loop.setter
    def loop(self, value: bool) -> None:
        self._loop = bool(value)

    @property
    def paused(self) -> bool:
        """Whether playback is paused, or no track is loaded.

        Returns ``True`` when the pipeline is in NULL (no track) or PAUSED,
        matching the AVFoundationPlayer contract where a freshly constructed
        or stopped player is considered paused until :meth:`play` is called.
        """
        _, state, _ = self._pipeline.get_state(Gst.CLOCK_TIME_NONE)
        return state in (Gst.State.NULL, Gst.State.PAUSED, Gst.State.READY)

    @property
    def position(self) -> float:
        """Current playback position in seconds."""
        success, pos = self._pipeline.query_position(Gst.Format.TIME)
        if success:
            return pos / Gst.SECOND
        return 0.0

    @property
    def duration(self) -> float | None:
        """Duration in seconds, or ``None`` when unknown."""
        success, dur = self._pipeline.query_duration(Gst.Format.TIME)
        if success and dur > 0:
            return dur / Gst.SECOND
        return None

    @property
    def media_title(self) -> str:
        return self._title

    @property
    def eof_reached(self) -> bool:
        return self._ended.is_set()

    @property
    def playing(self) -> bool:
        """Whether a track is loaded and hasn't failed."""
        _, state, pending = self._pipeline.get_state(Gst.CLOCK_TIME_NONE)
        return state in (Gst.State.PLAYING, Gst.State.PAUSED) or pending == Gst.State.PLAYING

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_volume(self) -> None:
        """Push the current volume/mute state onto the pipeline."""
        level = 0.0 if self._muted else self._volume / 100.0
        self._pipeline.set_property("volume", level)

    def _set_state(self, state: Gst.State) -> None:
        """Transition the pipeline to *state*, flushing bus messages."""
        self._pipeline.set_state(state)
        # Drain any messages that arrived during the state transition
        # so they don't accumulate for the next pump() call.
        self.pump()

    def _stop_pipeline(self) -> None:
        """Stop the pipeline: set to NULL and clear the URI."""
        with self._eos_lock:
            self._eos_received = False
        self._set_state(Gst.State.NULL)

    def _remove_local_file(self) -> None:
        """Delete an owned local file, if present."""
        if self._local_path is not None:
            try:
                os.unlink(self._local_path)
            except OSError:
                pass
            self._local_path = None

    def _handle_message(self, msg: Gst.Message) -> None:
        """Dispatch a single GStreamer bus message."""
        if msg.type == Gst.MessageType.EOS:
            self._on_eos()
        elif msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            # Log the error but don't crash — the client's retry loop
            # will detect that playback stopped and try again.
            import logging

            logging.getLogger(__name__).warning(
                "GStreamer error: %s (%s)", err.message, debug
            )
            self._ended.set()
            if self.on_track_end:
                self.on_track_end()

    def _on_eos(self) -> None:
        """Handle end-of-stream: loop or notify track-end."""
        with self._eos_lock:
            if self._eos_received:
                return
            self._eos_received = True
        if self._loop:
            # Seek to start and replay.  GStreamer playbin stays in
            # PLAYING after EOS, so the seek (with FLUSH) clears the
            # EOS condition and resumes playback from position 0.
            # Calling set_state(PLAYING) afterwards ensures the
            # pipeline doesn't stall on older GStreamer versions that
            # go to PAUSED on EOS.
            self._pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0,
            )
            self._set_state(Gst.State.PLAYING)
            with self._eos_lock:
                self._eos_received = False
            return
        self._ended.set()
        if self.on_track_end:
            self.on_track_end()