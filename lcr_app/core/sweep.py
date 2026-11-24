"""Frequency-sweep definition and execution (GUI independent)."""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np

from ..instrument.hioki3532 import Hioki3532, MeasureSettings
from .compensation import Compensation
from .params import PARAM_KEYS, derive


# Frequency setting resolution of the 3532-50 (Hioki datasheet): (upper limit Hz, step Hz)
FREQ_STEPS = [(1e3, 0.1), (1e4, 1.0), (1e5, 10.0), (1e6, 100.0), (math.inf, 1000.0)]


def meter_frequency(f: float) -> float:
    """The frequency the 3532-50 actually sets for a requested f: 0.1 Hz steps below 1 kHz,
    1 Hz to 10 kHz, 10 Hz to 100 kHz, 100 Hz to 1 MHz, 1 kHz above 1 MHz."""
    step = next(s for limit, s in FREQ_STEPS if f < limit)
    # the meter rounds halves up (1234.5 Hz -> 1235 Hz); Python's round() would give 1234
    return round(math.floor(f / step + 0.5 + 1e-9) * step, 1)


@dataclass
class SweepConfig:
    start_hz: float = 100.0
    stop_hz: float = 1e6
    points: int = 51
    spacing: str = "log"  # "log", "linear" or "list"
    custom_list: list[float] = field(default_factory=list)
    settle_ms: float = 50.0  # wait after changing frequency
    readings_per_point: int = 1  # >1 -> mean and standard deviation
    meas: MeasureSettings = field(default_factory=MeasureSettings)

    def frequencies(self) -> list[float]:
        if self.spacing == "list":
            freqs = [float(f) for f in self.custom_list]
        elif self.points < 2:
            freqs = [float(self.start_hz)]
        elif self.spacing == "log":
            freqs = list(np.geomspace(self.start_hz, self.stop_hz, int(self.points)))
        else:
            freqs = list(np.linspace(self.start_hz, self.stop_hz, int(self.points)))
        out: list[float] = []
        for f in freqs:  # round to what the meter can really set; drop resulting duplicates
            q = meter_frequency(f)
            if q not in out:
                out.append(q)
        return out

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Point:
    index: int
    freq_set: float
    freq: float
    values: dict[str, float]
    z_std: float = math.nan
    theta_std: float = math.nan
    z_raw: float = math.nan  # before open/short correction
    theta_raw: float = math.nan
    timestamp: str = ""

    def as_row(self) -> dict:
        row = {"index": self.index, "freq_set_Hz": self.freq_set, "freq_Hz": self.freq}
        row.update(self.values)
        row.update({"Z_std": self.z_std, "theta_std": self.theta_std,
                    "Z_raw": self.z_raw, "theta_raw": self.theta_raw, "timestamp": self.timestamp})
        return row


class SweepRunner:
    """Runs a sweep point by point; call stop() from another thread to abort."""

    def __init__(self, meter: Hioki3532, cfg: SweepConfig, compensation: Compensation | None = None) -> None:
        self.meter = meter
        self.cfg = cfg
        self.comp = compensation
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @property
    def stopped(self) -> bool:
        return self._stop

    def run(self, on_point: Callable[[Point, int, int], None] | None = None) -> list[Point]:
        cfg = self.cfg
        freqs = cfg.frequencies()
        self.meter.configure(cfg.meas)
        points: list[Point] = []
        try:
            for i, f in enumerate(freqs):
                if self._stop:
                    break
                self.meter.set_frequency(f)
                if cfg.settle_ms > 0:
                    time.sleep(cfg.settle_ms / 1000.0)
                zs, ths = [], []
                for _ in range(max(1, cfg.readings_per_point)):
                    if self._stop:
                        break
                    z, th = self.meter.measure_z_theta()
                    zs.append(z)
                    ths.append(th)
                if not zs:
                    break
                # overflow readings are NaN: use only the valid ones (statistics.stdev raises on NaN)
                pairs = [(a, b) for a, b in zip(zs, ths) if math.isfinite(a) and math.isfinite(b)]
                zs = [a for a, _ in pairs]
                ths = [b for _, b in pairs]
                z_raw = statistics.fmean(zs) if zs else math.nan
                th_raw = statistics.fmean(ths) if ths else math.nan
                z, th = self.comp.correct(i, f, z_raw, th_raw) if self.comp else (z_raw, th_raw)
                p = Point(
                    index=i,
                    freq_set=f,
                    freq=f,
                    values=derive(z, th, f),
                    z_std=statistics.stdev(zs) if len(zs) > 1 else math.nan,
                    theta_std=statistics.stdev(ths) if len(ths) > 1 else math.nan,
                    z_raw=z_raw,
                    theta_raw=th_raw,
                    timestamp=datetime.now().isoformat(timespec="milliseconds"),
                )
                points.append(p)
                if on_point:
                    on_point(p, i + 1, len(freqs))
        finally:
            self.meter.to_local()
        return points


ROW_KEYS = ["index", "freq_set_Hz", "freq_Hz", *PARAM_KEYS, "Z_std", "theta_std", "Z_raw", "theta_raw", "timestamp"]


def estimate_duration_s(cfg: SweepConfig) -> float:
    per_reading = {"FAST": 0.005, "NORMAL": 0.021, "SLOW": 0.072, "SLOW2": 0.140}
    t = per_reading.get(cfg.meas.speed.upper(), 0.021) * max(1, cfg.meas.averaging)
    # + ~10 ms GPIB overhead and range settling per reading (rough)
    n = len(cfg.frequencies())
    return n * (cfg.settle_ms / 1000 + max(1, cfg.readings_per_point) * (t + 0.01))
