"""Collect licence information of the Research-tab libraries and everything they depend on.

Runs on the build machine during make_release.py, so the versions and licence texts are exactly
those that end up in the release. Licence files are copied from each package's metadata.
"""
from __future__ import annotations

import re
import shutil
from importlib import metadata
from pathlib import Path

# top-level libraries added by the Research & Analysis tab
ROOTS = ["scipy", "matplotlib", "google-genai", "keyring", "python-docx", "Markdown"]
# already documented by hand in THIRD_PARTY_NOTICES.md
KNOWN = {"numpy", "pyside6", "pyside6-essentials", "pyside6-addons", "shiboken6", "pyqtgraph", "pyvisa",
         "openpyxl", "et-xmlfile", "typing-extensions"}
LICENSE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(roots=ROOTS) -> list[metadata.Distribution]:
    seen: dict[str, metadata.Distribution] = {}
    todo = list(roots)
    while todo:
        name = _norm(todo.pop())
        if name in seen:
            continue
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[name] = d
        for req in d.requires or []:
            if "extra ==" in req:
                continue
            todo.append(re.split(r"[ ;<>=!~\[(]", req.strip(), maxsplit=1)[0])
    return [d for n, d in sorted(seen.items()) if n not in KNOWN]


def licence_of(d: metadata.Distribution) -> str:
    m = d.metadata
    lic = m.get("License-Expression") or ""
    if not lic:
        cls = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = ", ".join(cls) or (m.get("License") or "").splitlines()[0][:80] if (cls or m.get("License")) else ""
    return lic or "see licence file"


def collect(dest: Path) -> str:
    """Copy licence files into dest/<package>/ and return a Markdown table."""
    rows = ["| Component | Version | Licence | Licence text |", "|---|---|---|---|"]
    for d in closure():
        name, ver = d.metadata["Name"], d.version
        target = dest / _norm(name)
        copied = []
        for f in d.files or []:
            if LICENSE_FILE.search(f.name) and ("dist-info" in str(f) or "licenses" in str(f).lower()):
                src = Path(d.locate_file(f))
                if src.is_file():
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target / src.name)
                    copied.append(src.name)
        where = f"`licenses/{_norm(name)}/`" if copied else "–"
        rows.append(f"| {name} | {ver} | {licence_of(d)} | {where} |")
    return "\n".join(rows)


if __name__ == "__main__":
    import sys
    import tempfile

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
    print(collect(out))
