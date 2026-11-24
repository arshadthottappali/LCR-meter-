"""Check which GPIB / VISA drivers are installed and explain what is missing.

Used by Tools > Check drivers / connection, and shown automatically when
Scan finds nothing or Connect fails.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field

from .transport import Gpib32Transport

# Where to get the drivers (manufacturer pages; search there for the adapter name).
DRIVER_LINKS = [
    ("Keithley KUSB-488 / KUSB-488A / KUSB-488B",
     "KUSB-488A driver (Tektronix/Keithley software downloads, search \"KUSB-488\")",
     "https://www.tek.com/en/support/software"),
    ("National Instruments GPIB-USB-HS, PCI/PCIe-GPIB",
     "NI-488.2 driver (includes the GPIB DLL)",
     "https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html"),
    ("Any adapter, for the 'VISA' interface",
     "NI-VISA runtime (or the VISA that comes with your adapter's software)",
     "https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html"),
    ("Keysight / Agilent 82357B",
     "Keysight IO Libraries Suite (includes Keysight VISA)",
     "https://www.keysight.com/find/iosuite"),
]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class DriverReport:
    checks: list[Check] = field(default_factory=list)
    gpib_dll: str = ""  # loadable NI-488.2 compatible DLL, if any
    visa_library: str = ""  # path of the VISA library PyVISA found, if any
    resources: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)

    @property
    def gpib_resources(self) -> list[str]:
        return [r for r in self.resources if r.upper().startswith("GPIB")]

    def to_html(self, error: str = "") -> str:
        h = []
        if error:
            h.append(f"<p style='color:#b00020'><b>Problem:</b> {_esc(error)}</p>")
        h.append("<h3>What was found on this PC</h3><table cellpadding='3'>")
        for c in self.checks:
            mark = "<span style='color:#1a7f37'>&#10004;</span>" if c.ok else "<span style='color:#b00020'>&#10008;</span>"
            h.append(f"<tr><td>{mark}</td><td><b>{_esc(c.name)}</b></td><td>{_esc(c.detail)}</td></tr>")
        h.append("</table>")
        if self.advice:
            h.append("<h3>What to do</h3><ol>")
            h.extend(f"<li>{a}</li>" for a in self.advice)
            h.append("</ol>")
        h.append("<h3>Driver downloads</h3><p>Install the driver for <b>your</b> GPIB adapter once "
                 "(this is the only extra software the app needs):</p><table border='1' cellspacing='0' cellpadding='4'>"
                 "<tr><th align='left'>Adapter</th><th align='left'>Install</th></tr>")
        for adapter, what, url in DRIVER_LINKS:
            h.append(f"<tr><td>{_esc(adapter)}</td><td><a href='{url}'>{_esc(what)}</a></td></tr>")
        h.append("</table>")
        return "".join(h)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _dll_path(handle) -> str:
    if sys.platform != "win32":
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    try:
        ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(handle._handle), buf, 1024)
    except Exception:
        return ""
    return buf.value


def usb_gpib_adapters() -> list[tuple[str, bool]]:
    """(name, connected) of GPIB adapters Windows knows about. Empty if it cannot be determined."""
    if sys.platform != "win32":
        return []
    import subprocess

    ps = ("Get-PnpDevice | Where-Object { $_.FriendlyName -match 'GPIB|KUSB|82357' } | "
          "ForEach-Object { $_.Status + '|' + $_.FriendlyName }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    seen: dict[str, bool] = {}
    for line in out.splitlines():
        status, _, name = line.strip().partition("|")
        if name:
            seen[name] = seen.get(name, False) or status == "OK"
    return sorted(seen.items())


def check_drivers(list_visa: bool = True) -> DriverReport:
    rep = DriverReport()

    # 0. Is a USB-GPIB adapter plugged in?
    adapters = usb_gpib_adapters()
    if adapters:
        plugged = [n for n, ok in adapters if ok]
        if plugged:
            rep.checks.append(Check("GPIB adapter connected", True, ", ".join(plugged)))
        else:
            rep.checks.append(Check("GPIB adapter connected", False,
                                    "driver knows " + ", ".join(n for n, _ in adapters)
                                    + ", but none is plugged in right now"))
            rep.advice.append("<b>Plug in the USB-GPIB adapter</b> (or re-plug it / try another USB port), wait "
                              "a few seconds, then click <b>Check again</b>.")

    # 1. NI-488.2 compatible GPIB DLL (used by the 'GPIB DLL' interface)
    if sys.platform == "win32":
        found = []
        for name in Gpib32Transport.DLL_NAMES:
            try:
                h = ctypes.windll.LoadLibrary(name)
                found.append(_dll_path(h) or name)
            except OSError:
                continue
        if found:
            rep.gpib_dll = found[0]
            rep.checks.append(Check("GPIB adapter driver (NI-488.2 compatible DLL)", True, ", ".join(found)))
        else:
            rep.checks.append(Check("GPIB adapter driver (NI-488.2 compatible DLL)", False,
                                    "not found (" + ", ".join(Gpib32Transport.DLL_NAMES) + ")"))
    else:
        rep.checks.append(Check("GPIB DLL interface", False, "only available on Windows; use VISA"))

    # 2. VISA library (used by the 'VISA' interface)
    try:
        import pyvisa

        try:
            rm = pyvisa.ResourceManager()
            rep.visa_library = str(getattr(rm.visalib, "library_path", "") or rm.visalib)
            rep.checks.append(Check("VISA library", True, rep.visa_library))
            if list_visa:
                try:
                    rep.resources = list(rm.list_resources())
                except Exception as e:  # e.g. VI_ERROR_RSRC_NFOUND when nothing is connected
                    rep.resources = []
                    if "RSRC_NFOUND" not in str(e):
                        rep.checks.append(Check("VISA resource list", False, str(e)))
            try:
                rm.close()
            except Exception:
                pass
        except Exception as e:
            rep.checks.append(Check("VISA library", False, f"not installed or not working ({e})"))
    except ImportError:
        rep.checks.append(Check("VISA library", False, "PyVISA (Python package) missing"))

    if rep.visa_library and list_visa:
        g = rep.gpib_resources
        rep.checks.append(Check("Instruments visible through VISA", bool(g),
                                ", ".join(rep.resources) if rep.resources else "none"))

    # 3. Advice
    if rep.gpib_dll and rep.gpib_resources:
        rep.advice.append(f"Drivers look fine. Choose <b>VISA</b> with <code>{rep.gpib_resources[0]}</code> "
                          "(or <b>GPIB DLL</b>) and click <b>Connect</b>.")
    elif rep.gpib_dll:
        rep.advice.append("The adapter driver is installed. Choose the interface <b>GPIB DLL</b> and click "
                          "<b>Scan</b>: it asks every GPIB address for *IDN?.")
        if not rep.visa_library:
            rep.advice.append("VISA is not needed for the GPIB DLL interface. Install a VISA library only if "
                              "you want to use the <b>VISA</b> interface.")
    elif rep.visa_library:
        rep.advice.append("A VISA library is installed but no GPIB adapter driver was found. Install the driver "
                          "for your adapter (table below), then restart the program.")
    else:
        rep.advice.append("No GPIB driver was found. Install the driver for <b>your</b> adapter from the table "
                          "below (for the Keithley KUSB-488A: the KUSB-488A driver), then restart the program.")
    if (rep.gpib_dll or rep.visa_library) and not rep.gpib_resources:
        rep.advice.append("If the drivers are installed but no instrument answers: check that the adapter is "
                          "plugged in (and shows up in its configuration utility as GPIB0), that the meter is "
                          "switched on, that the 3532-50 has the GP-IB interface board fitted, and that its GPIB "
                          "address (interface settings screen on the meter) matches the one you use.")
    rep.advice.append("Without any adapter you can still try the program: choose the interface "
                      "<b>Simulator (no hardware)</b>.")
    return rep
