"""Small date, number and formatting helpers."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DATE_KEYS = ("calendarDate", "summaryDate", "date")


def _bfs(obj: Any) -> Iterator[dict]:
    queue = deque([obj])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            yield cur
            queue.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            queue.extend(v for v in cur if isinstance(v, (dict, list)))


def deep_get(obj: Any, *keys: str) -> Any:
    """Return the first scalar found for any key (in priority order), searching breadth-first.

    Dotted keys ("sleepScores.overall.value") match a path starting at any nested dict.
    """
    if obj is None:
        return None
    for key in keys:
        head, *rest = key.split(".")
        for d in _bfs(obj):
            if head not in d:
                continue
            value = d[head]
            for part in rest:
                value = value.get(part) if isinstance(value, dict) else None
            if value is not None and not isinstance(value, (dict, list)):
                return value
    return None


def iter_dated(obj: Any) -> Iterator[tuple[str, dict]]:
    """Yield (YYYY-MM-DD, dict) for the outermost dicts that carry a calendar-date key."""
    if isinstance(obj, dict):
        for key in _DATE_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and _DATE_RE.match(value):
                yield value[:10], obj
                return
        for value in obj.values():
            yield from iter_dated(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dated(item)


def to_date(value: str | date | None) -> date:
    if value is None or value == "today":
        return date.today()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def rnd(value: Any, digits: int = 0) -> Any:
    if value is None:
        return None
    try:
        r = round(float(value), digits)
    except (TypeError, ValueError):
        return value
    return int(r) if digits == 0 else r


def hms(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_local(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("T", " ")[:19])
    except ValueError:
        return None
