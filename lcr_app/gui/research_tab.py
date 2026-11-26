"""Research & Analysis tab: sample description, data analysis, AI literature comparison, report."""
from __future__ import annotations

import json
import math
import re
import threading
import traceback
import webbrowser
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QByteArray, QEvent, QObject, QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..core.params import parse_si
from ..core.storage import Run, load_csv, safe_filename, save_csv
from ..research.dielectric import analyze_run, summary_text, temperature_series
from ..research.importer import import_file
from ..research.llm import GET_KEY_URL, GeminiLLM, LLMCancelled, LLMError, get_secret
from ..research.sample import ALL_FIELDS, SAMPLE_TYPES, SCHEMA, TYPE_FROM_LABEL, SampleInfo
from ..research.sources import SourceError, pubchem_lookup
from .diagrams import svg_for
from .research_dialogs import AISettingsDialog, ApproveDialog, ImportDialog

PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000"]
PLOTS = [  # label, array key, log x, log y
    ("ε′ (real permittivity)", "eps1", True, True), ("ε″ (imaginary permittivity)", "eps2", True, True),
    ("tan δ", "tan_d", True, True), ("σ′ ac conductivity (S/m)", "sigma", True, True),
    ("M″ electric modulus", "M2", True, False), ("Cp (F)", "Cp", True, True), ("D", "D", True, True),
    ("|Z| (Ω)", "Z", True, True), ("θ (°)", "theta", True, False), ("Cole–Cole: ε″ vs ε′", "colecole", False, False),
]


# ================================================================= threading
class TaskThread(QThread):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.error.emit(f"{e}\n\n{traceback.format_exc(limit=4)}")


class AgentIO(QObject):
    """UserIO for the agent: called from the worker thread, answered in the GUI thread."""

    status_sig = Signal(str)
    log_sig = Signal(str, str)
    ask_sig = Signal(str)
    approve_sig = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._event = threading.Event()
        self._answer = None
        self.cancelled = False

    def _check(self):
        if self.cancelled:
            raise LLMCancelled("Stopped by the user")

    def status(self, msg: str) -> None:
        self._check()
        self.status_sig.emit(msg)

    def log(self, kind: str, msg: str) -> None:
        self.log_sig.emit(kind, msg)

    def _wait(self, sig, *args):
        self._check()
        self._event.clear()
        sig.emit(*args)
        while not self._event.wait(0.2):
            if self.cancelled:
                raise LLMCancelled("Stopped by the user")
        return self._answer

    def ask(self, question: str) -> str:
        return self._wait(self.ask_sig, question) or ""

    def approve(self, title: str, text: str) -> bool:
        return bool(self._wait(self.approve_sig, title, text))

    def reply(self, value) -> None:
        self._answer = value
        self._event.set()


# ============================================================== sample form
def _fmt_input(v, unit: str) -> str:
    if v in (None, ""):
        return ""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if unit in ("m", "F") and v != 0:
        prefixes = {-15: "f", -12: "p", -9: "n", -6: "u", -3: "m", 0: ""}
        exp = max(-15, min(0, int(math.floor(math.log10(abs(v)) / 3) * 3)))
        return f"{v / 10 ** exp:.6g}{prefixes[exp]}"
    return f"{v:.6g}"


def _parse_field(text: str, unit: str) -> float:
    """Number with SI prefix ('120n', '2m', '1.5u') or with an explicit length unit ('2 mm', '150 nm')."""
    t = text.strip().replace("µ", "u").replace(",", ".")
    if unit == "m":
        m = re.fullmatch(r"([-+]?[\d.]+(?:[eE][-+]?\d+)?)\s*(mm|um|nm|cm)", t)
        if m:
            return float(m.group(1)) * {"mm": 1e-3, "um": 1e-6, "nm": 1e-9, "cm": 1e-2}[m.group(2)]
    if unit == "m²":
        m = re.fullmatch(r"([-+]?[\d.]+(?:[eE][-+]?\d+)?)\s*(mm2|mm²|cm2|cm²|um2|um²)", t)
        if m:
            return float(m.group(1)) * {"mm": 1e-6, "cm": 1e-4, "um": 1e-12}[m.group(2)[:2]]
    return parse_si(t)


