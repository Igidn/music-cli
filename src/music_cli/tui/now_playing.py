"""Now-playing bar: track metadata, progress and volume."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.color import Gradient
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import ProgressBar, Static

from .waveform import Waveform

BAR_GRADIENT = Gradient((0.0, "#8b5cf6"), (0.6, "#a78bfa"), (1.0, "#67e8f9"))


def format_time(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class NowPlaying(Widget):
    """Bottom bar showing the current track, playback progress and volume."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._has_track = False
        self._paused = False
        self._duration: float = 0.0
        self._position: float = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(id="np-head"):
            yield Static("♪", id="np-icon")
            with Vertical(id="np-meta"):
                yield Static("Nothing playing", id="np-title")
                yield Static(
                    "Type to search, or press / to focus the search box",
                    id="np-subtitle",
                )
            yield Static("Loop", id="np-loop", classes="off")
            yield Static("Auto", id="np-auto", classes="off")
            yield Static("", id="np-volume")
            yield Static("--:-- / --:--", id="np-time")
        yield Waveform(id="np-waveform")
        yield ProgressBar(
            total=0,
            show_eta=False,
            show_percentage=False,
            gradient=BAR_GRADIENT,
            id="np-progress",
        )
        with Horizontal(id="np-foot"):
            yield Static("Ready", id="np-status")

    def set_track(
        self, title: str, subtitle: str, duration: float | None = None
    ) -> None:
        self.query_one("#np-title", Static).update(title)
        self.query_one("#np-subtitle", Static).update(subtitle)
        self._has_track = True
        if duration:
            self._duration = float(duration)
        waveform = self.query_one(Waveform)
        waveform.set_seed(f"{title} — {subtitle}")
        waveform.set_active(True)
        waveform.set_paused(self._paused)
        self._render_icon()
        self._render_time()

    def clear_track(self) -> None:
        self._has_track = False
        self._paused = False
        self._duration = 0.0
        self._position = 0.0
        self.query_one("#np-title", Static).update("Nothing playing")
        self.query_one("#np-subtitle", Static).update(
            "Type to search, or press / to focus the search box"
        )
        self.query_one("#np-progress", ProgressBar).update(progress=0, total=0)
        self.query_one("#np-time", Static).update("--:-- / --:--")
        self.query_one(Waveform).set_active(False)
        self._render_icon()

    def set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            self._render_icon()
        self.query_one(Waveform).set_paused(paused)

    def set_progress(self, position: float, duration: float) -> None:
        self._position = max(0.0, position)
        if duration:
            self._duration = duration
        if self._has_track:
            self.query_one("#np-progress", ProgressBar).update(
                progress=self._position,
                total=self._duration,
            )
            self._render_time()

    def set_status(self, text: str) -> None:
        self.query_one("#np-status", Static).update(text)

    def set_volume(self, volume: int, muted: bool) -> None:
        text = f"Vol {volume}%"
        if muted:
            text += " · muted"
        self.query_one("#np-volume", Static).update(text)

    def set_modes(self, auto_next: bool, loop: bool) -> None:
        self.query_one("#np-loop", Static).set_classes(("on",) if loop else ("off",))
        self.query_one("#np-auto", Static).set_classes(
            ("on",) if auto_next else ("off",)
        )

    def _render_icon(self) -> None:
        icon = self.query_one("#np-icon", Static)
        if not self._has_track:
            icon.update("♪")
            self.set_classes(())
        elif self._paused:
            icon.update("⏸")
            self.set_classes(("has-track", "paused"))
        else:
            icon.update("▶")
            self.set_classes(("has-track",))

    def _render_time(self) -> None:
        if not self._has_track:
            return
        text = f"{format_time(self._position)} / {format_time(self._duration)}"
        self.query_one("#np-time", Static).update(text)
