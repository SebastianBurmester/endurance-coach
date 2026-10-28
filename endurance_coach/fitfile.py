"""Parse .FIT activity files into 1 Hz numpy streams."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

import fitdecode
import numpy as np

# Gaps up to this many seconds are filled by holding the last sample; longer gaps are pauses.
_MAX_FILL_GAP_S = 5

_RECORD_FIELDS = {
    "power": ("power",),
    "hr": ("heart_rate",),
    "cadence": ("cadence",),
    "speed": ("enhanced_speed", "speed"),
    "altitude": ("enhanced_altitude", "altitude"),
    "distance": ("distance",),
    "temperature": ("temperature",),
}
_LAP_FIELDS = (
    "total_timer_time", "total_distance", "avg_power", "max_power", "normalized_power",
    "avg_heart_rate", "max_heart_rate", "avg_cadence", "total_ascent", "avg_speed", "enhanced_avg_speed",
)


@dataclass
class Stream:
    """1 Hz samples over recorded (non-paused) time. Missing values are NaN."""

    power: np.ndarray
    hr: np.ndarray
    cadence: np.ndarray
    speed: np.ndarray
    altitude: np.ndarray
    distance: np.ndarray
    temperature: np.ndarray
    elapsed_s: float
    laps: list[dict[str, Any]] = field(default_factory=list)
    session: dict[str, Any] = field(default_factory=dict)
    device_ftp: float | None = None
    device_lthr: float | None = None
    device_max_hr: float | None = None

    @property
    def seconds(self) -> int:
        return len(self.power)

    def coverage(self, name: str) -> float:
        arr = getattr(self, name)
        return float(np.mean(~np.isnan(arr))) if len(arr) else 0.0


def fit_bytes_from_download(blob: bytes) -> bytes | None:
    """The download endpoint returns a ZIP of the original file; return the .fit inside, if any."""
    if blob[:2] != b"PK":
        return blob if blob[8:12] == b".FIT" else None
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".fit"):
                return zf.read(name)
    return None


def _value(frame: fitdecode.FitDataMessage, names: tuple[str, ...]) -> Any:
    for name in names:
        if frame.has_field(name):
            value = frame.get_value(name, fallback=None)
            if value is not None:
                return value
    return None


def parse(data: bytes) -> Stream:
    records: list[tuple[float, list[Any]]] = []
    laps: list[dict[str, Any]] = []
    session: dict[str, Any] = {}
    device: dict[str, Any] = {}

    with fitdecode.FitReader(
        io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED, error_handling=fitdecode.ErrorHandling.IGNORE
    ) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue
                records.append((ts.timestamp(), [_value(frame, names) for names in _RECORD_FIELDS.values()]))
            elif frame.name == "lap":
                lap = {name: frame.get_value(name, fallback=None) for name in _LAP_FIELDS if frame.has_field(name)}
                start = frame.get_value("start_time", fallback=None)
                lap["start_time"] = start.timestamp() if start else None
                laps.append(lap)
            elif frame.name == "session":
                for name in ("threshold_power", "normalized_power", "training_stress_score", "intensity_factor",
                             "total_work", "total_ascent", "total_timer_time", "total_elapsed_time", "sport", "sub_sport"):
                    if frame.has_field(name):
                        session[name] = frame.get_value(name, fallback=None)
            elif frame.name == "zones_target":
                for name in ("functional_threshold_power", "threshold_heart_rate", "max_heart_rate"):
                    if frame.has_field(name):
                        device[name] = frame.get_value(name, fallback=None)

    return _resample(records, laps, session, device)


def _resample(records, laps, session, device) -> Stream:
    records.sort(key=lambda r: r[0])
    rows: list[list[Any]] = []
    starts: list[float] = []  # absolute timestamp of each 1 Hz row (for lap alignment)
    prev_t: float | None = None
    for t, values in records:
        if prev_t is not None:
            gap = int(round(t - prev_t))
            if gap <= 0:
                continue
            if gap <= _MAX_FILL_GAP_S:
                for k in range(1, gap):
                    rows.append(rows[-1])
                    starts.append(prev_t + k)
        rows.append(values)
        starts.append(t)
        prev_t = t

    width = len(_RECORD_FIELDS)
    arr = np.array([[np.nan if v is None else float(v) for v in row] for row in rows], dtype=float).reshape(-1, width)
    cols = {name: arr[:, i] for i, name in enumerate(_RECORD_FIELDS)}

    # Attach 1 Hz row ranges to laps so lap-level NP can be computed from the stream.
    ts = np.array(starts)
    for lap in laps:
        if lap.get("start_time") is not None and lap.get("total_timer_time") and len(ts):
            i0 = int(np.searchsorted(ts, lap["start_time"]))
            i1 = int(np.searchsorted(ts, lap["start_time"] + float(lap["total_timer_time"])))
            lap["_rows"] = (i0, i1)

    elapsed = float(records[-1][0] - records[0][0]) if records else 0.0
    return Stream(
        **cols,
        elapsed_s=elapsed,
        laps=laps,
        session=session,
        device_ftp=device.get("functional_threshold_power") or session.get("threshold_power"),
        device_lthr=device.get("threshold_heart_rate"),
        device_max_hr=device.get("max_heart_rate"),
    )
