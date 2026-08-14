"""Animated spectrum visualizer for the now-playing bar."""

from __future__ import annotations

import math
import zlib

from rich.text import Text
from textual.color import Color
from textual.widget import Widget

BLOCKS = "▁▂▃▄▅▆▇█"


def _to_hex(color: Color) -> str:
    """RGB hex for a color regardless of whether it's a named ANSI color."""
    r, g, b = color.rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def _gradient_colors(theme) -> tuple[str, str, str, str]:
    """Four-stop gradient between the theme's accent and secondary colors."""
    start = (
        _theme_color(theme, "accent")
        or _theme_color(theme, "primary")
        or Color("#888888")
    )
    end = _theme_color(theme, "secondary") or start
    if start == end:
        end = start.lighten(0.35)
    return tuple(_to_hex(start.blend(end, i / 3)) for i in range(4))


def _theme_color(theme, name: str) -> Color | None:
    value = getattr(theme, name, None)
    if value is None:
        return None
    color = value if isinstance(value, Color) else Color.parse(value)
    if color.ansi is not None:
        r, g, b = color.rgb
        return Color(r, g, b)
    return color


def _muted_colors(theme) -> tuple[str, str]:
    """Paused and idle tints derived from the theme's surface/muted text."""
    base = (
        _theme_color(theme, "surface")
        or _theme_color(theme, "background")
        or _theme_color(theme, "panel")
        or _theme_color(theme, "foreground")
        or Color("#000000")
    )
    fg = _theme_color(theme, "foreground")
    muted = theme.variables.get("text-muted")
    if muted is not None:
        muted_color = muted if isinstance(muted, Color) else Color.parse(muted)
        if muted_color.ansi is not None:
            r, g, b = muted_color.rgb
            muted_color = Color(r, g, b)
        fg = muted_color
    if fg is None:
        fg = base
    return _to_hex(base.blend(fg, 0.6)), _to_hex(base.blend(fg, 0.25))


def _unit(seed: str, index: int) -> float:
    """Deterministic pseudo-random value in [0, 1) for a seed/column pair."""
    return (zlib.crc32(f"{seed}:{index}".encode()) & 0xFFFF) / 0xFFFF


class Waveform(Widget):
    """Animated equalizer bars for the track currently playing.

    Column amplitudes are a deterministic per-track profile (seeded with the
    track identity) modulated by layered sines over time, so every track gets
    its own shape while the bars keep dancing. Nothing is decoded from the
    audio; the bars are purely decorative.
    """

    DEFAULT_CSS = """
    Waveform {
        height: 3;
        margin: 0 1 1 1;
    }
    """

    TICK = 1 / 12

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._seed = ""
        self._active = False
        self._paused = False
        self._time = 0.0
        self._gradient = ("#8b5cf6", "#a78bfa", "#c4b5fd", "#67e8f9")
        self._paused_color = "#5b6580"
        self._idle_color = "#3a4358"

    def set_seed(self, seed: str) -> None:
        if seed != self._seed:
            self._seed = seed
            self._time = 0.0

    def set_active(self, active: bool) -> None:
        self._active = active
        self.refresh()

    def set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            self.refresh()

    def _tick(self) -> None:
        if not self._active or self._paused:
            return
        self._time += self.TICK
        self.refresh()

    def _column_level(self, column: int) -> float:
        seed = self._seed
        profile = _unit(seed, column)
        phase = _unit(seed, column + 1000) * math.tau
        speed = 1.0 + 2.5 * _unit(seed, column + 2000)
        pulse = 0.5 + 0.5 * math.sin(self._time * speed + phase)
        beat = 0.75 + 0.25 * math.sin(self._time * 2.4)
        return min(1.0, (0.15 + 0.85 * profile) * (0.35 + 0.65 * pulse) * beat)

    def _color_for(self, level: float, paused: bool) -> str:
        if paused:
            return self._paused_color
        return self._gradient[
            min(int(level * len(self._gradient)), len(self._gradient) - 1)
        ]

    def _apply_theme(self) -> None:
        theme = self.app.current_theme
        self._gradient = _gradient_colors(theme)
        self._paused_color, self._idle_color = _muted_colors(theme)

    def on_mount(self) -> None:
        self._apply_theme()
        self.set_interval(self.TICK, self._tick)

    def render(self) -> Text:
        width = max(0, self.size.width)
        rows = max(0, self.size.height)
        text = Text()
        if not rows:
            return text
        if not self._active:
            for _ in range(rows - 1):
                text.append("\n")
            text.append("▁" * width, style=self._idle_color)
            return text
        for row in range(rows):
            if row:
                text.append("\n")
            for column in range(width):
                level = self._column_level(column)
                # Bottom-up bars: each row covers one eighth of the range.
                cell_value = level * (rows * len(BLOCKS)) - (rows - 1 - row) * len(
                    BLOCKS
                )
                if cell_value <= 0:
                    text.append(" ")
                else:
                    text.append(
                        BLOCKS[min(int(cell_value), len(BLOCKS)) - 1],
                        style=self._color_for(level, self._paused),
                    )
        return text