class SampleForm(QWidget):
    changed = Signal()
    highlight = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.widgets: dict[str, QWidget] = {}
        self.rows: dict[str, tuple[QFormLayout, QWidget]] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.lookup_btn = QPushButton("Look up in PubChem")
        self.lookup_btn.setToolTip("Fills formula, CAS, SMILES and synonyms from PubChem (free).")
        for sec in SCHEMA:
            g = QGroupBox(sec.title)
            gl = QVBoxLayout(g)
            if sec.diagram:
                self.diagram = QSvgWidget()
                self.diagram.setFixedSize(361, 209)
                gl.addWidget(self.diagram, 0, Qt.AlignHCenter)
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.WrapAllRows)  # label above field: fits a narrow pane
            gl.addLayout(form)
            for f in sec.fields:
                w = self._make(f)
                lbl = f.label + (f" [{_unit_label(f.unit)}]" if f.unit and f.kind == "number" else "")
                form.addRow(lbl + ":", w)
                self.rows[f.key] = (form, w)
            if sec.title == "Material":
                gl.addWidget(self.lookup_btn)
            lay.addWidget(g)
        self.update_visibility()

    def _make(self, f) -> QWidget:
        if f.kind == "choice":
            w = QComboBox()
            w.addItem("")
            w.addItems(f.choices)
            w.currentIndexChanged.connect(self._on_change)
            if f.key == "sample_type":
                w.setCurrentIndex(1)
                w.currentIndexChanged.connect(self.update_visibility)
        elif f.kind == "multiline":
            w = QPlainTextEdit()
            w.setMaximumHeight(60)
            w.setPlaceholderText(f.placeholder)
            w.textChanged.connect(self._on_change)
        else:
            w = QLineEdit()
            w.setPlaceholderText(f.placeholder or ("e.g. 2m = 2 mm, 150n = 150 nm" if f.unit == "m" else ""))
            w.editingFinished.connect(self._on_change)
            if f.kind == "number":
                w.editingFinished.connect(lambda w=w, f=f: self._validate(w, f))
        tip = f.help + ("\nNumbers accept SI prefixes: p n u m k M (e.g. 120n, 2m, 1.5u)." if f.kind == "number" else "")
        w.setToolTip(tip.strip())
        w.installEventFilter(self)
        w.setProperty("hl", f.highlight)
        self.widgets[f.key] = w
        return w

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.FocusIn:
            self.highlight.emit(obj.property("hl") or "")
            self._draw(obj.property("hl") or "")
        return False

    def _validate(self, w: QLineEdit, f) -> None:
        t = w.text().strip()
        ok = True
        if t:
            try:
                _parse_field(t, f.unit)
            except (ValueError, KeyError):
                ok = False
        w.setStyleSheet("" if ok else "border: 2px solid #d00;")
        if not ok:
            w.setToolTip(f"Cannot read '{t}'. Use a number with an optional SI prefix, e.g. 2m (=2 mm), 120n, 1.5u.")

    def _on_change(self, *_):
        self.changed.emit()

    def sample_type(self) -> str:
        return TYPE_FROM_LABEL.get(self.widgets["sample_type"].currentText(), "unknown")

    def _draw(self, hl: str = "") -> None:
        if hasattr(self, "diagram"):
            self.diagram.load(QByteArray(svg_for(self.sample_type(), hl).encode("utf-8")))

    def update_visibility(self) -> None:
        t = self.sample_type()
        for key, (form, w) in self.rows.items():
            f = ALL_FIELDS[key]
            form.setRowVisible(w, not f.types or t in f.types)
        self._draw()

    def values(self) -> dict:
        out = {}
        for key, w in self.widgets.items():
            f = ALL_FIELDS[key]
            if isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, QPlainTextEdit):
                out[key] = w.toPlainText().strip()
            else:
                t = w.text().strip()
                if f.kind == "number" and t:
                    try:
                        out[key] = _parse_field(t, f.unit)
                    except (ValueError, KeyError):
                        out[key] = None
                else:
                    out[key] = t
        return out

    def set_values(self, vals: dict) -> None:
        for key, w in self.widgets.items():
            v = vals.get(key, "")
            f = ALL_FIELDS[key]
            w.blockSignals(True)
            if isinstance(w, QComboBox):
                i = w.findText(str(v or ""))
                if key == "sample_type" and i <= 0:
                    i = 1
                w.setCurrentIndex(max(i, 0))
            elif isinstance(w, QPlainTextEdit):
                w.setPlainText(str(v or ""))
            else:
                w.setText(_fmt_input(v, f.unit) if f.kind == "number" else str(v or ""))
            w.blockSignals(False)
        self.update_visibility()


def _unit_label(u: str) -> str:
    return {"m": "m, e.g. 2m = 2 mm", "F": "F, e.g. 0.5p"}.get(u, u)


