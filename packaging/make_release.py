"""Build the stand-alone Windows release: dist/LCRSweep-<version>-windows.zip (+ .sha256).

Run from the project folder with the project's virtual environment:
    .venv\\Scripts\\python.exe packaging\\make_release.py

Steps: PyInstaller build -> remove GPL-only Qt modules (licence compliance) ->
add read-me, third-party notices and licence texts -> zip -> SHA-256 checksum.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
APP = DIST / "LCRSweep"

# Qt modules that are GPL/commercial only (not LGPL). They must not be shipped.
GPL_ONLY_QT = ["virtualkeyboard", "charts", "datavisualization", "graphs", "quick3d", "lottie",
               "networkauth", "coap", "mqtt", "opcua"]


def is_gpl_only(name: str) -> bool:
    """Qt6<Module>*.dll, Qt<Module>*.pyd or qt<module>*plugin.dll of a GPL-only module."""
    n = name.lower()
    return any(n.startswith(("qt6" + m, "qt" + m)) for m in GPL_ONLY_QT)


def version() -> str:
    ns: dict = {}
    exec((ROOT / "lcr_app" / "__init__.py").read_text(encoding="utf-8"), ns)
    return ns["__version__"]


def build() -> None:
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "LCRSweep",
        "--exclude-module", "tkinter", "--exclude-module", "matplotlib", "--exclude-module", "IPython",
        "--exclude-module", "PySide6.QtVirtualKeyboard",
        "--add-data", "lcr_app/instrument/commands_3532_50.json;lcr_app/instrument",
        "--add-data", "lcr_app/help/manual.html;lcr_app/help",
        "run_lcr.py",
    ], cwd=ROOT, check=True)


def strip_gpl_only() -> list[str]:
    removed = []
    for f in APP.rglob("*"):
        if f.is_file() and is_gpl_only(f.name):
            f.unlink()
            removed.append(str(f.relative_to(APP)))
    left = [f for f in APP.rglob("*") if f.is_file() and is_gpl_only(f.name)]
    if left:
        raise SystemExit(f"GPL-only Qt files still present: {left}")
    return removed


def add_documents() -> None:
    shutil.copy2(PKG / "READ ME FIRST.txt", APP / "READ ME FIRST.txt")
    shutil.copy2(PKG / "THIRD_PARTY_NOTICES.md", APP / "THIRD_PARTY_NOTICES.md")
    if (ROOT / "LICENSE").is_file():
        shutil.copy2(ROOT / "LICENSE", APP / "LICENSE.txt")
    shutil.rmtree(APP / "licenses", ignore_errors=True)
    shutil.copytree(PKG / "licenses", APP / "licenses")


def make_zip(ver: str) -> Path:
    zpath = DIST / f"LCRSweep-{ver}-windows.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(APP.rglob("*")):
            if f.is_file():
                z.write(f, Path("LCRSweep") / f.relative_to(APP))
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    zpath.with_name(zpath.name + ".sha256").write_text(f"{digest}  {zpath.name}\n", encoding="ascii")
    return zpath


def main() -> None:
    ver = version()
    if "--no-build" not in sys.argv:
        build()
    removed = strip_gpl_only()
    print("Removed GPL-only Qt files:", removed or "none")
    add_documents()
    z = make_zip(ver)
    print(f"Release: {z} ({z.stat().st_size / 1e6:.1f} MB)")
    print("SHA-256:", z.with_name(z.name + ".sha256").read_text().split()[0])


if __name__ == "__main__":
    main()
