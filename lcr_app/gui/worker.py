"""Runs a sweep in a background thread so the window never freezes."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, Signal, Slot

from ..core.sweep import SweepConfig, SweepRunner
from ..instrument.hioki3532 import Hioki3532


class SweepWorker(QObject):
    point = Signal(dict, int, int)  # row, done, total
    finished = Signal(bool)  # True = completed, False = aborted
    failed = Signal(str)

    def __init__(self, meter: Hioki3532, cfg: SweepConfig) -> None:
        super().__init__()
        self.runner = SweepRunner(meter, cfg)

    @Slot()
    def run(self) -> None:
        try:
            self.runner.run(lambda p, i, n: self.point.emit(p.as_row(), i, n))
            self.finished.emit(not self.runner.stopped)
        except Exception as e:  # report to the GUI instead of crashing the thread
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")

    def stop(self) -> None:
        self.runner.stop()


class TrafficBridge(QObject):
    """Forwards GPIB traffic from any thread to the console widget."""

    message = Signal(str, str)
