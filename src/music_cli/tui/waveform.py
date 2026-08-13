"""Animated spectrum visualizer for the now-playing bar."""

from __future__ import annotations

import math
import zlib

from rich.text import Text
from textual.widget import Widget

BLOCKS = "▁▂▃▄▅▆▇█"
# Accent → cyan gradient, sampled per bar level.
GRADIENT = ("#8b5cf6", "#a78bfa", "#c4b5fd", "#67e8f9")
PAUSED_COLOR = "#5b6580"
IDLE_COLOR = "#3a4358"


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

    def on_mount(self) -> None:
        self.set_interval(self.TICK, self._tick)

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

    @staticmethod
    def _color_for(level: float, paused: bool) -> str:
        if paused:
            return PAUSED_COLOR
        return GRADIENT[min(int(level * len(GRADIENT)), len(GRADIENT) - 1)]

    def render(self) -> Text:
        width = max(0, self.size.width)
        rows = max(0, self.size.height)
        text = Text()
        if not rows:
            return text
        if not self._active:
            for _ in range(rows - 1):
                text.append("\n")
            text.append("▁" * width, style=IDLE_COLOR)
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
