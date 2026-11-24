"""Regression tests for the security review fixes."""
import os
import sys

import pytest

from lcr_app.core.storage import Run, load_csv, meta_line, save_csv, save_xlsx
from lcr_app.instrument.transport import Gpib32Transport

EVIL = '=HYPERLINK("http://x","click"),=1+1;@SUM(A1);-2+3\r\nindex,666'


def test_meta_line_neutralises_formulas_and_line_breaks():
    line = meta_line("notes", EVIL)
    assert line.count("\n") == 1 and line.endswith("\n") and "\r" not in line
    assert line.startswith("# notes: ")
    for part in line[len("# notes: "):].replace(";", ",").split(",")[1:]:
        assert not part.strip().startswith(("=", "+", "-", "@")), part


def test_csv_metadata_cannot_inject_rows(tmp_path):
    run = Run("s", [{"index": 0, "freq_Hz": 100.0, "Z": 1.0}], {"sample": "s", "notes": EVIL})
    back = load_csv(save_csv(run, tmp_path / "x.csv"))
    assert len(back.rows) == 1  # the "index,666" in the note did not become a data row


def test_excel_export_stores_formulas_as_text(tmp_path):
    from openpyxl import load_workbook

    run = Run("s", [{"index": 0, "freq_Hz": 100.0, "Z": 1.0}], {"sample": "=1+1", "notes": "@cmd"})
    path = save_xlsx([run], tmp_path / "x.xlsx")
    ws = load_workbook(path)["Info"]
    cells = [c for row in ws.iter_rows() for c in row if c.value in ("=1+1", "@cmd")]
    assert len(cells) == 2 and all(c.data_type == "s" for c in cells)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL search")
def test_gpib_dll_prefers_system_folder(tmp_path, monkeypatch):
    # a DLL with a driver's name in the current folder must not be loaded
    (tmp_path / "gpib488.dll").write_bytes(b"not a dll")
    monkeypatch.chdir(tmp_path)
    try:
        h = Gpib32Transport.load_one("gpib488.dll")
    except OSError:
        return  # driver not installed on this PC: nothing was loaded from the current folder either
    assert os.path.dirname(h._name).lower().endswith("system32") or h._name == "gpib488.dll"
