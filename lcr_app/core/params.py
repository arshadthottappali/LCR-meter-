"""Derive every LCR parameter from the impedance magnitude |Z|, phase angle
theta and the test frequency.

The 3532-50 itself computes Ls, Cp, D, ... from the same two primary
quantities, so reading only |Z| and theta from the meter and computing the
rest here gives identical results, and keeps the GPIB traffic small and fast.

Conventions (same as the Hioki front panel):
    Z  = Rs + jX          (series equivalent circuit)
    Y  = 1/Z = G + jB     (parallel equivalent circuit)
    Ls = X / w            Cs = -1 / (w X)
    Lp = -1 / (w B)       Cp = B / w
    Rp = 1 / G
    D  = |Rs / X|         Q  = |X / Rs| = 1 / D
"""
from __future__ import annotations

import math

# (key, label, unit) in the order used for tables and CSV files.
PARAMETERS: list[tuple[str, str, str]] = [
    ("Z", "|Z|", "Ω"),
    ("theta", "θ", "°"),
    ("Rs", "Rs", "Ω"),
    ("X", "X", "Ω"),
    ("Ls", "Ls", "H"),
    ("Cs", "Cs", "F"),
    ("Rp", "Rp", "Ω"),
    ("Lp", "Lp", "H"),
    ("Cp", "Cp", "F"),
    ("Y", "|Y|", "S"),
    ("G", "G", "S"),
    ("B", "B", "S"),
    ("D", "D", ""),
    ("Q", "Q", ""),
]
PARAM_KEYS = [p[0] for p in PARAMETERS]
PARAM_LABEL = {k: lbl for k, lbl, _ in PARAMETERS}
PARAM_UNIT = {k: u for k, _, u in PARAMETERS}

NAN = float("nan")


def _div(a: float, b: float) -> float:
    if b == 0 or math.isnan(b) or math.isnan(a):
        return NAN
    return a / b


def derive(z_abs: float, theta_deg: float, freq_hz: float) -> dict[str, float]:
    """Return all parameters in PARAM_KEYS from |Z| [ohm], theta [deg], f [Hz]."""
    out = {k: NAN for k in PARAM_KEYS}
    if any(math.isnan(v) for v in (z_abs, theta_deg, freq_hz)) or z_abs <= 0 or freq_hz <= 0:
        out["Z"], out["theta"] = z_abs, theta_deg
        return out

    th = math.radians(theta_deg)
    w = 2.0 * math.pi * freq_hz
    rs = z_abs * math.cos(th)
    x = z_abs * math.sin(th)
    y = 1.0 / z_abs
    g = y * math.cos(th)
    b = -y * math.sin(th)

    out.update(
        Z=z_abs,
        theta=theta_deg,
        Rs=rs,
        X=x,
        Ls=x / w,
        Cs=_div(-1.0, w * x),
        Rp=_div(1.0, g),
        Lp=_div(-1.0, w * b),
        Cp=b / w,
        Y=y,
        G=g,
        B=b,
        D=abs(_div(rs, x)),
        Q=abs(_div(x, rs)),
    )
    return out


_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0,
       "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def parse_si(text: str) -> float:
    """Parse '1k', '2.5M', '100m', '10u', '1e3', '1 kHz' -> float. Raises ValueError."""
    import re

    s = text.strip().replace(" ", "")
    s = re.sub(r"(Hz|hz|HZ|V|v|A|Ω|ohm|F|H|s)$", "", s)
    m = re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([fpnuµmkKMG]?)", s)
    if not m:
        raise ValueError(f"Cannot read number {text!r}")
    return float(m.group(1)) * _SI[m.group(2)]


def si_format(value: float, unit: str = "", digits: int = 5) -> str:
    """Format a number with an SI prefix, e.g. 1.2345e-9 F -> '1.2345 nF'."""
    if value is None or isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "—"
    if value == 0:
        return f"0 {unit}".strip()
    prefixes = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}
    exp = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exp = max(-15, min(12, exp))
    scaled = value / 10**exp
    return f"{scaled:.{digits}g} {prefixes[exp]}{unit}".strip()
