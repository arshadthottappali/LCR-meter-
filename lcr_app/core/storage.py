"""Saving and loading measurement data.

CSV layout (opens directly in Excel / Origin / Python):
    # key: value            <- metadata lines (sample, settings, instrument ...)
    index,freq_set_Hz,...    <- header
    0,100,...                <- one row per frequency point
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .params import PARAM_UNIT
from .sweep import ROW_KEYS

_FORMULA_START = ("=", "+", "-", "@")


def meta_line(key: str, value) -> str:
    """One '# key: value' metadata line, safe to open in Excel.

    Line breaks are removed (they would start fake data rows), and any part after a
    separator (, ; tab) that starts like a formula gets a leading apostrophe, so
    spreadsheet programs show it as text instead of evaluating it (CSV injection).
    """
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    text = re.sub(r"[\r\n]+", " ", f"{key}: {value}")
    text = re.sub(r"([,;\t]\s*)([=+\-@])", r"\1'\2", text)
    return f"# {text}\n"


def _xl_append(ws, values: list) -> None:
    """ws.append() that stores formula-like strings as plain text (no formula injection)."""
    ws.append(values)
    for cell in ws[ws.max_row]:
        if isinstance(cell.value, str) and cell.value.startswith(_FORMULA_START):
            cell.data_type = "s"


@dataclass
class Run:
    name: str
    rows: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    path: str = ""

    def column(self, key: str) -> list[float]:
        return [float(r.get(key, math.nan)) for r in self.rows]


def header_label(key: str) -> str:
    unit = PARAM_UNIT.get(key)
    if key == "theta":
        return "theta_deg"
    if key == "Z_raw":
        return "Z_raw_ohm"
    if key == "theta_raw":
        return "theta_raw_deg"
    if unit:
        return f"{key}_{'ohm' if unit == 'Ω' else unit}"
    return key


def safe_filename(text: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", text).strip("_") or "run"


def default_filename(sample: str, operator: str = "", test_no: int | str | None = None) -> str:
    """YYYYMMDD_HHMMSS_<sample>_<operator>_T<test no>.csv (empty parts are left out)."""
    parts = [f"{datetime.now():%Y%m%d_%H%M%S}", safe_filename(sample)]
    if operator and operator.strip():
        parts.append(safe_filename(operator))
    if test_no not in (None, ""):
        parts.append(f"T{safe_filename(str(test_no))}")
    return "_".join(parts) + ".csv"


def save_csv(run: Run, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        for k, v in run.meta.items():
            fh.write(meta_line(k, v))
        w = csv.writer(fh)
        w.writerow([header_label(k) for k in ROW_KEYS])
        for r in run.rows:
            w.writerow([_fmt(r.get(k)) for k in ROW_KEYS])
    run.path = str(path)
    return path


def _fmt(v) -> str:
    if isinstance(v, float):
        return "" if math.isnan(v) else f"{v:.7g}"
    return "" if v is None else str(v)


class CsvAppender:
    """Writes each point to disk as soon as it is measured (crash-safe autosave)."""

    def __init__(self, path: str | Path, meta: dict) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w", newline="", encoding="utf-8")
        for k, v in meta.items():
            self.fh.write(meta_line(k, v))
        self.w = csv.writer(self.fh)
        self.w.writerow([header_label(k) for k in ROW_KEYS])
        self.fh.flush()

    def append(self, row: dict) -> None:
        self.w.writerow([_fmt(row.get(k)) for k in ROW_KEYS])
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def load_csv(path: str | Path) -> Run:
    path = Path(path)
    meta, lines = {}, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                k, _, v = line[1:].partition(":")
                meta[k.strip()] = v.strip()
            elif line.strip():
                lines.append(line)
    reader = csv.reader(io.StringIO("".join(lines)))
    header = next(reader)
    label_to_key = {header_label(k): k for k in ROW_KEYS}
    keys = [label_to_key.get(h, h) for h in header]
    rows = []
    for rec in reader:
        row = {}
        for k, v in zip(keys, rec):
            if k == "timestamp":
                row[k] = v
            else:
                try:
                    row[k] = float(v) if v != "" else math.nan
                except ValueError:
                    row[k] = v
        rows.append(row)
    return Run(name=meta.get("sample", path.stem), rows=rows, meta=meta, path=str(path))


def save_xlsx(runs: list[Run], path: str | Path) -> Path:
    """One worksheet per run plus an 'Info' sheet with the metadata."""
    from openpyxl import Workbook

    wb = Workbook()
    info = wb.active
    info.title = "Info"
    info.append(["run", "key", "value"])
    used = set()
    for run in runs:
        title = safe_filename(run.name)[:28] or "run"
        base, n = title, 1
        while title in used:
            n += 1
            title = f"{base[:25]}_{n}"
        used.add(title)
        ws = wb.create_sheet(title)
        ws.append([header_label(k) for k in ROW_KEYS])
        for r in run.rows:
            _xl_append(ws, [None if isinstance(r.get(k), float) and math.isnan(r.get(k)) else r.get(k)
                            for k in ROW_KEYS])
        for k, v in run.meta.items():
            _xl_append(info, [title, k, json.dumps(v) if isinstance(v, (dict, list)) else str(v)])
    wb.save(path)
    return Path(path)
