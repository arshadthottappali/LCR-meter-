"""Dialogs of the Research tab: data import, AI settings, send-approval."""
from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..research.importer import CAP_UNITS, FREQ_UNITS, INPUT_KINDS, RES_UNITS, ImportSpec, sniff
from ..research.llm import GET_KEY_URL, GeminiLLM, LLMError, get_secret, pick_default_model, set_secret


# ------------------------------------------------------------------ import
class ImportDialog(QDialog):
    """Map the columns of any CSV/TXT file to frequency + a measured pair."""

    def __init__(self, path: str, c0: float, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Import {Path(path).name}")
        self.resize(820, 560)
        self.path = path
        self.c0 = c0
        delim, rows, first = sniff(path)
        self.rows = rows
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Preview of the file. Choose which columns hold the frequency and the measured values "
                             "(column numbers start at 1)."))
        self.table = QTableWidget()
        lay.addWidget(self.table, 1)
        form = QFormLayout()
        self.delim = QComboBox()
        for label, d in (("Comma ,", ","), ("Semicolon ;", ";"), ("Tab", "\t"), ("Space", " ")):
            self.delim.addItem(label, d)
        self.delim.setCurrentIndex(max(0, self.delim.findData(delim)))
        self.delim.currentIndexChanged.connect(self._reload)
        form.addRow("Delimiter:", self.delim)
        self.skip = QSpinBox()
        self.skip.setRange(0, 1000)
        self.skip.setValue(first)
        form.addRow("Header lines to skip:", self.skip)
        self.kind = QComboBox()
        for k, (label, _, _) in INPUT_KINDS.items():
            self.kind.addItem(label, k)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("The file contains:", self.kind)
        fh = QHBoxLayout()
        self.fcol = QSpinBox()
        self.fcol.setRange(1, 99)
        self.funit = QComboBox()
        self.funit.addItems(list(FREQ_UNITS))
        fh.addWidget(self.fcol)
        fh.addWidget(self.funit)
        form.addRow("Frequency column / unit:", fh)
        ah = QHBoxLayout()
        self.acol = QSpinBox()
        self.acol.setRange(1, 99)
        self.acol.setValue(2)
        self.aunit = QComboBox()
        ah.addWidget(self.acol)
        ah.addWidget(self.aunit)
        self.alabel = QLabel()
        form.addRow(self.alabel, ah)
        bh = QHBoxLayout()
        self.bcol = QSpinBox()
        self.bcol.setRange(1, 99)
        self.bcol.setValue(3)
        self.bunit = QComboBox()
        bh.addWidget(self.bcol)
        bh.addWidget(self.bunit)
        self.blabel = QLabel()
        form.addRow(self.blabel, bh)
        self.name = QLineEdit(Path(path).stem)
        form.addRow("Run name:", self.name)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill(rows)
        self._kind_changed()

    def _reload(self) -> None:
        import csv
        import io

        text = Path(self.path).read_text(encoding="utf-8", errors="replace").splitlines()[:60]
        self._fill(list(csv.reader(io.StringIO("\n".join(text)), delimiter=self.delim.currentData(),
                                   skipinitialspace=True)))

    def _fill(self, rows) -> None:
        rows = rows[:30]
        ncol = max((len(r) for r in rows), default=1)
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(ncol)
        self.table.setHorizontalHeaderLabels([str(i + 1) for i in range(ncol)])
        for i, r in enumerate(rows):
            for j, c in enumerate(r):
                self.table.setItem(i, j, QTableWidgetItem(c))

    def _kind_changed(self) -> None:
        k = self.kind.currentData()
        _, a, b = INPUT_KINDS[k]
        self.alabel.setText(f"{a} column / unit:")
        self.blabel.setText(f"{b} column / unit:")
        self.aunit.clear()
        self.bunit.clear()
        if k.startswith("C"):
            self.aunit.addItems(list(CAP_UNITS))
        elif k in ("Z_deg", "Z_rad", "ReIm", "ReNegIm"):
            self.aunit.addItems(list(RES_UNITS))
        else:
            self.aunit.addItem("—")
        self.bunit.addItems(list(RES_UNITS) if k == "Cp_Rp" else ["—"])

    def spec(self) -> ImportSpec:
        return ImportSpec(freq_col=self.fcol.value() - 1, a_col=self.acol.value() - 1, b_col=self.bcol.value() - 1,
                          kind=self.kind.currentData(), freq_unit=self.funit.currentText(),
                          a_unit=self.aunit.currentText(), b_unit=self.bunit.currentText(),
                          skip_rows=self.skip.value(), delimiter=self.delim.currentData(), c0=self.c0)


