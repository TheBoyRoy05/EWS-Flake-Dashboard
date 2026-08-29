"""Geometry for the trend chart, computed here and rendered as inline SVG.

There is no JavaScript on any page of this dashboard. A month of daily points does not need a
charting library, and one `<title>` per point gets hover text from the browser itself, so the chart
costs no CDN dependency, no build step and no vendored bundle. If this ever needs zooming or a
crosshair, uPlot is the escape hatch — vendor it then, not now.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ews_dashboard.analysis import trend

WIDTH = 1600
# The card is a fixed height — the queue rail's — and the svg scales to fit inside it without
# distorting, so this aspect ratio only decides how much of that card's width the plot uses. 1600x700
# fills it at the widths the page is read at.
HEIGHT = 700
PADDING_LEFT = 52
PADDING_RIGHT = 20
PADDING_TOP = 18
PADDING_BOTTOM = 34

GRIDLINE_COUNT = 4
SMALLEST_Y_MAX_PCT = 10
DAY_LABELS_WANTED = 8

PLOT_WIDTH = WIDTH - PADDING_LEFT - PADDING_RIGHT
PLOT_HEIGHT = HEIGHT - PADDING_TOP - PADDING_BOTTOM


@dataclass(frozen=True)
class Dot:
    x: float
    y: float
    title: str


@dataclass(frozen=True)
class Gridline:
    y: float
    label: str


@dataclass(frozen=True)
class DayLabel:
    x: float
    text: str


@dataclass(frozen=True)
class DeploymentMark:
    x: float
    label: str
    title: str


@dataclass(frozen=True)
class Chart:
    y_max: int
    dots: tuple
    rolling_path: str
    gridlines: tuple
    day_labels: tuple
    deployments: tuple

    width: int = WIDTH
    height: int = HEIGHT
    plot_left: int = PADDING_LEFT
    plot_top: int = PADDING_TOP
    plot_width: int = PLOT_WIDTH
    plot_height: int = PLOT_HEIGHT

    @property
    def empty(self) -> bool:
        return not self.dots


def _y(percent: float, y_max: int) -> float:
    return round(PADDING_TOP + PLOT_HEIGHT * (1 - percent / y_max), 1)


def _x_of_fraction(fraction: float) -> float:
    return round(PADDING_LEFT + PLOT_WIDTH * fraction, 1)


def _y_max(points: list) -> int:
    observed = [
        percent
        for point in points
        for percent in (point.daily_pct, point.rolling_pct)
        if percent is not None
    ]
    highest = max(observed) if observed else 0
    return max(SMALLEST_Y_MAX_PCT, 10 * math.ceil(highest / 10))


def _dot_title(point: trend.Point) -> str:
    classifiable = point.counts.classifiable
    blamed = point.counts.partial_fp + point.counts.false_red
    return (f'{point.day.isoformat()}: {point.daily_pct}% — '
            f'{blamed} of {classifiable} builds blamed an author for noise')


def _rolling_path(points: list, y_max: int) -> str:
    """A single path, broken wherever a day had nothing to average, so a gap reads as a gap."""
    segments = []
    pen_is_down = False
    for index, point in enumerate(points):
        if point.rolling_pct is None:
            pen_is_down = False
            continue
        command = 'L' if pen_is_down else 'M'
        segments.append(f'{command} {_x_of_day(index, len(points))} {_y(point.rolling_pct, y_max)}')
        pen_is_down = True
    return ' '.join(segments)


def _x_of_day(index: int, count: int) -> float:
    return _x_of_fraction((index + 0.5) / count)


def _label_stride(count: int) -> int:
    """Days between dated labels, so a 90-day span reads as densely as a 14-day one."""
    return max(1, math.ceil(count / DAY_LABELS_WANTED))


def _deployment_marks(points: list, deployments: list) -> tuple:
    first = trend.day_bounds(points[0].day)[0]
    last = trend.day_bounds(points[-1].day)[1]
    span = last - first
    marks = []
    for deployment in deployments:
        at = int(deployment.at.timestamp())
        marks.append(DeploymentMark(
            x=_x_of_fraction((at - first) / span),
            label=deployment.label,
            title=f'{deployment.at.strftime("%Y-%m-%d %H:%M")} UTC — {deployment.detail}',
        ))
    return tuple(marks)


def of_trend(points: list, deployments: Optional[list] = None) -> Chart:
    if not points:
        return Chart(SMALLEST_Y_MAX_PCT, (), '', (), (), ())

    y_max = _y_max(points)
    count = len(points)
    return Chart(
        y_max=y_max,
        dots=tuple(
            Dot(_x_of_day(index, count), _y(point.daily_pct, y_max), _dot_title(point))
            for index, point in enumerate(points)
            if point.daily_pct is not None
        ),
        rolling_path=_rolling_path(points, y_max),
        gridlines=tuple(
            Gridline(_y(y_max * step / GRIDLINE_COUNT, y_max), f'{round(y_max * step / GRIDLINE_COUNT)}%')
            for step in range(GRIDLINE_COUNT + 1)
        ),
        day_labels=tuple(
            DayLabel(_x_of_day(index, count), points[index].day.strftime('%b %d'))
            for index in reversed(range(count - 1, -1, -_label_stride(count)))
        ),
        deployments=_deployment_marks(points, deployments or []),
    )
