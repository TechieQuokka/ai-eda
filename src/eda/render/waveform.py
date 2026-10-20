"""Static SVG plots of transient results, one panel per unit."""

import math
from dataclasses import dataclass
from html import escape

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


def plot(title: str, time: list[float], traces: list[Trace], bounds: list[Bound]) -> str:
    units = list(dict.fromkeys(t.unit for t in traces))
    height = MARGIN_TOP + len(units) * (PANEL_HEIGHT + MARGIN_BOTTOM) + 4
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" font-family="sans-serif" font-size="11">',
        f'<rect width="{WIDTH}" height="{height}" fill="#fff"/>',
        f'<text x="{MARGIN_LEFT}" y="20" font-size="14" font-weight="bold">{escape(title)}</text>',
    ]
    t_low, t_high = (time[0], time[-1]) if time else (0.0, 1.0)
    t_ticks = nice_ticks(t_low, t_high)
    color_index = 0
    for panel, unit in enumerate(units):
        top = MARGIN_TOP + panel * (PANEL_HEIGHT + MARGIN_BOTTOM)
        panel_traces = [t for t in traces if t.unit == unit]
        panel_bounds = [b for b in bounds if b.unit == unit]
        values = [v for t in panel_traces for v in t.values if math.isfinite(v)]
        values += [b.value for b in panel_bounds]
        y_ticks = nice_ticks(min(values), max(values)) if values else [0.0, 1.0]
        y_low, y_high = y_ticks[0], y_ticks[-1]
        left, right = MARGIN_LEFT, WIDTH - MARGIN_RIGHT
        bottom = top + PANEL_HEIGHT

        x_axis = (t_low, t_high, left, right)
        y_axis = (y_low, y_high, bottom, top)

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
        for tick in t_ticks:
            if not t_low <= tick <= t_high:
                continue
            x = _scale(tick, x_axis)
            parts.append(
                f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="#eee"/>'
                f'<text x="{x:.1f}" y="{bottom + 14}" text-anchor="middle">'
                f"{escape(format_quantity(tick, 's', 3))}</text>"
            )
        for bound in panel_bounds:
            y = _scale(bound.value, y_axis)
            parts.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#555" '
                f'stroke-dasharray="5 4"/><text x="{right - 4}" y="{y - 4:.1f}" '
                f'text-anchor="end" fill="#555">{escape(bound.label)}</text>'
            )
        legend_x = left + 8
        for trace in panel_traces:
            color = COLORS[color_index % len(COLORS)]
            color_index += 1
            points = " ".join(
                f"{_scale(t, x_axis):.1f},{_scale(v, y_axis):.1f}"
                for t, v in zip(time, trace.values, strict=False)
                if math.isfinite(v)
            )
            parts.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            )
            parts.append(
                f'<text x="{legend_x}" y="{top + 14}" fill="{color}">{escape(trace.name)}</text>'
            )
            legend_x += 8 + 7 * len(trace.name)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
