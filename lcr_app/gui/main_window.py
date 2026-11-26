"""Main window of the LCR sweep application."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .. import __version__
from ..core.params import PARAM_KEYS, PARAM_LABEL, PARAM_UNIT, parse_si, si_format
from ..core.storage import CsvAppender, Run, default_filename, load_csv, save_csv, save_xlsx
from ..core.sweep import ROW_KEYS, SweepConfig, estimate_duration_s
from ..instrument.hioki3532 import PROFILE_PATH, Hioki3532, MeasureSettings, load_profile
from ..instrument.simulator import DUT_MODELS, SimulatedTransport
from ..instrument.transport import Gpib32Transport, TransportError, VisaTransport
from .worker import SweepWorker, TrafficBridge

BACKENDS = [
    ("visa", "VISA (Keithley KI-VISA / NI-VISA)"),
    ("gpib32", "GPIB-32 DLL (KUSB-488A driver)"),
    ("sim", "Simulator (no hardware)"),
]
# Colour-blind-safe series palette (Okabe-Ito)
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000", "#F0E442"]
LOG_DEFAULT = {"Z", "Y", "Rp", "Q", "D"}


def _num_item(v) -> QTableWidgetItem:
    if isinstance(v, float):
        txt = "" if math.isnan(v) else f"{v:.6g}"
    else:
        txt = str(v)
    it = QTableWidgetItem(txt)
    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return it


class PlotPanel(QWidget):
    """A pyqtgraph plot with a parameter selector and log-scale toggles."""

    def __init__(self, default_param: str, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.param = QComboBox()
        for k in PARAM_KEYS:
            unit = PARAM_UNIT[k]
            self.param.addItem(f"{PARAM_LABEL[k]}" + (f"  [{unit}]" if unit else ""), k)
        self.param.setCurrentIndex(PARAM_KEYS.index(default_param))
        self.logx = QCheckBox("log f")
        self.logx.setChecked(True)
        self.logy = QCheckBox("log y")
        self.absy = QCheckBox("|y|")
        self.absy.setToolTip("Plot absolute value (useful for log scale of negative quantities)")
        bar.addWidget(QLabel("Y axis:"))
        bar.addWidget(self.param, 1)
        bar.addWidget(self.logx)
        bar.addWidget(self.logy)
        bar.addWidget(self.absy)
        lay.addLayout(bar)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend(offset=(-10, 10))
        for ax in ("left", "bottom"):
            self.plot.getAxis(ax).enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "Frequency (Hz)")
        lay.addWidget(self.plot, 1)
        self.param.currentIndexChanged.connect(self._param_changed)
        self._param_changed()

    def _param_changed(self) -> None:
        k = self.param.currentData()
        self.logy.blockSignals(True)
        self.logy.setChecked(k in LOG_DEFAULT)
        self.absy.setChecked(False)
        self.logy.blockSignals(False)

    def key(self) -> str:
        return self.param.currentData()

    def redraw(self, runs: list[tuple[Run, str]]) -> None:
        k = self.key()
        self.plot.clear()
        self.plot.setLogMode(x=self.logx.isChecked(), y=self.logy.isChecked())
        unit = PARAM_UNIT[k]
        self.plot.setLabel("left", f"{PARAM_LABEL[k]} ({unit})" if unit else PARAM_LABEL[k])
        for run, color in runs:
            f = np.array(run.column("freq_Hz"))
            y = np.array(run.column(k))
            if self.absy.isChecked():
                y = np.abs(y)
            ok = np.isfinite(f) & np.isfinite(y)
            if self.logy.isChecked():
                ok &= y > 0
            if not ok.any():
                continue
            pen = pg.mkPen(color, width=2)
            self.plot.plot(f[ok], y[ok], pen=pen, symbol="o", symbolSize=5, symbolBrush=color,
                           symbolPen=None, name=run.name)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"LCR Sweep — Hioki 3532-50   v{__version__}")
        self._session_key = ""
        self.resize(1400, 860)
        self.settings = QSettings("LCRSweep", "LCRSweep")
        self.profile = load_profile()
        self.meter: Hioki3532 | None = None
        self.transport = None
        self.runs: list[Run] = []
        self.current: Run | None = None
        self.appender: CsvAppender | None = None
        self.thread: QThread | None = None
        self.worker: SweepWorker | None = None
        self.t_start = 0.0
        self.bridge = TrafficBridge()
        self.bridge.message.connect(self._log_traffic)

        self._build_menu()
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 1000])
        from .research_tab import ResearchTab

        self.top_tabs = QTabWidget()
        self.top_tabs.setDocumentMode(True)
        self.top_tabs.addTab(splitter, "📈  Measurement")
        self.research = ResearchTab(self)
        self.top_tabs.addTab(self.research, "🔬  Research && Analysis")
        self.setCentralWidget(self.top_tabs)
        self.statusBar().showMessage("Not connected")
        self._restore_settings()
        self._backend_changed()
        self._update_estimate()

    # ================================================================ UI build
    def _build_menu(self) -> None:
        m = self.menuBar()
        f = m.addMenu("&File")
        for text, slot, sc in [
            ("Open data file (CSV)…", self.open_csv, "Ctrl+O"),
            ("Save selected run as CSV…", self.save_selected_csv, "Ctrl+S"),
            ("Export all runs to Excel…", self.export_xlsx, None),
            ("Save plots as image…", self.save_plot_image, None),
            (None, None, None),
            ("Exit", self.close, "Ctrl+Q"),
        ]:
            if text is None:
                f.addSeparator()
                continue
            a = QAction(text, self)
            a.triggered.connect(slot)
            if sc:
                a.setShortcut(sc)
            f.addAction(a)
        t = m.addMenu("&Tools")
        a = QAction("Verify command profile on meter", self)
        a.triggered.connect(self.verify_profile)
        t.addAction(a)
        a = QAction("Open command profile (JSON)…", self)
        a.triggered.connect(self.open_profile)
        t.addAction(a)
        a = QAction("Reload command profile", self)
        a.triggered.connect(self.reload_profile)
        t.addAction(a)
        h = m.addMenu("&Help")
        a = QAction("About", self)
        a.triggered.connect(self.about)
        h.addAction(a)

    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # ---- connection
        g = QGroupBox("1. Connection")
        fl = QFormLayout(g)
        self.backend = QComboBox()
        for key, label in BACKENDS:
            self.backend.addItem(label, key)
        self.backend.currentIndexChanged.connect(self._backend_changed)
        fl.addRow("Interface:", self.backend)
        self.visa_res = QComboBox()
        self.visa_res.setEditable(True)
        self.visa_res.setEditText("GPIB0::1::INSTR")
        self.visa_row_lbl = QLabel("VISA resource:")
        fl.addRow(self.visa_row_lbl, self.visa_res)
        self.gpib_addr = QSpinBox()
        self.gpib_addr.setRange(0, 30)
        self.gpib_addr.setValue(1)
        self.gpib_board = QSpinBox()
        self.gpib_board.setRange(0, 9)
        ab = QHBoxLayout()
        ab.addWidget(QLabel("addr"))
        ab.addWidget(self.gpib_addr)
        ab.addWidget(QLabel("board"))
        ab.addWidget(self.gpib_board)
        self.addr_row = QWidget()
        self.addr_row.setLayout(ab)
        ab.setContentsMargins(0, 0, 0, 0)
        self.addr_lbl = QLabel("GPIB address:")
        fl.addRow(self.addr_lbl, self.addr_row)
        self.sim_dut = QComboBox()
        self.sim_dut.addItems(list(DUT_MODELS))
        self.sim_lbl = QLabel("Simulated DUT:")
        fl.addRow(self.sim_lbl, self.sim_dut)
        hb = QHBoxLayout()
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.clicked.connect(self.scan)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.toggle_connect)
        hb.addWidget(self.btn_scan)
        hb.addWidget(self.btn_connect)
        fl.addRow(hb)
        self.idn_lbl = QLabel("—")
        self.idn_lbl.setWordWrap(True)
        fl.addRow("Instrument:", self.idn_lbl)
        lay.addWidget(g)

        # ---- sweep
        g = QGroupBox("2. Frequency sweep")
        fl = QFormLayout(g)
        self.f_start = QLineEdit("100")
        self.f_stop = QLineEdit("1M")
        for e in (self.f_start, self.f_stop):
            e.setToolTip("Hz. SI prefixes allowed: 42, 120, 1k, 2.5k, 1M, 5M")
            e.editingFinished.connect(self._update_estimate)
        fl.addRow("Start freq (Hz):", self.f_start)
        fl.addRow("Stop freq (Hz):", self.f_stop)
        self.n_points = QSpinBox()
        self.n_points.setRange(1, 10001)
        self.n_points.setValue(51)
        self.n_points.valueChanged.connect(self._update_estimate)
        fl.addRow("Points:", self.n_points)
        self.spacing = QComboBox()
        self.spacing.addItem("Logarithmic", "log")
        self.spacing.addItem("Linear", "linear")
        self.spacing.addItem("Custom list", "list")
        self.spacing.currentIndexChanged.connect(self._spacing_changed)
        fl.addRow("Spacing:", self.spacing)
        self.custom = QPlainTextEdit()
        self.custom.setPlaceholderText("One frequency per line or comma separated, e.g.\n100, 120, 1k, 10k, 100k, 1M")
        self.custom.setMaximumHeight(70)
        self.custom.textChanged.connect(self._update_estimate)
        self.custom_lbl = QLabel("Frequencies:")
        fl.addRow(self.custom_lbl, self.custom)
        lay.addWidget(g)

        # ---- signal / measurement
        g = QGroupBox("3. Test signal && measurement")
        fl = QFormLayout(g)
        self.level_mode = QComboBox()
        for k, v in self.profile["level_modes"].items():
            self.level_mode.addItem(v, k)
        self.level_mode.currentIndexChanged.connect(self._level_mode_changed)
        fl.addRow("Level type:", self.level_mode)
        self.level_val = QLineEdit("1")
        self.level_unit = QLabel("V rms")
        lh = QHBoxLayout()
        lh.addWidget(self.level_val)
        lh.addWidget(self.level_unit)
        fl.addRow("Level:", lh)
        self.speed = QComboBox()
        for s in self.profile["speeds"]:
            self.speed.addItem(s.upper(), s)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(self._update_estimate)
        fl.addRow("Speed:", self.speed)
        self.avg = QSpinBox()
        self.avg.setRange(1, 256)
        self.avg.valueChanged.connect(self._update_estimate)
        fl.addRow("Averaging (meter):", self.avg)
        self.settle = QSpinBox()
        self.settle.setRange(0, 60000)
        self.settle.setValue(50)
        self.settle.setSuffix(" ms")
        self.settle.setToolTip("Wait after changing frequency before triggering the measurement")
        self.settle.valueChanged.connect(self._update_estimate)
        fl.addRow("Settling delay:", self.settle)
        self.reads = QSpinBox()
        self.reads.setRange(1, 100)
        self.reads.setToolTip("Readings per frequency. >1 stores mean and standard deviation of |Z| and θ")
        self.reads.valueChanged.connect(self._update_estimate)
        fl.addRow("Readings / point:", self.reads)
        self.use_trg = QCheckBox("Trigger each point (*TRG) — recommended")
        self.use_trg.setChecked(True)
        fl.addRow(self.use_trg)
        lay.addWidget(g)

        # ---- sample
        g = QGroupBox("4. Sample && output")
        fl = QFormLayout(g)
        self.sample = QLineEdit("sample1")
        fl.addRow("Sample name:", self.sample)
        self.operator = QLineEdit()
        fl.addRow("Operator:", self.operator)
        self.temp = QLineEdit()
        self.temp.setPlaceholderText("optional, e.g. 25 °C")
        fl.addRow("Temperature:", self.temp)
        self.notes = QLineEdit()
        fl.addRow("Notes:", self.notes)
        oh = QHBoxLayout()
        self.out_dir = QLineEdit(str(Path.home() / "LCR_data"))
        b = QPushButton("…")
        b.setFixedWidth(30)
        b.clicked.connect(self._browse_out)
        oh.addWidget(self.out_dir)
        oh.addWidget(b)
        fl.addRow("Data folder:", oh)
        self.autosave = QCheckBox("Auto-save every point to CSV")
        self.autosave.setChecked(True)
        fl.addRow(self.autosave)
        lay.addWidget(g)

        # ---- run
        rh = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start sweep")
        self.btn_start.setMinimumHeight(38)
        f = QFont()
        f.setBold(True)
        self.btn_start.setFont(f)
        self.btn_start.clicked.connect(self.start_sweep)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.setMinimumHeight(38)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_sweep)
        rh.addWidget(self.btn_start, 2)
        rh.addWidget(self.btn_stop, 1)
        lay.addLayout(rh)
        self.progress = QProgressBar()
        lay.addWidget(self.progress)
        self.eta = QLabel("")
        lay.addWidget(self.eta)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(410)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _build_right(self) -> QWidget:
        tabs = QTabWidget()

        # plots + run list
        pw = QWidget()
        pl = QVBoxLayout(pw)
        sp = QSplitter(Qt.Vertical)
        self.plot1 = PlotPanel("Z")
        self.plot2 = PlotPanel("theta")
        for p in (self.plot1, self.plot2):
            for wdg in (p.param, p.logx, p.logy, p.absy):
                sig = wdg.currentIndexChanged if isinstance(wdg, QComboBox) else wdg.toggled
                sig.connect(self.redraw)
        sp.addWidget(self.plot1)
        sp.addWidget(self.plot2)
        pl.addWidget(sp, 1)
        self.readout = QLabel("")
        self.readout.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pl.addWidget(self.readout)
        tabs.addTab(pw, "Plots")

        # Nyquist
        self.nyq = pg.PlotWidget()
        self.nyq.showGrid(x=True, y=True, alpha=0.3)
        self.nyq.addLegend()
        self.nyq.setLabel("bottom", "Z' = Rs", units="Ω")
        self.nyq.setLabel("left", "-Z'' = -X", units="Ω")
        self.nyq.setAspectLocked(False)
        tabs.addTab(self.nyq, "Nyquist (Z' vs −Z'')")

        # table
        self.table = QTableWidget(0, len(ROW_KEYS))
        self.table.setHorizontalHeaderLabels(
            [f"{PARAM_LABEL.get(k, k)}" + (f" [{PARAM_UNIT[k]}]" if PARAM_UNIT.get(k) else "") for k in ROW_KEYS]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        tabs.addTab(self.table, "Data table")

        # console
        cw = QWidget()
        cl = QVBoxLayout(cw)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setFont(QFont("Consolas", 9))
        cl.addWidget(self.console, 1)
        ch = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("GPIB command, e.g. *IDN?  or  :FREQuency 1000   (ending in ? = query)")
        self.cmd.returnPressed.connect(self.send_cmd)
        b = QPushButton("Send")
        b.clicked.connect(self.send_cmd)
        self.log_traffic = QCheckBox("Log all GPIB traffic")
        ch.addWidget(self.cmd, 1)
        ch.addWidget(b)
        ch.addWidget(self.log_traffic)
        cl.addLayout(ch)
        tabs.addTab(cw, "GPIB console")

        # runs list on the side
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(QLabel("<b>Runs</b> (tick to overlay)"))
        self.run_list = QListWidget()
        self.run_list.itemChanged.connect(self.redraw)
        self.run_list.currentRowChanged.connect(self._fill_table)
        sl.addWidget(self.run_list, 1)
        for text, slot in [("Load CSV…", self.open_csv), ("Save selected…", self.save_selected_csv),
                           ("Remove selected", self.remove_run), ("Clear all", self.clear_runs)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            sl.addWidget(b)

        outer = QSplitter(Qt.Horizontal)
        outer.addWidget(tabs)
        outer.addWidget(side)
        outer.setStretchFactor(0, 1)
        outer.setSizes([900, 220])
        return outer

    # ============================================================ UI helpers
    def _backend_changed(self) -> None:
        b = self.backend.currentData()
        for wdg, show in [(self.visa_res, b == "visa"), (self.visa_row_lbl, b == "visa"),
                          (self.addr_row, b == "gpib32"), (self.addr_lbl, b == "gpib32"),
                          (self.sim_dut, b == "sim"), (self.sim_lbl, b == "sim")]:
            wdg.setVisible(show)
        self.btn_scan.setEnabled(b != "sim")

    def _spacing_changed(self) -> None:
        custom = self.spacing.currentData() == "list"
        for wdg in (self.custom, self.custom_lbl):
            wdg.setVisible(custom)
        for wdg in (self.f_start, self.f_stop, self.n_points):
            wdg.setEnabled(not custom)
        self._update_estimate()

    def _level_mode_changed(self) -> None:
        self.level_unit.setText("A rms" if self.level_mode.currentData() == "CC" else "V rms")
        if self.level_mode.currentData() == "CC" and self.level_val.text() in ("1", "1.0"):
            self.level_val.setText("10m")

    def _browse_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Data folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _log(self, text: str, color: str | None = None) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.console.appendPlainText(f"[{ts}] {text}")

    def _log_traffic(self, direction: str, text: str) -> None:
        if self.log_traffic.isChecked():
            self._log(f"{direction} {text}")

    def _check_max_level(self, cfg: SweepConfig) -> str | None:
        """The 3532-50 restricts the level range above 1 MHz."""
        m = cfg.meas
        fmax = max(cfg.frequencies())
        if m.level_mode in ("V", "CV"):
            lo, hi = (0.05, 1.0) if fmax > 1e6 else (0.01, 5.0)
            unit = "V"
        else:
            lo, hi = (50e-6, 20e-3) if fmax > 1e6 else (10e-6, 100e-3)
            unit = "A"
        if not lo <= m.level_value <= hi:
            return (f"Level {si_format(m.level_value, unit)} is outside {si_format(lo, unit)} … "
                    f"{si_format(hi, unit)} allowed {'above 1 MHz' if fmax > 1e6 else ''}.")
        return None

    # ========================================================== config / meta
    def build_config(self) -> SweepConfig:
        spacing = self.spacing.currentData()
        custom = []
        if spacing == "list":
            txt = self.custom.toPlainText().replace(",", " ").replace(";", " ").split()
            custom = [parse_si(t) for t in txt]
            if not custom:
                raise ValueError("The custom frequency list is empty")
        cfg = SweepConfig(
            start_hz=parse_si(self.f_start.text()) if spacing != "list" else 0,
            stop_hz=parse_si(self.f_stop.text()) if spacing != "list" else 0,
            points=self.n_points.value(),
            spacing=spacing,
            custom_list=custom,
            settle_ms=self.settle.value(),
            readings_per_point=self.reads.value(),
            meas=MeasureSettings(
                level_mode=self.level_mode.currentData(),
                level_value=parse_si(self.level_val.text()),
                speed=self.speed.currentData(),
                averaging=self.avg.value(),
                use_trigger=self.use_trg.isChecked(),
            ),
        )
        lo, hi = self.profile["freq_min_hz"], self.profile["freq_max_hz"]
        bad = [f for f in cfg.frequencies() if not lo <= f <= hi]
        if bad:
            raise ValueError(f"Frequencies must be within {lo} Hz … {si_format(hi, 'Hz')} (got {bad[0]:g} Hz)")
        return cfg

    def _update_estimate(self) -> None:
        try:
            cfg = self.build_config()
            n = len(cfg.frequencies())
            t = estimate_duration_s(cfg)
            self.eta.setText(f"{n} points, estimated time ≈ {t:.0f} s" if t >= 1 else f"{n} points, < 1 s")
        except Exception as e:
            self.eta.setText(f"<span style='color:#c00'>{e}</span>")

    def metadata(self, cfg: SweepConfig) -> dict:
        return {
            "sample": self.sample.text().strip() or "sample",
            "operator": self.operator.text(),
            "temperature": self.temp.text(),
            "notes": self.notes.text(),
            "start_time": datetime.now().isoformat(timespec="seconds"),
            "instrument": self.idn_lbl.text(),
            "interface": self.backend.currentText(),
            "level_mode": cfg.meas.level_mode,
            "level_value": cfg.meas.level_value,
            "speed": cfg.meas.speed,
            "averaging": cfg.meas.averaging,
            "settle_ms": cfg.settle_ms,
            "readings_per_point": cfg.readings_per_point,
            "spacing": cfg.spacing,
            "start_hz": cfg.start_hz,
            "stop_hz": cfg.stop_hz,
            "points": len(cfg.frequencies()),
            "software": f"LCR Sweep v{__version__}",
        }

    # ============================================================ connection
    def scan(self) -> None:
        b = self.backend.currentData()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if b == "visa":
                res = VisaTransport.list_resources()
                self.visa_res.clear()
                self.visa_res.addItems(res)
                self._log(f"VISA resources: {res or 'none found'}")
                if not res:
                    QMessageBox.information(self, "Scan", "No VISA resources found.\nIs Keithley I/O Layer (KI-VISA) or NI-VISA installed?\nTry the 'GPIB-32 DLL' interface instead.")
            elif b == "gpib32":
                found = Gpib32Transport.scan(self.gpib_board.value())
                self._log(f"GPIB devices: {found or 'none found'}")
                if found:
                    self.gpib_addr.setValue(found[0][0])
                    QMessageBox.information(self, "Scan", "\n".join(f"Address {a}: {i}" for a, i in found))
                else:
                    QMessageBox.warning(self, "Scan", "No instruments answered *IDN? on addresses 1–30.")
        except Exception as e:
            QMessageBox.critical(self, "Scan failed", str(e))
        finally:
            QApplication.restoreOverrideCursor()

    def toggle_connect(self) -> None:
        if self.meter:
            self.disconnect_meter()
        else:
            self.connect_meter()

    def connect_meter(self) -> None:
        b = self.backend.currentData()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if b == "visa":
                t = VisaTransport(self.visa_res.currentText().strip())
            elif b == "gpib32":
                t = Gpib32Transport(self.gpib_addr.value(), self.gpib_board.value())
            else:
                t = SimulatedTransport(self.sim_dut.currentText())
            t.on_traffic = self.bridge.message.emit
            meter = Hioki3532(t, self.profile)
            idn = meter.idn()
            if not idn:
                raise TransportError("No answer to *IDN?")
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Connection failed", f"{e}\n\nCheck: adapter driver installed, GPIB cable, "
                                 "meter switched on, GPIB address on the 3532-50 interface settings.")
            return
        QApplication.restoreOverrideCursor()
        self.transport, self.meter = t, meter
        self.idn_lbl.setText(idn)
        self.btn_connect.setText("Disconnect")
        self.backend.setEnabled(False)
        self.statusBar().showMessage(f"Connected: {idn}")
        self._log(f"Connected via {t.name}: {idn}")
        if "3532" not in idn:
            QMessageBox.warning(self, "Unexpected instrument", f"*IDN? returned:\n{idn}\n\nThis app is made for the Hioki 3532-50.")

    def disconnect_meter(self) -> None:
        if self.thread:
            return
        if self.transport:
            self.transport.close()
        self.transport = self.meter = None
        self.idn_lbl.setText("—")
        self.btn_connect.setText("Connect")
        self.backend.setEnabled(True)
        self.statusBar().showMessage("Not connected")
        self._log("Disconnected")

    def send_cmd(self) -> None:
        c = self.cmd.text().strip()
        if not c:
            return
        if not self.meter:
            QMessageBox.information(self, "Console", "Connect first.")
            return
        if self.thread:
            QMessageBox.information(self, "Console", "Wait until the sweep has finished.")
            return
        try:
            if c.endswith("?"):
                self._log(f"{c}  →  {self.meter.raw_query(c)}")
            else:
                self.meter.raw_write(c)
                self._log(f"{c}  (sent)")
        except Exception as e:
            self._log(f"{c}  ERROR: {e}")
        self.cmd.clear()

    # ================================================================= sweep
    def start_sweep(self) -> None:
        if not self.meter:
            QMessageBox.information(self, "Not connected", "Connect to the meter first (or choose the Simulator).")
            return
        try:
            cfg = self.build_config()
        except Exception as e:
            QMessageBox.warning(self, "Invalid settings", str(e))
            return
        warn = self._check_max_level(cfg)
        if warn and QMessageBox.question(self, "Signal level", warn + "\n\nStart anyway?") != QMessageBox.Yes:
            return

        meta = self.metadata(cfg)
        name = meta["sample"]
        existing = {r.name for r in self.runs}
        k = 2
        while name in existing:
            name = f"{meta['sample']} ({k})"
            k += 1
        self.current = Run(name=name, meta=meta)
        self._add_run(self.current)
        self.run_list.setCurrentRow(self.run_list.count() - 1)
        self.table.setRowCount(0)

        if self.autosave.isChecked():
            try:
                path = Path(self.out_dir.text()) / default_filename(meta["sample"])
                self.appender = CsvAppender(path, meta)
                self.current.path = str(path)
                self._log(f"Auto-saving to {path}")
            except OSError as e:
                QMessageBox.warning(self, "Auto-save", f"Cannot write to data folder:\n{e}")
                self.appender = None

        self.progress.setRange(0, len(cfg.frequencies()))
        self.progress.setValue(0)
        self._set_running(True)
        self.t_start = time.time()
        self._log(f"Sweep started: {len(cfg.frequencies())} points")

        self.thread = QThread(self)
        self.worker = SweepWorker(self.meter, cfg)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.point.connect(self._on_point)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.thread.start()

    def stop_sweep(self) -> None:
        if self.worker:
            self.worker.stop()
            self.btn_stop.setEnabled(False)
            self._log("Stopping after the current point…")

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_connect.setEnabled(not running)

    def _on_point(self, row: dict, done: int, total: int) -> None:
        self.current.rows.append(row)
        if self.appender:
            self.appender.append(row)
        self.progress.setValue(done)
        el = time.time() - self.t_start
        rem = el / done * (total - done)
        self.eta.setText(f"Point {done}/{total}   elapsed {el:.0f} s   remaining ≈ {rem:.0f} s")
        v = row
        self.readout.setText(
            f"f = {si_format(v['freq_Hz'], 'Hz')}   |Z| = {si_format(v['Z'], 'Ω')}   θ = {v['theta']:.3f}°   "
            f"Cs = {si_format(v['Cs'], 'F')}   Cp = {si_format(v['Cp'], 'F')}   Ls = {si_format(v['Ls'], 'H')}   "
            f"Rs = {si_format(v['Rs'], 'Ω')}   D = {v['D']:.5g}   Q = {v['Q']:.5g}"
        )
        self._append_table_row(row)
        self.redraw()

    def _finish(self) -> None:
        if self.appender:
            self.appender.close()
            self.appender = None
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
        self.thread = self.worker = None
        self._set_running(False)

    def _on_finished(self, complete: bool) -> None:
        n = len(self.current.rows) if self.current else 0
        self._finish()
        msg = f"Sweep {'completed' if complete else 'stopped'}: {n} points in {time.time() - self.t_start:.1f} s"
        if self.current and self.current.path:
            msg += f" — saved to {self.current.path}"
        self._log(msg)
        self.statusBar().showMessage(msg)

    def _on_failed(self, err: str) -> None:
        self._finish()
        self._log(f"ERROR: {err}")
        QMessageBox.critical(self, "Measurement error",
                             f"{err.splitlines()[0]}\n\nData measured so far is kept"
                             + (" and saved." if self.current and self.current.path else "."))

    # ========================================================= runs / plots
    def _add_run(self, run: Run) -> None:
        self.runs.append(run)
        it = QListWidgetItem(run.name)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        color = PALETTE[(len(self.runs) - 1) % len(PALETTE)]
        it.setData(Qt.UserRole, color)
        it.setForeground(QColor(color))
        self.run_list.blockSignals(True)
        self.run_list.addItem(it)
        self.run_list.blockSignals(False)

    def visible_runs(self) -> list[tuple[Run, str]]:
        out = []
        for i in range(self.run_list.count()):
            it = self.run_list.item(i)
            if it.checkState() == Qt.Checked:
                out.append((self.runs[i], it.data(Qt.UserRole)))
        return out

    def redraw(self) -> None:
        runs = self.visible_runs()
        self.plot1.redraw(runs)
        self.plot2.redraw(runs)
        self.nyq.clear()
        for run, color in runs:
            x = np.array(run.column("Rs"))
            y = -np.array(run.column("X"))
            ok = np.isfinite(x) & np.isfinite(y)
            self.nyq.plot(x[ok], y[ok], pen=pg.mkPen(color, width=2), symbol="o", symbolSize=5,
                          symbolBrush=color, symbolPen=None, name=run.name)

    def _append_table_row(self, row: dict) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, k in enumerate(ROW_KEYS):
            self.table.setItem(r, c, _num_item(row.get(k, "")))
        self.table.scrollToBottom()

    def _fill_table(self, idx: int) -> None:
        self.table.setRowCount(0)
        if 0 <= idx < len(self.runs):
            self.table.setUpdatesEnabled(False)
            for row in self.runs[idx].rows:
                self._append_table_row(row)
            self.table.setUpdatesEnabled(True)

    def _selected_run(self) -> Run | None:
        i = self.run_list.currentRow()
        return self.runs[i] if 0 <= i < len(self.runs) else None

    def remove_run(self) -> None:
        i = self.run_list.currentRow()
        if i < 0 or (self.thread and self.runs[i] is self.current):
            return
        self.runs.pop(i)
        self.run_list.takeItem(i)
        self.redraw()

    def clear_runs(self) -> None:
        if self.thread:
            return
        self.runs.clear()
        self.run_list.clear()
        self.table.setRowCount(0)
        self.redraw()

    # =================================================================== files
    def open_csv(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open data", self.out_dir.text(), "CSV files (*.csv)")
        for p in paths:
            try:
                run = load_csv(p)
                self._add_run(run)
                self._log(f"Loaded {p} ({len(run.rows)} points)")
            except Exception as e:
                QMessageBox.warning(self, "Open", f"{p}\n{e}")
        self.redraw()

    def save_selected_csv(self) -> None:
        run = self._selected_run()
        if not run:
            QMessageBox.information(self, "Save", "Select a run in the list first.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save CSV", str(Path(self.out_dir.text()) / default_filename(run.name)),
                                           "CSV files (*.csv)")
        if p:
            save_csv(run, p)
            self._log(f"Saved {p}")

    def export_xlsx(self) -> None:
        if not self.runs:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Export Excel", str(Path(self.out_dir.text()) / "lcr_runs.xlsx"),
                                           "Excel files (*.xlsx)")
        if p:
            try:
                save_xlsx(self.runs, p)
                self._log(f"Exported {len(self.runs)} runs to {p}")
            except Exception as e:
                QMessageBox.warning(self, "Export", str(e))

    def save_plot_image(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Save plots", str(Path(self.out_dir.text()) / "plot.png"),
                                           "PNG (*.png)")
        if not p:
            return
        from pyqtgraph.exporters import ImageExporter

        base = Path(p)
        for suffix, plot in (("_1", self.plot1.plot), ("_2", self.plot2.plot), ("_nyquist", self.nyq)):
            ex = ImageExporter(plot.plotItem)
            ex.parameters()["width"] = 1600
            ex.export(str(base.with_name(base.stem + suffix + base.suffix)))
        self._log(f"Plots saved as {base.stem}_1/_2/_nyquist.png")

    # =========================================================== profile tools
    def verify_profile(self) -> None:
        if not self.meter or self.thread:
            QMessageBox.information(self, "Verify", "Connect first (and do not run a sweep).")
            return
        try:
            cfg = self.build_config()
        except Exception as e:
            QMessageBox.warning(self, "Verify", str(e))
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = self.meter.verify_profile(cfg.meas)
        finally:
            QApplication.restoreOverrideCursor()
        lines = [f"{'OK  ' if ok else 'FAIL'}  {c:<32} {r}" for c, r, ok in res]
        self._log("Command profile check:\n" + "\n".join(lines))
        bad = [c for c, _, ok in res if not ok]
        if bad:
            QMessageBox.warning(self, "Verify", "The meter rejected:\n" + "\n".join(bad)
                                + "\n\nCorrect them in the command profile (Tools menu), then Reload.\nDetails in the GPIB console.")
        else:
            QMessageBox.information(self, "Verify", "All commands accepted by the meter.")

    def open_profile(self) -> None:
        path = str(PROFILE_PATH)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])

    def reload_profile(self) -> None:
        try:
            self.profile = load_profile()
            if self.meter:
                self.meter.p = self.profile
            self._log("Command profile reloaded")
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Profile", f"Cannot read {PROFILE_PATH}:\n{e}")

    def about(self) -> None:
        QMessageBox.about(self, "About", f"<b>LCR Sweep</b> v{__version__}<br>Frequency-sweep measurements with the "
                          "Hioki 3532-50 LCR HiTESTER over GPIB.<br><br>|Z| and θ are read from the meter; all other "
                          "parameters (Rs, X, Ls, Cs, Rp, Lp, Cp, |Y|, G, B, D, Q) are calculated from them.")

    # ============================================================ settings
    _PERSIST = ["f_start", "f_stop", "level_val", "sample", "operator", "out_dir"]
    _PERSIST_SPIN = ["n_points", "avg", "settle", "reads", "gpib_addr", "gpib_board"]
    _PERSIST_COMBO = ["backend", "spacing", "level_mode", "speed"]

    def _restore_settings(self) -> None:
        s = self.settings
        for n in self._PERSIST:
            v = s.value(n)
            if v:
                getattr(self, n).setText(str(v))
        for n in self._PERSIST_SPIN:
            v = s.value(n)
            if v is not None:
                getattr(self, n).setValue(int(v))
        for n in self._PERSIST_COMBO:
            v = s.value(n)
            if v is not None:
                getattr(self, n).setCurrentIndex(int(v))
        v = s.value("visa_res")
        if v:
            self.visa_res.setEditText(str(v))
        v = s.value("custom")
        if v:
            self.custom.setPlainText(str(v))
        self._spacing_changed()
        self._level_mode_changed()

    def closeEvent(self, ev) -> None:
        if self.thread:
            if QMessageBox.question(self, "Quit", "A sweep is running. Stop it and quit?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.worker.stop()
            self.thread.quit()
            self.thread.wait(5000)
        s = self.settings
        for n in self._PERSIST:
            s.setValue(n, getattr(self, n).text())
        for n in self._PERSIST_SPIN:
            s.setValue(n, getattr(self, n).value())
        for n in self._PERSIST_COMBO:
            s.setValue(n, getattr(self, n).currentIndex())
        s.setValue("visa_res", self.visa_res.currentText())
        s.setValue("custom", self.custom.toPlainText())
        if self.appender:
            self.appender.close()
        if self.transport:
            self.transport.close()
        ev.accept()
