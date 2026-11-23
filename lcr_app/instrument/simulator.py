"""A software stand-in for the 3532-50 so the app can be tried without hardware.

It understands the commands in commands_3532_50.json and returns |Z| and theta
of a selectable test component (DUT) with a little measurement noise.
"""
from __future__ import annotations

import cmath
import math
import random
import re
import time

from .transport import Transport

# name -> function(freq) returning complex impedance
DUT_MODELS = {
    "Capacitor 100 nF (ESR 0.2 Ω, ESL 15 nH)":
        lambda f: 0.2 + 1j * 2 * math.pi * f * 15e-9 + 1 / (1j * 2 * math.pi * f * 100e-9),
    "Inductor 100 µH (DCR 0.5 Ω, Cpar 20 pF)":
        lambda f: _par(0.5 + 1j * 2 * math.pi * f * 100e-6, 1 / (1j * 2 * math.pi * f * 20e-12)),
    "Resistor 1 kΩ (Cpar 2 pF)":
        lambda f: _par(1000, 1 / (1j * 2 * math.pi * f * 2e-12)),
    "Dielectric sample (Debye relaxation)":
        lambda f: _debye(f),
}


def _par(a: complex, b: complex) -> complex:
    return a * b / (a + b)


def _debye(f: float) -> complex:
    # parallel-plate capacitor C0 = 10 pF filled with a Debye material
    eps_s, eps_inf, tau, sigma = 80.0, 5.0, 1e-5, 1e-7
    w = 2 * math.pi * f
    eps = eps_inf + (eps_s - eps_inf) / (1 + 1j * w * tau) - 1j * sigma / (w * 8.854e-12)
    return 1 / (1j * w * 10e-12 * eps)


class SimulatedTransport(Transport):
    name = "Simulator"

    MEAS_TIME = {"FAST": 0.005, "NORMAL": 0.021, "SLOW": 0.072, "SLOW2": 0.140}

    def __init__(self, dut: str | None = None, noise: float = 2e-4, realtime: bool = True) -> None:
        super().__init__()
        self.dut = dut or next(iter(DUT_MODELS))
        self.noise = noise
        self.realtime = realtime
        self.freq = 1000.0
        self.speed = "NORMAL"
        self.averaging = 1
        self.esr = 0
        self._out = ""

    def _write(self, cmd: str) -> None:
        for part in cmd.split(";"):
            self._handle(part.strip())

    def _read(self) -> str:
        out, self._out = self._out, ""
        return out

    def _handle(self, c: str) -> None:
        u = c.upper()
        if u == "*IDN?":
            self._out = "HIOKI,3532-50,SIMULATED,V1.00"
        elif u == "*ESR?":
            self._out, self.esr = str(self.esr), 0
        elif u.startswith(":FREQ") and u.endswith("?"):
            self._out = f"{self.freq:.5E}"
        elif u.startswith(":FREQ"):
            try:
                f = float(c.split()[1])
                if not 42 <= f <= 5e6:
                    raise ValueError
                self.freq = f
            except (IndexError, ValueError):
                self.esr |= 16  # execution error
        elif u.startswith(":SPEE"):
            arg = u.split()[1] if len(u.split()) > 1 else ""
            self.speed = "NORMAL" if arg.startswith("NORM") else arg
        elif u.startswith(":AVER"):
            m = re.search(r"\d+", u)
            self.averaging = int(m.group()) if m else 1
        elif u.startswith(":MEAS:ITEM") or u.startswith(":MEASURE:ITEM"):
            pass
        elif u.startswith(":MEAS") and u.endswith("?"):
            self._out = self._measure()
        elif u.startswith((":HEAD", ":LEV", ":RANG", ":TRIG", "*RST", "*CLS", "*TRG")):
            pass
        elif u:
            self.esr |= 32  # command error

    def _measure(self) -> str:
        if self.realtime:
            time.sleep(self.MEAS_TIME.get(self.speed, 0.02) * max(1, self.averaging))
        z = DUT_MODELS[self.dut](self.freq)
        n = self.noise / math.sqrt(max(1, self.averaging))
        mag = abs(z) * (1 + random.gauss(0, n))
        ph = math.degrees(cmath.phase(z)) + random.gauss(0, n * 57.3)
        return f"{mag:.5E},{ph:.3E}"
