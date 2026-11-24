"""High-level driver for the Hioki 3532-50 LCR HiTESTER.

All GPIB command strings come from commands_3532_50.json, so they can be
corrected without touching the code.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .transport import Transport, TransportError

PROFILE_PATH = Path(__file__).with_name("commands_3532_50.json")
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def load_profile(path: str | Path = PROFILE_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class MeasureSettings:
    level_mode: str = "V"  # V, CV or CC
    level_value: float = 1.0  # volts (V, CV) or amperes (CC)
    speed: str = "NORMal"
    averaging: int = 1
    use_trigger: bool = True  # external trigger + *TRG for each point (fresh reading guaranteed)


class Hioki3532:
    def __init__(self, transport: Transport, profile: dict | None = None) -> None:
        self.t = transport
        self.p = profile or load_profile()

    # ------------------------------------------------------------------ basics
    def idn(self) -> str:
        return self.t.query(self.p["idn"])

    def raw_write(self, cmd: str) -> None:
        self.t.write(cmd)

    def raw_query(self, cmd: str) -> str:
        return self.t.query(cmd)

    def event_status(self) -> int:
        try:
            return int(float(self.t.query(self.p["event_status_query"])))
        except (ValueError, TransportError):
            return -1

    # ----------------------------------------------------------- configuration
    def configure(self, s: MeasureSettings) -> None:
        p = self.p
        allowed = p.get("averaging_values")
        if allowed and int(s.averaging) not in allowed:
            raise ValueError(f"Averaging {s.averaging} is not supported by the meter; use one of {allowed}")
        self.t.write(p["clear_status"])
        for cmd in (
            p["header_off"],
            p["set_measure_items"],
            p["set_range_auto"],
            p["set_speed"].format(speed=s.speed),
            p["set_averaging"].format(value=int(s.averaging)),
            p["set_level_mode"].format(mode=s.level_mode),
            p["set_level_value"][s.level_mode].format(value=s.level_value),
            p["trigger_external"] if s.use_trigger else p["trigger_internal"],
        ):
            self.t.write(cmd)
            # The meter silently keeps its old setting when it rejects a command, so check every
            # setting: ESR bit 16 = execution error (value out of range), 32 = command error.
            esr = self.event_status()
            if esr > 0 and esr & 0x30:
                raise TransportError(
                    f"The meter rejected the setting '{cmd}' (*ESR? = {esr}). The value is probably outside "
                    "what the meter allows at these settings. Nothing was measured.")
        self.use_trigger = s.use_trigger

    def set_frequency(self, hz: float) -> None:
        lo, hi = self.p["freq_min_hz"], self.p["freq_max_hz"]
        if not lo <= hz <= hi:
            raise ValueError(f"Frequency {hz} Hz outside {lo}..{hi} Hz")
        self.t.write(self.p["set_frequency"].format(value=hz))

    def get_frequency(self) -> float:
        try:
            return float(_NUM.findall(self.t.query(self.p["query_frequency"]))[0])
        except (IndexError, ValueError):
            return math.nan

    def to_local(self) -> None:
        """Return the meter to free-running internal trigger after a sweep."""
        try:
            self.t.write(self.p["trigger_internal"])
        except TransportError:
            pass

    # ------------------------------------------------------------- measuring
    def measure_z_theta(self) -> tuple[float, float]:
        """One reading. Returns (|Z| ohm, theta deg); NaN on overflow."""
        if getattr(self, "use_trigger", False):
            self.t.write(self.p["trigger_now"])
        resp = self.t.query(self.p["measure"])
        nums = [float(x) for x in _NUM.findall(resp)]
        if len(nums) < 2:
            raise TransportError(f"Unexpected :MEASure? response: {resp!r}")
        z, th = nums[0], nums[1]
        lim = self.p.get("overflow_threshold", 1e19)
        if abs(z) >= lim:
            z = math.nan
        if abs(th) >= lim:
            th = math.nan
        return z, th

    # ---------------------------------------------------- profile self-check
    def verify_profile(self, s: MeasureSettings) -> list[tuple[str, str, bool]]:
        """Send every configuration command one at a time and read *ESR? after
        each. Returns (command, result, ok). A set command error bit
        (16 = execution error, 32 = command error) means the meter rejected it."""
        p = self.p
        cmds = [
            p["header_off"], p["set_measure_items"], p["set_range_auto"],
            p["set_speed"].format(speed=s.speed),
            p["set_averaging"].format(value=int(s.averaging)),
            p["set_level_mode"].format(mode=s.level_mode),
            p["set_level_value"][s.level_mode].format(value=s.level_value),
            p["set_frequency"].format(value=1000.0),
            p["trigger_external"], p["trigger_now"], p["trigger_internal"],
        ]
        results = []
        self.t.write(p["clear_status"])
        for c in cmds:
            try:
                self.t.write(c)
                time.sleep(0.05)
                esr = self.event_status()
                ok = esr >= 0 and not (esr & 0x30)
                results.append((c, f"ESR={esr}", ok))
            except TransportError as e:
                results.append((c, str(e), False))
        for q in (p["query_frequency"], p["measure"]):
            try:
                r = self.t.query(q)
                results.append((q, r, bool(_NUM.findall(r))))
            except TransportError as e:
                results.append((q, str(e), False))
        return results
