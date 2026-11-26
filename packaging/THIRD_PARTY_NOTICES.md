# Third-party software in LCR Sweep (Windows release)

The stand-alone Windows release (`LCRSweep.exe` and the `_internal` folder) contains the following
third-party software. The full licence texts are in the `licenses` folder next to this file.

| Component | Version | Licence | Licence text |
|---|---|---|---|
| Qt 6 and Qt for Python (PySide6, Shiboken6) | 6.12.0 | **LGPL-3.0** (used under the LGPL option) | `licenses/LGPL-3.0.txt`, `licenses/GPL-3.0.txt` |
| Python runtime | 3.13 | PSF License 2.0 | `licenses/Python-PSF.txt` |
| NumPy (incl. bundled OpenBLAS) | 2.5.5 | BSD-3-Clause and others (listed in the file) | `licenses/numpy.txt` |
| pyqtgraph | 0.14.0 | MIT | `licenses/pyqtgraph-MIT.txt` |
| PyVISA | 1.16.2 | MIT | `licenses/PyVISA-MIT.txt` |
| openpyxl | 3.1.5 | MIT | `licenses/openpyxl-MIT.txt` |
| et_xmlfile | 2.0.0 | MIT | `licenses/et_xmlfile-MIT.txt` |
| typing_extensions | 4.16.0 | PSF-2.0 | `licenses/typing_extensions-PSF.txt` |
| Microsoft Visual C++ runtime (`VCRUNTIME140*.dll`, `msvcp140*.dll`, `ucrtbase.dll`, `api-ms-win-*.dll`) | – | Microsoft redistributable runtime, distributed with the Python runtime | – |
| Research & Analysis tab libraries (SciPy, Matplotlib, google-genai, keyring, python-docx, Markdown and their dependencies) | see the generated section at the end of this file | BSD, MIT, Apache-2.0, PSF, MPL-2.0 (certifi) and others | `licenses/<package>/` |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later **with the bootloader exception**, which allows distributing programs built with it under any licence | – |

## Qt / PySide6 (LGPL-3.0)

* LCR Sweep uses Qt and PySide6 under the **GNU Lesser General Public License v3**. Qt is a trademark of
  The Qt Company Ltd. Copyright (C) The Qt Company Ltd. and other contributors.
* The Qt and PySide6 libraries are **not modified** and are shipped as separate, dynamically loaded files in
  `_internal\PySide6\` (`Qt6*.dll`, `*.pyd`). You may replace them with your own compatible build of the
  same version; the program will use the replaced files.
* Source code of the exact versions used: Qt for Python 6.12.0 from
  <https://download.qt.io/official_releases/QtForPython/> and Qt 6 from
  <https://download.qt.io/official_releases/qt/>. If these links ever stop working, open an issue on the
  LCR Sweep GitHub project to obtain the source code.
* GPL-only Qt modules (for example Qt Virtual Keyboard, Qt Charts) are **not** included.

## Source code of LCR Sweep

<https://github.com/arshadthottappali/LCR-meter->

## Trademarks

Hioki and LCR HiTESTER are trademarks of HIOKI E.E. CORPORATION. Keithley is a trademark of Tektronix, Inc.
NI, NI-488.2 and NI-VISA are trademarks of National Instruments / Emerson. Keysight is a trademark of
Keysight Technologies. They are used only to name the compatible hardware and software. LCR Sweep is not
affiliated with, endorsed by or supported by any of these companies.