# --------------------------------------------------------------- settings
class AISettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI research settings")
        self.resize(560, 520)
        self.qs = settings
        lay = QVBoxLayout(self)

        g = QGroupBox("Google Gemini (free API key)")
        f = QFormLayout(g)
        info = QLabel("1. Click <b>Get free key</b> → sign in with Google → <b>Create API key</b>.<br>"
                      "2. Copy the key, paste it below, click <b>Test &amp; load models</b>.<br>"
                      "The key is stored in the Windows Credential Manager, never in a file.<br>"
                      "<i>Free tier: Google may use requests to improve its products; you approve every "
                      "send.</i>")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        f.addRow(info)
        kh = QHBoxLayout()
        self.key = QLineEdit(get_secret("gemini_api_key"))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("paste the API key here")
        show = QCheckBox("show")
        show.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        getk = QPushButton("Get free key")
        getk.clicked.connect(lambda: webbrowser.open(GET_KEY_URL))
        kh.addWidget(self.key, 1)
        kh.addWidget(show)
        kh.addWidget(getk)
        f.addRow("API key:", kh)
        mh = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        saved = self.qs.value("ai/model", "")
        if saved:
            self.model.addItem(saved)
        test = QPushButton("Test && load models")
        test.clicked.connect(self.test_key)
        mh.addWidget(self.model, 1)
        mh.addWidget(test)
        f.addRow("Model:", mh)
        self.model.setToolTip("Leave empty to pick the newest 'flash' model automatically (best free option).")
        lay.addWidget(g)

        g = QGroupBox("Literature sources (free)")
        f = QFormLayout(g)
        self.use_oa = QCheckBox("OpenAlex")
        self.use_oa.setChecked(self.qs.value("ai/use_openalex", True, type=bool))
        self.use_s2 = QCheckBox("Semantic Scholar")
        self.use_s2.setChecked(self.qs.value("ai/use_s2", True, type=bool))
        h = QHBoxLayout()
        h.addWidget(self.use_oa)
        h.addWidget(self.use_s2)
        f.addRow("Search in:", h)
        self.oa_email = QLineEdit(self.qs.value("ai/openalex_email", ""))
        self.oa_email.setPlaceholderText("optional – OpenAlex 'polite pool' (faster)")
        f.addRow("E-mail for OpenAlex:", self.oa_email)
        self.oa_key = QLineEdit(get_secret("openalex_api_key"))
        self.oa_key.setEchoMode(QLineEdit.Password)
        self.oa_key.setPlaceholderText("optional free key from openalex.org")
        f.addRow("OpenAlex API key:", self.oa_key)
        self.s2_key = QLineEdit(get_secret("s2_api_key"))
        self.s2_key.setEchoMode(QLineEdit.Password)
        self.s2_key.setPlaceholderText("optional free key from semanticscholar.org (higher rate limit)")
        f.addRow("Semantic Scholar key:", self.s2_key)
        lay.addWidget(g)

        g = QGroupBox("Limits (keep the free quota)")
        f = QFormLayout(g)
        self.max_q = QSpinBox()
        self.max_q.setRange(2, 12)
        self.max_q.setValue(self.qs.value("ai/max_queries", 5, type=int))
        f.addRow("Search queries per material:", self.max_q)
        self.max_p = QSpinBox()
        self.max_p.setRange(5, 60)
        self.max_p.setValue(self.qs.value("ai/max_papers", 20, type=int))
        f.addRow("Abstracts read by the AI:", self.max_p)
        self.max_steps = QSpinBox()
        self.max_steps.setRange(2, 20)
        self.max_steps.setValue(self.qs.value("ai/max_steps", 8, type=int))
        f.addRow("Max. agent steps (comparison):", self.max_steps)
        self.max_questions = QSpinBox()
        self.max_questions.setRange(0, 10)
        self.max_questions.setValue(self.qs.value("ai/max_questions", 3, type=int))
        f.addRow("Max. questions to you per run:", self.max_questions)
        self.each = QCheckBox("Ask before EVERY single request (not only once per run)")
        self.each.setChecked(self.qs.value("ai/approve_each", False, type=bool))
        f.addRow(self.each)
        lay.addWidget(g)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def test_key(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            llm = GeminiLLM(self.key.text().strip())
            names = llm.list_models()
        except LLMError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Gemini", str(e))
            return
        cur = self.model.currentText().strip()
        # one tiny real request: shows which model really has free quota for this key
        llm.model = cur if cur in names else pick_default_model(names)
        llm._model_cache = names
        llm.min_interval = 0
        notes = []
        llm.status = notes.append
        try:
            llm.generate("Key test", "Answer with one word.", "Reply with OK.")
            working, problem = llm.model, ""
        except LLMError as e:
            working, problem = "", str(e)
        QApplication.restoreOverrideCursor()
        self.model.clear()
        self.model.addItem("")  # automatic
        self.model.addItems(names)
        if working:
            self.model.setCurrentText(working)
            extra = ("\n\n" + "\n".join(notes)) if notes else ""
            QMessageBox.information(self, "Gemini", f"The key works. {len(names)} models available.\n"
                                                    f"Model with free quota that answered: {working}{extra}\n\n"
                                                    "Click Save to use it.")
        else:
            QMessageBox.warning(self, "Gemini", "The key is valid, but no model answered:\n\n" + problem +
                                "\n\nIf it mentions 'limit: 0', your Google project has no free quota for these "
                                "models (this happens in some regions or for some account types). "
                                "If it mentions a daily limit, try again tomorrow.")

    def save(self) -> None:
        ok = set_secret("gemini_api_key", self.key.text().strip())
        set_secret("openalex_api_key", self.oa_key.text().strip())
        set_secret("s2_api_key", self.s2_key.text().strip())
        if not ok and self.key.text().strip():
            QMessageBox.warning(self, "Key storage", "No secure credential store is available on this system. "
                                "The key is kept only until the program is closed.")
            self.parent()._session_key = self.key.text().strip()  # type: ignore[attr-defined]
        q = self.qs
        q.setValue("ai/model", self.model.currentText().strip())
        q.setValue("ai/use_openalex", self.use_oa.isChecked())
        q.setValue("ai/use_s2", self.use_s2.isChecked())
        q.setValue("ai/openalex_email", self.oa_email.text().strip())
        q.setValue("ai/max_queries", self.max_q.value())
        q.setValue("ai/max_papers", self.max_p.value())
        q.setValue("ai/max_steps", self.max_steps.value())
        q.setValue("ai/max_questions", self.max_questions.value())
        q.setValue("ai/approve_each", self.each.isChecked())
        self.accept()


# ----------------------------------------------------------------- approval
class ApproveDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lab = QLabel("<b>This will be sent to Google Gemini.</b> Read it, and only continue if you are happy to "
                     "share it (unpublished results!).")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        t = QPlainTextEdit(text)
        t.setReadOnly(True)
        lay.addWidget(t, 1)
        bb = QDialogButtonBox()
        bb.addButton("Send", QDialogButtonBox.AcceptRole)
        bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