# ================================================================ main tab
class ResearchTab(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        self.qs = QSettings("LCRSweep", "LCRSweep")
        self.sample = SampleInfo()
        self.runs: list[Run] = []
        self.results: list[dict] = []
        self.project_dir: Path | None = None
        self.thread: QThread | None = None
        self.io: AgentIO | None = None
        self._session_key = ""
        self._dirty = False

        sp = QSplitter(Qt.Horizontal)
        sp.addWidget(self._build_sample_pane())
        sp.addWidget(self._build_data_pane())
        sp.addWidget(self._build_ai_pane())
        sp.setSizes([400, 640, 540])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(sp)
        last = self.qs.value("research/last_project", "")
        if last and (Path(last) / "sample_info.json").exists():
            try:
                self.open_project(Path(last))
            except Exception:
                pass
        self._update_completeness()
        self._update_key_status()

    # ------------------------------------------------------------- panes
    def _build_sample_pane(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("Project")
        gl = QVBoxLayout(g)
        self.proj_lbl = QLabel("<i>new project (not saved yet)</i>")
        self.proj_lbl.setWordWrap(True)
        self.proj_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        gl.addWidget(self.proj_lbl)
        h = QHBoxLayout()
        for text, slot in (("New", self.new_project), ("Open…", self.open_project_dialog), ("Save", self.save_project)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            h.addWidget(b)
        gl.addLayout(h)
        lay.addWidget(g)
        self.complete = QProgressBar()
        self.complete.setFormat("Sample description %p% complete")
        lay.addWidget(self.complete)
        self.missing = QLabel()
        self.missing.setWordWrap(True)
        self.missing.setStyleSheet("color:#b35900")
        lay.addWidget(self.missing)
        self.form = SampleForm()
        self.form.changed.connect(self._form_changed)
        self.form.lookup_btn.clicked.connect(self.pubchem)
        scroll = QScrollArea()
        scroll.setWidget(self.form)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(scroll, 1)
        w.setMinimumWidth(420)
        return w

    def _build_data_pane(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("Data (tick the runs to analyse; temperature can be edited)")
        gl = QVBoxLayout(g)
        self.run_table = QTableWidget(0, 6)
        self.run_table.setHorizontalHeaderLabels(["Use", "Run", "Points", "Frequency range", "T (°C)", "Source"])
        self.run_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.run_table.horizontalHeader().setStretchLastSection(True)
        self.run_table.setMaximumHeight(170)
        self.run_table.itemChanged.connect(self._run_item_changed)
        gl.addWidget(self.run_table)
        h = QHBoxLayout()
        self.btn_from_meas = QPushButton("Add from Measurement ▾")
        self.btn_from_meas.clicked.connect(self._menu_from_measurement)
        b2 = QPushButton("Load CSV (this app)…")
        b2.clicked.connect(self.load_csvs)
        b3 = QPushButton("Import other file…")
        b3.setToolTip("Data from any instrument / software: map the columns (Cp & D, |Z| & θ, Z′ & Z″, ε′ & ε″ …)")
        b3.clicked.connect(self.import_other)
        b4 = QPushButton("Remove")
        b4.clicked.connect(self.remove_run)
        for b in (self.btn_from_meas, b2, b3, b4):
            h.addWidget(b)
        gl.addLayout(h)
        lay.addWidget(g)

        h = QHBoxLayout()
        self.btn_analyze = QPushButton("① Analyse data (local, free)")
        self.btn_analyze.setMinimumHeight(32)
        self.btn_analyze.clicked.connect(self.analyze)
        self.btn_local_report = QPushButton("Report without AI")
        self.btn_local_report.clicked.connect(lambda: self.make_report(ai=False))
        h.addWidget(self.btn_analyze, 2)
        h.addWidget(self.btn_local_report, 1)
        lay.addLayout(h)

        ph = QHBoxLayout()
        ph.addWidget(QLabel("Plot:"))
        self.plot_sel = QComboBox()
        for label, *_ in PLOTS:
            self.plot_sel.addItem(label)
        self.plot_sel.currentIndexChanged.connect(self.redraw)
        ph.addWidget(self.plot_sel, 1)
        self.show_fit = QCheckBox("show fits")
        self.show_fit.setChecked(True)
        self.show_fit.toggled.connect(self.redraw)
        ph.addWidget(self.show_fit)
        lay.addLayout(ph)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend()
        for ax in ("left", "bottom"):
            self.plot.getAxis(ax).enableAutoSIPrefix(False)
        lay.addWidget(self.plot, 2)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setPlaceholderText("Analysis results (permittivity, fits, data-quality checks) appear here.")
        lay.addWidget(self.summary, 1)
        return w

    def _build_ai_pane(self) -> QWidget:
        tabs = QTabWidget()
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("AI research assistant (Google Gemini, free)")
        gl = QVBoxLayout(g)
        self.key_lbl = QLabel()
        self.key_lbl.setWordWrap(True)
        gl.addWidget(self.key_lbl)
        h = QHBoxLayout()
        b = QPushButton("AI settings…")
        b.clicked.connect(self.ai_settings)
        k = QPushButton("Get free key")
        k.clicked.connect(lambda: webbrowser.open(GET_KEY_URL))
        h.addWidget(b)
        h.addWidget(k)
        gl.addLayout(h)
        self.refresh = QCheckBox("Search the literature again (ignore cached papers/summary)")
        gl.addWidget(self.refresh)
        self.btn_ai = QPushButton("② Research literature && write report 🔬")
        self.btn_ai.setMinimumHeight(36)
        self.btn_ai.clicked.connect(lambda: self.make_report(ai=True))
        gl.addWidget(self.btn_ai)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_agent)
        gl.addWidget(self.btn_stop)
        lay.addWidget(g)
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        lay.addWidget(self.status_lbl)

        self.q_box = QGroupBox("The AI asks you")
        self.q_box.setStyleSheet("QGroupBox{border:2px solid #E69F00;margin-top:8px}")
        ql = QVBoxLayout(self.q_box)
        self.q_lbl = QLabel()
        self.q_lbl.setWordWrap(True)
        ql.addWidget(self.q_lbl)
        self.q_ans = QLineEdit()
        self.q_ans.setPlaceholderText("Your answer (it is saved in the sample file)")
        self.q_ans.returnPressed.connect(self._answer)
        ql.addWidget(self.q_ans)
        qh = QHBoxLayout()
        a = QPushButton("Answer")
        a.clicked.connect(self._answer)
        s = QPushButton("I don't know / skip")
        s.clicked.connect(lambda: self._answer(skip=True))
        qh.addWidget(a)
        qh.addWidget(s)
        ql.addLayout(qh)
        self.q_box.hide()
        lay.addWidget(self.q_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        lay.addWidget(self.log, 1)
        tabs.addTab(w, "AI research")

        rw = QWidget()
        rl = QVBoxLayout(rw)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setPlaceholderText("The report appears here after ① Analyse and ② Research (or 'Report "
                                        "without AI').")
        rl.addWidget(self.browser, 1)
        h = QHBoxLayout()
        for text, ext in (("Open in browser", "html"), ("Open Word file", "docx"), ("Open folder", "")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, e=ext: self.open_output(e))
            h.addWidget(b)
        rl.addLayout(h)
        tabs.addTab(rw, "Report")
        self.tabs = tabs
        return tabs

    # ----------------------------------------------------------- project
    def data_root(self) -> Path:
        return Path(self.main.out_dir.text())

    def knowledge_root(self) -> Path:
        return self.data_root() / "knowledge"

    def _sync_sample(self) -> None:
        self.sample.values = self.form.values()

    def _form_changed(self) -> None:
        self._dirty = True
        self._sync_sample()
        self._update_completeness()

    def _update_completeness(self) -> None:
        self._sync_sample()
        frac, missing = self.sample.completeness()
        self.complete.setValue(int(round(frac * 100)))
        self.missing.setText(("Needed for permittivity: " + "; ".join(missing)) if missing else "")

    def new_project(self) -> None:
        if self.thread:
            return
        self.sample = SampleInfo()
        self.form.set_values({})
        self.runs.clear()
        self.results.clear()
        self.project_dir = None
        self.proj_lbl.setText("<i>new project (not saved yet)</i>")
        self._fill_runs()
        self.redraw()
        self.summary.clear()
        self.browser.clear()
        self._update_completeness()

    def ensure_project_dir(self) -> Path:
        if self.project_dir:
            return self.project_dir
        name = self.sample.name or "sample"
        base = self.data_root() / "research" / safe_filename(name)
        d, k = base, 2
        while d.exists() and (d / "sample_info.json").exists():
            d = Path(f"{base}_{k}")
            k += 1
        d.mkdir(parents=True, exist_ok=True)
        self.project_dir = d
        self.proj_lbl.setText(str(d))
        self.qs.setValue("research/last_project", str(d))
        return d

    def save_project(self) -> None:
        self._sync_sample()
        d = self.ensure_project_dir()
        self.sample.save(d / "sample_info.json")
        entries = []
        for i, r in enumerate(self.runs):
            if not r.path or r.meta.get("imported_from") or not Path(r.path).exists():
                p = d / "data" / f"{safe_filename(r.name)}.csv"
                save_csv(r, p)
                r.path = str(p)
            entries.append({"path": r.path, "name": r.name, "temperature_c": r.meta.get("temperature_c"),
                            "use": self._run_used(i)})
        (d / "project.json").write_text(json.dumps({"runs": entries}, indent=1), encoding="utf-8")
        self._dirty = False
        self.main.statusBar().showMessage(f"Project saved: {d}", 5000)

    def open_project_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Open research project (folder with sample_info.json)",
                                             str(self.data_root() / "research"))
        if d:
            try:
                self.open_project(Path(d))
            except Exception as e:
                QMessageBox.warning(self, "Open project", str(e))

    def open_project(self, d: Path) -> None:
        if not (d / "sample_info.json").exists():
            raise FileNotFoundError("This folder has no sample_info.json")
        self.sample = SampleInfo.load(d / "sample_info.json")
        self.form.set_values(self.sample.values)
        self.project_dir = d
        self.proj_lbl.setText(str(d))
        self.runs.clear()
        uses = []
        pj = d / "project.json"
        if pj.exists():
            for e in json.loads(pj.read_text(encoding="utf-8")).get("runs", []):
                try:
                    r = load_csv(e["path"])
                except OSError:
                    self._log("warn", f"Data file missing: {e['path']}")
                    continue
                r.name = e.get("name", r.name)
                if e.get("temperature_c") is not None:
                    r.meta["temperature_c"] = e["temperature_c"]
                self.runs.append(r)
                uses.append(e.get("use", True))
        self._fill_runs(uses)
        rep = d / "report.html"
        if rep.exists():
            self.browser.setSource(QUrl.fromLocalFile(str(rep)))
        self.qs.setValue("research/last_project", str(d))
        self._update_completeness()

    # -------------------------------------------------------------- runs
    def _fill_runs(self, uses: list[bool] | None = None) -> None:
        t = self.run_table
        t.blockSignals(True)
        t.setRowCount(0)
        for i, r in enumerate(self.runs):
            t.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if (uses[i] if uses and i < len(uses) else True) else Qt.Unchecked)
            t.setItem(i, 0, chk)
            f = r.column("freq_Hz")
            fs = [x for x in f if x == x]
            rng = f"{min(fs):g} – {max(fs):g} Hz" if fs else "—"
            temp = r.meta.get("temperature_c", r.meta.get("temperature", ""))
            for j, txt in enumerate([r.name, str(len(r.rows)), rng, str(temp or ""),
                                     Path(r.path).name if r.path else "measured (unsaved)"], 1):
                it = QTableWidgetItem(txt)
                if j != 4:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                t.setItem(i, j, it)
        t.blockSignals(False)

    def _run_used(self, i: int) -> bool:
        it = self.run_table.item(i, 0)
        return bool(it and it.checkState() == Qt.Checked)

    def _run_item_changed(self, it: QTableWidgetItem) -> None:
        if it.column() == 4:
            txt = it.text().replace("°C", "").strip()
            try:
                self.runs[it.row()].meta["temperature_c"] = float(txt) if txt else None
            except ValueError:
                QMessageBox.warning(self, "Temperature", "Enter the temperature in °C as a number.")
        self._dirty = True

    def add_run(self, run: Run) -> None:
        names = {r.name for r in self.runs}
        base, k = run.name, 2
        while run.name in names:
            run.name = f"{base} ({k})"
            k += 1
        self.runs.append(run)
        uses = [self._run_used(i) for i in range(self.run_table.rowCount())] + [True]
        self._fill_runs(uses)
        self._dirty = True

    def _menu_from_measurement(self) -> None:
        m = QMenu(self)
        if not self.main.runs:
            m.addAction("(no runs in the Measurement tab yet)").setEnabled(False)
        for r in self.main.runs:
            a = m.addAction(f"{r.name}  ({len(r.rows)} points)")
            a.triggered.connect(lambda _=False, r=r: self.add_run(Run(r.name, list(r.rows), dict(r.meta), r.path)))
        m.exec(self.btn_from_meas.mapToGlobal(self.btn_from_meas.rect().bottomLeft()))

    def load_csvs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Load LCR Sweep CSV files", str(self.data_root()),
                                                "CSV files (*.csv)")
        for p in paths:
            try:
                self.add_run(load_csv(p))
            except Exception as e:
                QMessageBox.warning(self, "Load", f"{p}\n{e}\n\nFor files from other software use "
                                                  "'Import other file…'.")

    def import_other(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Import data file", str(self.data_root()),
                                           "Data files (*.csv *.txt *.dat *.tsv);;All files (*)")
        if not p:
            return
        self._sync_sample()
        dlg = ImportDialog(p, self.sample.geometric_capacitance(), self)
        if dlg.exec():
            try:
                run = import_file(p, dlg.spec(), dlg.name.text().strip() or None)
            except Exception as e:
                QMessageBox.warning(self, "Import", str(e))
                return
            self.add_run(run)
            if run.meta.get("rows_skipped"):
                self._log("info", f"Import: {run.meta['rows_skipped']} non-numeric rows were skipped.")

    def remove_run(self) -> None:
        i = self.run_table.currentRow()
        if 0 <= i < len(self.runs) and not self.thread:
            uses = [self._run_used(k) for k in range(len(self.runs)) if k != i]
            self.runs.pop(i)
            self._fill_runs(uses)

    def selected_runs(self) -> list[Run]:
        return [r for i, r in enumerate(self.runs) if self._run_used(i)]

    # ---------------------------------------------------------- PubChem
    def pubchem(self) -> None:
        self._sync_sample()
        v = self.sample.values
        if not (v.get("material_name") or v.get("cas") or v.get("smiles")):
            QMessageBox.information(self, "PubChem", "Enter a name, CAS number or SMILES first.")
            return
        self.form.lookup_btn.setEnabled(False)
        self.form.lookup_btn.setText("Looking up …")

        def done(mol):
            self.form.lookup_btn.setEnabled(True)
            self.form.lookup_btn.setText("Look up in PubChem")
            self.sample.pubchem = mol
            vals = self.form.values()
            vals["formula"] = vals.get("formula") or mol.get("MolecularFormula", "")
            vals["cas"] = vals.get("cas") or mol.get("cas", "")
            vals["smiles"] = vals.get("smiles") or mol.get("SMILES", "")
            if not vals.get("synonyms"):
                vals["synonyms"] = ", ".join(s for s in mol.get("synonyms", [])[:6] if len(s) < 40)
            self.form.set_values(vals)
            self._form_changed()
            self._log("source", f"PubChem CID {mol.get('cid')}: {mol.get('MolecularFormula')} "
                                f"{mol.get('IUPACName', '')}")
            QMessageBox.information(self, "PubChem", f"Found CID {mol.get('cid')}: {mol.get('MolecularFormula')}\n"
                                                     f"{mol.get('IUPACName', '')}")

        def err(e):
            self.form.lookup_btn.setEnabled(True)
            self.form.lookup_btn.setText("Look up in PubChem")
            QMessageBox.information(self, "PubChem", e.split("\n\n")[0] + "\n\nThat is fine for polymers and "
                                    "blends – the literature search uses the name and synonyms.")

        self._start(TaskThread(lambda: pubchem_lookup(v.get("material_name", ""), str(v.get("cas") or ""),
                                                      str(v.get("smiles") or ""))), done, err)

    # --------------------------------------------------------- analysis
    def _start(self, th: TaskThread, done, err) -> None:
        def fin_done(x):
            self.thread = None
            done(x)

        def fin_err(e):
            self.thread = None
            err(e)

        th.done.connect(fin_done)
        th.error.connect(fin_err)
        self._keep = th  # keep a reference
        self.thread = th
        th.start()

    def analyze(self, then=None) -> None:
        if self.thread:
            return
        runs = self.selected_runs()
        if not runs:
            QMessageBox.information(self, "Analyse", "Add data first (and tick 'Use').")
            return
        self._sync_sample()
        cache = (self.project_dir or self.ensure_project_dir()) / ".cache"
        sample = SampleInfo(values=dict(self.sample.values))
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.setText("Analysing …")

        def work():
            return [analyze_run(r, sample, cache) for r in runs]

        def done(res):
            self.btn_analyze.setEnabled(True)
            self.btn_analyze.setText("① Analyse data (local, free)")
            self.results = res
            txt = "\n\n".join(summary_text(r) for r in res)
            ts = temperature_series(res)
            for k, v in ts.items():
                txt += f"\n\nArrhenius ({k}): Ea = {v['Ea_eV']:.3f} eV, R² = {v['r2']:.3f}"
            self.summary.setPlainText(txt)
            if not any(r.get("has_permittivity") for r in res):
                self.plot_sel.setCurrentIndex(5)
            self.redraw()
            if then:
                then()

        def err(e):
            self.btn_analyze.setEnabled(True)
            self.btn_analyze.setText("① Analyse data (local, free)")
            QMessageBox.critical(self, "Analysis failed", e)

        self._start(TaskThread(work), done, err)

    def redraw(self) -> None:
        label, key, lx, ly = PLOTS[self.plot_sel.currentIndex()]
        p = self.plot
        p.clear()
        p.setLogMode(x=lx, y=ly)
        p.setLabel("bottom", "ε′" if key == "colecole" else "Frequency (Hz)")
        p.setLabel("left", "ε″" if key == "colecole" else label)
        for i, r in enumerate(self.results):
            a = r.get("arrays", {})
            c = PALETTE[i % len(PALETTE)]
            if key == "colecole":
                if "eps1" not in a:
                    continue
                x, y = _np(a["eps1"]), _np(a["eps2"])
            else:
                if key not in a:
                    continue
                x, y = _np(a["f"]), _np(a[key])
            ok = np.isfinite(x) & np.isfinite(y)
            if ly:
                ok &= y > 0
            if lx:
                ok &= x > 0
            p.plot(x[ok], y[ok], pen=None, symbol="o", symbolSize=5, symbolBrush=c, symbolPen=None, name=r["run"])
            if not self.show_fit.isChecked():
                continue
            rf = r.get("relaxation_fit")
            if rf and rf.get("relaxation_in_window") and key in ("eps1", "eps2", "colecole"):
                fx = _np(rf["eps1_fit"]) if key == "colecole" else _np(rf["f_fit"])
                fy = _np(rf["eps2_fit"] if key in ("eps2", "colecole") else rf["eps1_fit"])
                p.plot(fx, fy, pen=pg.mkPen(c, width=1.5), name=f"{rf['model']} fit")
            jf = r.get("jonscher_fit")
            if key == "sigma" and jf and jf.get("good_fit"):
                p.plot(_np(jf["f_fit"]), _np(jf["sigma_fit"]), pen=pg.mkPen(c, width=1.5, style=Qt.DashLine),
                       name=f"Jonscher n={jf['n']:.2f}")

    # ------------------------------------------------------------ report
    def _settings(self):
        from ..research.agent import ResearchSettings

        q = self.qs
        return ResearchSettings(
            model=q.value("ai/model", ""), max_queries=q.value("ai/max_queries", 5, type=int),
            max_extract_papers=q.value("ai/max_papers", 20, type=int),
            max_agent_steps=q.value("ai/max_steps", 8, type=int),
            max_questions=q.value("ai/max_questions", 3, type=int),
            use_openalex=q.value("ai/use_openalex", True, type=bool),
            use_semantic_scholar=q.value("ai/use_s2", True, type=bool),
            openalex_email=q.value("ai/openalex_email", ""), openalex_api_key=get_secret("openalex_api_key"),
            s2_api_key=get_secret("s2_api_key"), force_refresh=self.refresh.isChecked())

    def _runs_meta(self, runs: list[Run]) -> list[dict]:
        out = []
        for r in runs:
            m = r.meta
            lvl = f"{m.get('level_mode', '')} {m.get('level_value', '')}".strip()
            out.append({"name": r.name, "instrument": m.get("instrument") or m.get("import_kind") or "—",
                        "level": lvl or "—", "speed": m.get("speed", "—"), "averaging": m.get("averaging", "—"),
                        "source": Path(r.path).name if r.path else "—"})
        return out

    def make_report(self, ai: bool, analyzed: bool = False) -> None:
        if self.thread:
            return
        self._sync_sample()
        if ai and not self.sample.name:
            QMessageBox.information(self, "Research", "Enter the material / molecule name first.")
            return
        if ai and not (get_secret("gemini_api_key") or self._session_key):
            if QMessageBox.question(self, "Gemini key", "No Gemini API key yet. Open AI settings to add the "
                                                        "free key now?") == QMessageBox.Yes:
                self.ai_settings()
            return
        if not analyzed:  # always (re)analyse first – cached, so this is fast when nothing changed
            self.analyze(then=lambda: self.make_report(ai, True))
            return
        self.save_project()
        runs = self.selected_runs()
        sample = self.sample
        proj = self.project_dir
        results = self.results
        runs_meta = self._runs_meta(runs)

        if not ai:
            from ..research.report import build_report

            self.status_lbl.setText("Writing report …")
            self._start(TaskThread(lambda: build_report(proj, sample, results, None, runs_meta)),
                        self._report_done, self._report_err)
            return

        from ..research.agent import ResearchAgent
        from ..research.report import build_report

        settings = self._settings()
        self.io = io = AgentIO()
        io.status_sig.connect(lambda s: (self.status_lbl.setText(s), self._log("step", s)))
        io.log_sig.connect(self._log)
        io.ask_sig.connect(self._ask)
        io.approve_sig.connect(self._approve)
        approve_each = self.qs.value("ai/approve_each", False, type=bool)
        try:
            llm = GeminiLLM(get_secret("gemini_api_key") or self._session_key, settings.model,
                            status=lambda s: io.status_sig.emit(s),
                            before_send=(lambda task, txt: io.approve(f"Send request: {task}", txt))
                            if approve_each else None)
        except LLMError as e:
            QMessageBox.warning(self, "Gemini", str(e))
            return

        def work():
            agent = ResearchAgent(llm, sample, results, proj, self.knowledge_root(), settings, io,
                                  save_sample=lambda: sample.save(proj / "sample_info.json"))
            res = agent.run()
            io.status("Building the report files …")
            return build_report(proj, sample, results, res, runs_meta)

        self.btn_ai.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.tabs.setCurrentIndex(0)
        self._log("step", "=== Research started ===")
        self._start(TaskThread(work), self._report_done, self._report_err)

    def _report_done(self, paths) -> None:
        self.btn_ai.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.q_box.hide()
        self.status_lbl.setText(f"Report written: {paths['html']}")
        self._log("step", f"Report: {paths['md'].parent}")
        self.browser.clearHistory()
        self.browser.setSource(QUrl())
        self.browser.setSource(QUrl.fromLocalFile(str(paths["html"])))
        self.tabs.setCurrentIndex(1)
        self.form.set_values(self.sample.values)  # answers to questions may have been added

    def _report_err(self, e: str) -> None:
        self.btn_ai.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.q_box.hide()
        first = e.split("\n\n")[0]
        self.status_lbl.setText(first)
        self._log("error", e if "Cancelled" not in e and "Stopped" not in e and "Not approved" not in e else first)
        if not any(s in e for s in ("Cancelled", "Stopped", "Not approved")):
            QMessageBox.warning(self, "Research", first)

    def stop_agent(self) -> None:
        if self.io:
            self.io.cancelled = True
            self.status_lbl.setText("Stopping …")

    def _ask(self, q: str) -> None:
        self.q_lbl.setText(q)
        self.q_ans.clear()
        self.q_box.show()
        self.q_ans.setFocus()
        QApplication_alert(self)

    def _answer(self, skip: bool = False) -> None:
        if self.io:
            ans = "" if skip else self.q_ans.text().strip()
            self._log("answer", ans or "(skipped)")
            self.q_box.hide()
            self.io.reply(ans)

    def _approve(self, title: str, text: str) -> None:
        ok = ApproveDialog(title, text, self).exec()
        self._log("step", "Approved – sending." if ok else "Not approved – stopped.")
        if self.io:
            self.io.reply(bool(ok))

    def _log(self, kind: str, msg: str) -> None:
        icon = {"search": "🔎", "source": "📚", "cache": "♻️", "tool": "🛠", "question": "❓", "answer": "💬",
                "warn": "⚠️", "check": "✔", "error": "❌", "step": "▶", "info": "ℹ️"}.get(kind, "•")
        self.log.appendPlainText(f"{icon} {msg}")

    def open_output(self, ext: str) -> None:
        if not self.project_dir:
            return
        target = self.project_dir / (f"report.{ext}" if ext else "")
        if ext and not target.exists():
            QMessageBox.information(self, "Report", "No report yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def ai_settings(self) -> None:
        dlg = AISettingsDialog(self.qs, self)
        dlg.exec()
        self._update_key_status()

    def _update_key_status(self) -> None:
        has = bool(get_secret("gemini_api_key") or self._session_key)
        model = self.qs.value("ai/model", "") or "automatic (newest flash)"
        self.key_lbl.setText(("✅ Gemini key saved. " if has else "⚠️ No Gemini key yet – click <b>Get free key</b>, "
                              "then paste it in <b>AI settings</b>. ") + f"Model: {model}.")
        self.key_lbl.setTextFormat(Qt.RichText)


def _np(v) -> np.ndarray:
    return np.asarray([math.nan if x is None else x for x in v], dtype=float)


def QApplication_alert(w: QWidget) -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.alert(w.window())
