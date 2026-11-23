"""Start the application:  python -m lcr_app"""
import sys

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from .gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LCR Sweep")
    app.setStyle("Fusion")
    pg.setConfigOptions(antialias=True, background="w", foreground="k")
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
