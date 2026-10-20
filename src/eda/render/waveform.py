"""Static SVG plots: transient (linear time) and AC (log frequency), one panel per unit."""

import math
from dataclasses import dataclass
from html import escape
from typing import Literal

from eda.units import format_quantity

WIDTH = 760
PANEL_HEIGHT = 220
MARGIN_LEFT = 70
MARGIN_RIGHT = 20
MARGIN_TOP = 34
MARGIN_BOTTOM = 36
COLORS = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2")


@dataclass(frozen=True)
class Trace:
    name: str
    unit: str
    values: list[float]


@dataclass(frozen=True)
class Bound:
    label: str
    unit: str
    value: float
    axis: Literal["x", "y"] = "y"  # x: a time/frequency limit drawn as a vertical line


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if not math.isfinite(low) or not math.isfinite(high):
        return []
    if high == low:
        high, low = high + 1 if high == 0 else high * 1.1, low - 1 if low == 0 else low * 0.9
        low, high = min(low, high), max(low, high)
    raw = (high - low) / max(count, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start = math.floor(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step * 1e-9:
        ticks.append(round(value / step) * step)
        value += step
    if ticks and ticks[0] > low:
        ticks.insert(0, ticks[0] - step)
    if ticks and ticks[-1] < high:
        ticks.append(ticks[-1] + step)
    return ticks


def _scale(value: float, axis: tuple[float, float, float, float]) -> float:
    """Map `value` from data range (lo, hi) to pixel range (start, end)."""
    lo, hi, start, end = axis
    return start + (value - lo) / ((hi - lo) or 1) * (end - start)


def log_ticks(low: float, high: float) -> list[float]:
    """Decades (1, 10, 100, ...) covering a positive range."""
    if not low > 0 or not high > low:
        return []
    return [10.0**k for k in range(math.floor(math.log10(low)), math.ceil(math.log10(high)) + 1)]


def _label(x: float, y: float, text: str, fill: str, anchor: str = "start") -> str:
    """Text with a white halo drawn underneath, so it stays readable over traces.
    (A separate element instead of `paint-order`, which CairoSVG does not support.)"""
    attrs = f'x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"'
    body = escape(text)
    return (
        f'<text {attrs} fill="#fff" stroke="#fff" stroke-width="3">{body}</text>'
        f'<text {attrs} fill="{fill}">{body}</text>'
    )


def plot(
    title: str,
    x: list[float],
    traces: list[Trace],
    bounds: list[Bound],
    x_unit: str = "s",
    log_x: bool = False,
) -> str:
    def tx(value: float) -> float:
        return math.log10(value) if log_x else value

    units = list(dict.fromkeys(t.unit for t in traces))
    height = MARGIN_TOP + len(units) * (PANEL_HEIGHT + MARGIN_BOTTOM) + 4
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" font-family="sans-serif" font-size="11">',
        f'<rect width="{WIDTH}" height="{height}" fill="#fff"/>',
        f'<text x="{MARGIN_LEFT}" y="20" font-size="14" font-weight="bold">{escape(title)}</text>',
    ]
    x_low, x_high = (x[0], x[-1]) if x else (1.0, 10.0)
    x_ticks = log_ticks(x_low, x_high) if log_x else nice_ticks(x_low, x_high)
    x_bounds = [b for b in bounds if b.axis == "x"]
    left, right = MARGIN_LEFT, WIDTH - MARGIN_RIGHT
    x_axis = (tx(x_low), tx(x_high), left, right)
    color_index = 0
    for panel, unit in enumerate(units):
        top = MARGIN_TOP + panel * (PANEL_HEIGHT + MARGIN_BOTTOM)
        bottom = top + PANEL_HEIGHT
        panel_traces = [t for t in traces if t.unit == unit]
        panel_bounds = [b for b in bounds if b.unit == unit and b.axis == "y"]
        values = [v for t in panel_traces for v in t.values if math.isfinite(v)]
        values += [b.value for b in panel_bounds]
        y_ticks = nice_ticks(min(values), max(values)) if values else [0.0, 1.0]
        y_axis = (y_ticks[0], y_ticks[-1], bottom, top)

        parts.append(
            f'<rect x="{left}" y="{top}" width="{right - left}" height="{PANEL_HEIGHT}" '
            'fill="none" stroke="#999"/>'
        )
        for tick in y_ticks:
            y = _scale(tick, y_axis)
            parts.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#eee"/>'
                f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">'
                f"{escape(format_quantity(tick, unit, 3))}</text>"
            )
        for tick in x_ticks:
            if not x_low <= tick <= x_high:
                continue
            px = _scale(tx(tick), x_axis)
            parts.append(
                f'<line x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{bottom}" stroke="#eee"/>'
                f'<text x="{px:.1f}" y="{bottom + 14}" text-anchor="middle">'
                f"{escape(format_quantity(tick, x_unit, 3))}</text>"
            )
        for bound in panel_bounds:
            y = _scale(bound.value, y_axis)
            parts.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#555" '
                'stroke-dasharray="5 4"/>'
            )
            if bound.label:
                parts.append(_label(right - 4, y - 4, bound.label, "#555", "end"))
        for bound in x_bounds:
            if not (bound.value > 0 or not log_x):
                continue
            px = _scale(tx(bound.value), x_axis)
            if not left <= px <= right:
                continue
            parts.append(
                f'<line x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{bottom}" stroke="#555" '
                'stroke-dasharray="5 4"/>'
            )
            if bound.label and panel == 0:
                anchor = "end" if px > (left + right) / 2 else "start"
                offset = -4 if anchor == "end" else 4
                parts.append(_label(px + offset, top + 30, bound.label, "#555", anchor))
        legend_x = left + 8
        for trace in panel_traces:
            color = COLORS[color_index % len(COLORS)]
            color_index += 1
            points = " ".join(
                f"{_scale(tx(t), x_axis):.1f},{_scale(v, y_axis):.1f}"
                for t, v in zip(x, trace.values, strict=False)
                if math.isfinite(v) and (t > 0 or not log_x)
            )
            parts.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            )
            parts.append(_label(legend_x, top + 14, trace.name, color))
            legend_x += 8 + 7 * len(trace.name)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
