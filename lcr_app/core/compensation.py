"""Open / short fixture compensation (done in software, point by point).

The test fixture and cables add their own impedance to every reading:
a small series impedance Zs (leads, contacts) and a stray admittance Yo
(capacitance between the clips). Measuring the fixture once OPEN (empty) and
once SHORTED, at exactly the same frequencies and test level as the real
measurement, gives both, and every sample reading is then corrected with

    Zs = Z_short
    Yo = 1 / (Z_open - Z_short)
    Z_dut = (Z_meas - Zs) / (1 - (Z_meas - Zs) * Yo)
"""
from __future__ import annotations

import cmath
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Level/frequency must match exactly; speed and averaging only change noise.
MATCH_KEYS = ("level_mode", "level_value")


def to_complex(z_abs: float, theta_deg: float) -> complex:
    if math.isnan(z_abs) or math.isnan(theta_deg):
        return complex(math.nan, math.nan)
    return cmath.rect(z_abs, math.radians(theta_deg))


def to_polar(z: complex) -> tuple[float, float]:
    if cmath.isnan(z):
        return math.nan, math.nan
    return abs(z), math.degrees(cmath.phase(z))


@dataclass
class Compensation:
    """Open and short data measured with one sweep configuration."""

    settings: dict = field(default_factory=dict)  # level_mode, level_value, speed, ...
    freqs: list[float] = field(default_factory=list)
    open_z: list[list[float]] = field(default_factory=list)  # [[|Z|, theta], ...] per freq
    short_z: list[list[float]] = field(default_factory=list)
    open_time: str = ""
    short_time: str = ""
    path: str = ""

    # ------------------------------------------------------------- state
    @property
    def has_open(self) -> bool:
        return bool(self.open_z) and len(self.open_z) == len(self.freqs)

    @property
    def has_short(self) -> bool:
        return bool(self.short_z) and len(self.short_z) == len(self.freqs)

    @property
    def complete(self) -> bool:
        return self.has_open and self.has_short

    def describe(self) -> str:
        parts = [p for p, ok in (("open", self.has_open), ("short", self.has_short)) if ok]
        return "+".join(parts) or "none"

    def mismatch(self, freqs: list[float], settings: dict) -> list[str]:
        """Reasons why this calibration does not fit a planned sweep (empty = fits)."""
        out = []
        if [float(f) for f in freqs] != [float(f) for f in self.freqs]:
            if self.freqs:
                out.append(f"frequencies: calibrated {len(self.freqs)} points "
                           f"{self.freqs[0]:g}–{self.freqs[-1]:g} Hz, sweep has {len(freqs)} points "
                           f"{freqs[0]:g}–{freqs[-1]:g} Hz")
            else:
                out.append("no calibration frequencies")
        for k in MATCH_KEYS:
            a, b = self.settings.get(k), settings.get(k)
            if a != b:
                out.append(f"{k.replace('_', ' ')}: calibrated {a}, sweep uses {b}")
        return out

    # ------------------------------------------------------------ apply
    def correct(self, index: int, freq: float, z_abs: float, theta_deg: float) -> tuple[float, float]:
        """Corrected (|Z|, theta) for the reading at sweep point `index`."""
        if not (0 <= index < len(self.freqs)) or self.freqs[index] != freq:
            raise ValueError(f"No calibration data for point {index} at {freq:g} Hz")
        zm = to_complex(z_abs, theta_deg)
        if cmath.isnan(zm):
            return z_abs, theta_deg
        zs = to_complex(*self.short_z[index]) if self.has_short else 0j
        if cmath.isnan(zs):
            zs = 0j
        yo = 0j
        if self.has_open:
            zo = to_complex(*self.open_z[index])
            # an open reading that overflowed means the stray admittance is negligible
            if not cmath.isnan(zo) and zo != zs:
                yo = 1 / (zo - zs)
        d = zm - zs
        denom = 1 - d * yo
        if denom == 0:
            return math.nan, math.nan
        return to_polar(d / denom)

    # ------------------------------------------------------------- files
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        data = asdict(self)
        data.pop("path")
        # JSON has no NaN; store None instead
        for key in ("open_z", "short_z"):
            data[key] = [[None if math.isnan(v) else v for v in pair] for pair in data[key]]
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Compensation":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key in ("open_z", "short_z"):
            data[key] = [[math.nan if v is None else float(v) for v in pair] for pair in data.get(key, [])]
        c = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        c.path = str(path)
        return c

    @staticmethod
    def now() -> str:
        return datetime.now().isoformat(timespec="seconds")
