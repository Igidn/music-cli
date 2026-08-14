"""Lyrics strip: a fixed-height, collapsible pane above the player.

Shows a small time-synced window of the current track's lyrics, centred on
the line currently being sung. Hidden until toggled (default ``display:
none``), so it costs nothing until the user asks for it. When a track has no
timed lyrics this renders an explanatory placeholder instead.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

WINDOW = 7


class LyricsPane(Widget):
    """Fixed-height lyric strip with a moving highlight window."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lines: list[tuple[int, str]] = []
        self._position_ms = 0
        self._loaded = False
        self._mounted = False

    def compose(self) -> ComposeResult:
        yield Static("Lyrics", id="lyrics-title")
        yield Static("", id="lyrics-body")

    def on_mount(self) -> None:
        self._mounted = True
        self._rerender()

    def set_lyrics(self, lines: list[tuple[int, str]]) -> None:
        self._lines = sorted(lines, key=lambda item: item[0])
        self._loaded = True
        self._position_ms = 0
        self._rerender()

    def set_unavailable(self, reason: str) -> None:
        self._lines = []
        self._loaded = True
        if not self._mounted:
            return
        self.query_one("#lyrics-body", Static).update(reason)
        self.query_one("#lyrics-title", Static).update("Lyrics")

    def reset(self) -> None:
        self._lines = []
        self._loaded = False
        self._position_ms = 0
        if not self._mounted:
            return
        self.query_one("#lyrics-title", Static).update("Lyrics")
        self.query_one("#lyrics-body", Static).update("")

    def set_position(self, position_sec: float) -> None:
        ms = int(position_sec * 1000)
        if ms == self._position_ms or not self._loaded or not self._mounted:
            return
        self._position_ms = ms
        self._rerender()

    def _active_index(self) -> int:
        idx = 0
        for i, (start, _) in enumerate(self._lines):
            if self._position_ms >= start:
                idx = i
            else:
                break
        return idx

    def _rerender(self) -> None:
        if not self._mounted:
            return
        if not self._lines:
            if not self._loaded:
                self.query_one("#lyrics-body", Static).update("Loading lyrics…")
            return
        active = self._active_index()
        start = max(0, active - WINDOW // 2)
        end = min(len(self._lines), start + WINDOW)
        rows = []
        for i in range(start, end):
            text = self._lines[i][1]
            if i == active:
                rows.append(f"[bold]{text}[/bold]")
            else:
                rows.append(text)
        self.query_one("#lyrics-body", Static).update("\n".join(rows))
        self.query_one("#lyrics-title", Static).update(
            f"Lyrics · line {active + 1}/{len(self._lines)}"
        )
