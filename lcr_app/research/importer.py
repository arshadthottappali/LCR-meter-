"""Import impedance data from any CSV / text file (analysis-only use).

The user tells us which columns hold the frequency and which pair of
quantities was measured; everything is converted to |Z|, θ and then to all
other parameters, so imported data behaves exactly like data measured by
this app.
"""
from __future__ import annotations

import cmath
import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path

from ..core.params import derive
from ..core.storage import Run

# kind -> (label, name of column A, name of column B)
INPUT_KINDS = {
    "Cp_D": ("Cp and D (parallel capacitance, dissipation)", "Cp", "D"),
    "Cs_D": ("Cs and D (series capacitance, dissipation)", "Cs", "D"),
    "Cp_Rp": ("Cp and Rp (parallel capacitance, resistance)", "Cp", "Rp"),
    "Cp_G": ("Cp and G (parallel capacitance, conductance)", "Cp", "G"),
    "Z_deg": ("|Z| and θ in degrees", "|Z|", "θ (deg)"),
    "Z_rad": ("|Z| and θ in radians", "|Z|", "θ (rad)"),
    "ReIm": ("Z′ and Z″ (real, imaginary)", "Z′", "Z″"),
    "ReNegIm": ("Z′ and −Z″ (real, minus imaginary)", "Z′", "−Z″"),
    "eps": ("ε′ and ε″ (permittivity; needs C₀ from the sample geometry)", "ε′", "ε″"),
}
FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "rad/s (ω)": 1 / (2 * math.pi)}
CAP_UNITS = {"F": 1.0, "µF": 1e-6, "nF": 1e-9, "pF": 1e-12}
RES_UNITS = {"Ω": 1.0, "kΩ": 1e3, "MΩ": 1e6}


@dataclass
class ImportSpec:
    freq_col: int = 0
    a_col: int = 1
    b_col: int = 2
    kind: str = "Cp_D"
    freq_unit: str = "Hz"
    a_unit: str = "F"  # capacitance or resistance unit for column A (where applicable)
    b_unit: str = "Ω"  # resistance unit for column B (Rp) or 'S' scale for G
    skip_rows: int = 0
    delimiter: str = ","
    c0: float = math.nan  # for eps input


def sniff(path: str | Path, max_lines: int = 60) -> tuple[str, list[list[str]], int]:
    """Guess delimiter and return (delimiter, first rows, index of first numeric row)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines()[:max_lines]]
    sample = "\n".join(ln for ln in lines if ln.strip() and not ln.startswith("#"))
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t ").delimiter
    except csv.Error:
        delim = "\t" if "\t" in sample else ("," if "," in sample else ";")
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim, skipinitialspace=True))
    first_numeric = 0
    for i, r in enumerate(rows):
        if r and not r[0].startswith("#") and _is_number(r[0]):
            first_numeric = i
            break
    return delim, rows, first_numeric


def _is_number(s: str) -> bool:
    try:
        float(s.replace(",", ".") if s.count(",") == 1 and "." not in s else s)
        return True
    except ValueError:
        return False


def _f(s: str) -> float:
    s = s.strip()
    if s.count(",") == 1 and "." not in s:  # decimal comma
        s = s.replace(",", ".")
    return float(s)


def to_z(kind: str, f: float, a: float, b: float, c0: float = math.nan) -> complex:
    """Convert a measured pair to complex impedance at frequency f."""
    w = 2 * math.pi * f
    if kind in ("Cp_D", "Cp_Rp", "Cp_G"):
        g = {"Cp_D": lambda: b * w * a, "Cp_Rp": lambda: 1 / b, "Cp_G": lambda: b}[kind]()
        return 1 / complex(g, w * a)
    if kind == "Cs_D":
        x = -1 / (w * a)
        return complex(b * abs(x), x)
    if kind == "Z_deg":
        return cmath.rect(a, math.radians(b))
    if kind == "Z_rad":
        return cmath.rect(a, b)
    if kind == "ReIm":
        return complex(a, b)
    if kind == "ReNegIm":
        return complex(a, -b)
    if kind == "eps":
        if not c0 > 0:
            raise ValueError("ε input needs the geometric capacitance C₀ (fill in the sample geometry first)")
        # eps* = eps' - j eps''  ->  Y = j w C0 eps*
        y = 1j * w * c0 * complex(a, -b)
        return 1 / y
    raise ValueError(f"Unknown input kind {kind}")


def import_file(path: str | Path, spec: ImportSpec, name: str | None = None) -> Run:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=spec.delimiter, skipinitialspace=True)
    fu = FREQ_UNITS.get(spec.freq_unit, 1.0)
    au = {**CAP_UNITS, **RES_UNITS}.get(spec.a_unit, 1.0)
    bu = {**CAP_UNITS, **RES_UNITS}.get(spec.b_unit, 1.0)
    rows, skipped = [], 0
    for i, rec in enumerate(reader):
        if i < spec.skip_rows or not rec or rec[0].startswith("#"):
            continue
        try:
            f = _f(rec[spec.freq_col]) * fu
            a = _f(rec[spec.a_col])
            b = _f(rec[spec.b_col])
        except (ValueError, IndexError):
            skipped += 1
            continue
        if spec.kind in ("Cp_D", "Cs_D", "Cp_Rp", "Cp_G"):
            a *= au
        if spec.kind == "Cp_Rp":
            b *= bu
        if spec.kind in ("Z_deg", "Z_rad", "ReIm", "ReNegIm"):
            a *= au if spec.a_unit in RES_UNITS else 1.0
            if spec.kind in ("ReIm", "ReNegIm"):
                b *= au if spec.a_unit in RES_UNITS else 1.0
        try:
            z = to_z(spec.kind, f, a, b, spec.c0)
        except (ZeroDivisionError, ValueError) as e:
            if isinstance(e, ValueError) and "C₀" in str(e):
                raise
            skipped += 1
            continue
        vals = derive(abs(z), math.degrees(cmath.phase(z)), f)
        row = {"index": len(rows), "freq_set_Hz": f, "freq_Hz": f, **vals,
               "Z_std": math.nan, "theta_std": math.nan, "timestamp": ""}
        rows.append(row)
    if not rows:
        raise ValueError("No numeric rows could be read. Check the column numbers and the delimiter.")
    rows.sort(key=lambda r: r["freq_Hz"])
    for i, r in enumerate(rows):
        r["index"] = i
    meta = {"sample": name or Path(path).stem, "imported_from": str(path),
            "import_kind": INPUT_KINDS[spec.kind][0], "rows_skipped": skipped}
    return Run(name=name or Path(path).stem, rows=rows, meta=meta, path=str(path))
