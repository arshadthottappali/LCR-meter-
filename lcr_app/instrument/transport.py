"""Low-level GPIB transports.

Three ways to reach the meter, all with the same tiny interface
(write / query / close):

* VisaTransport  - PyVISA on top of any installed VISA library
                   (Keithley I/O Layer "KI-VISA", NI-VISA, Keysight VISA).
* Gpib32Transport - calls gpib-32.dll / ni4882.dll directly through ctypes.
                   The Keithley KUSB-488A driver installs an NI-488.2
                   compatible gpib-32.dll, so this works even when no VISA
                   library is installed.
* SimulatedTransport (simulator.py) - a fake 3532-50 for trying the app
                   without hardware.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time

log = logging.getLogger(__name__)


class TransportError(RuntimeError):
    pass


class Transport:
    """Base class. Subclasses implement _write and _read."""

    name = "base"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.on_traffic = None  # optional callback(direction, text) for the console

    def _emit(self, direction: str, text: str) -> None:
        if self.on_traffic:
            try:
                self.on_traffic(direction, text)
            except Exception:  # never let the console break a measurement
                pass

    def write(self, cmd: str) -> None:
        with self._lock:
            self._emit(">>", cmd)
            self._write(cmd)

    def query(self, cmd: str) -> str:
        with self._lock:
            self._emit(">>", cmd)
            self._write(cmd)
            resp = self._read().strip()
            self._emit("<<", resp)
            return resp

    def clear(self) -> None:  # device clear (SDC)
        pass

    def close(self) -> None:
        pass

    # subclass hooks
    def _write(self, cmd: str) -> None:
        raise NotImplementedError

    def _read(self) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------- VISA
class VisaTransport(Transport):
    name = "VISA"

    def __init__(self, resource: str, timeout_ms: int = 5000, visa_library: str = "") -> None:
        super().__init__()
        try:
            import pyvisa
        except ImportError as e:
            raise TransportError("PyVISA is not installed (pip install pyvisa)") from e
        try:
            self.rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
            self.inst = self.rm.open_resource(resource)
        except Exception as e:
            raise TransportError(f"Could not open {resource} via VISA: {e}") from e
        self.inst.timeout = timeout_ms
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self.resource = resource

    @staticmethod
    def list_resources(visa_library: str = "") -> list[str]:
        import pyvisa

        rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
        return list(rm.list_resources())

    def _write(self, cmd: str) -> None:
        try:
            self.inst.write(cmd)
        except Exception as e:
            raise TransportError(f"VISA write failed: {e}") from e

    def _read(self) -> str:
        try:
            return self.inst.read()
        except Exception as e:
            raise TransportError(f"VISA read failed (timeout?): {e}") from e

    def clear(self) -> None:
        try:
            self.inst.clear()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.inst.close()
        except Exception:
            pass


# ----------------------------------------------------------------- gpib-32.dll
_ERR = 0x8000  # ibsta bit: error
_TIMO = 0x4000  # ibsta bit: timeout
# NI-488.2 timeout codes: T10us = 1 ... T1s = 11, T3s = 12, T10s = 13, T30s = 14, T100s = 15
_TMO_CODES = [(10e-6, 1), (30e-6, 2), (100e-6, 3), (300e-6, 4), (1e-3, 5), (3e-3, 6), (10e-3, 7),
              (30e-3, 8), (100e-3, 9), (300e-3, 10), (1, 11), (3, 12), (10, 13), (30, 14), (100, 15)]


def _timeout_code(seconds: float) -> int:
    for limit, code in _TMO_CODES:
        if seconds <= limit:
            return code
    return 16  # T300s


class Gpib32Transport(Transport):
    """Direct NI-488.2 API calls (ibdev/ibwrt/ibrd) through gpib-32.dll."""

    name = "GPIB-32 DLL"
    DLL_NAMES = ["gpib-32.dll", "ni4882.dll", "ke4882.dll"]

    def __init__(self, address: int, board: int = 0, timeout_s: float = 5.0, dll_path: str = "") -> None:
        super().__init__()
        self.dll = self._load_dll(dll_path)
        self._setup_prototypes()
        tmo = _timeout_code(timeout_s)
        # ibdev(board, pad, sad, tmo, eot, eos)  eos 0x140A = REOS|'\n'
        self.ud = self.dll.ibdev(board, address, 0, tmo, 1, 0x140A)
        if self.ud < 0:
            raise TransportError(
                f"ibdev failed for GPIB{board}::{address}. Is the KUSB-488A driver installed and the adapter plugged in?"
            )
        self.address = address
        self.board = board

    @classmethod
    def _load_dll(cls, dll_path: str):
        if sys.platform != "win32" and not dll_path:
            raise TransportError("The GPIB-32 DLL backend is only available on Windows.")
        names = [dll_path] if dll_path else cls.DLL_NAMES
        errors = []
        for n in names:
            try:
                return ctypes.windll.LoadLibrary(n) if sys.platform == "win32" else ctypes.CDLL(n)
            except OSError as e:
                errors.append(f"{n}: {e}")
        raise TransportError("No NI-488.2 compatible DLL found (" + "; ".join(errors) + ")")

    def _setup_prototypes(self) -> None:
        d = self.dll
        d.ibdev.argtypes = [ctypes.c_int] * 6
        d.ibdev.restype = ctypes.c_int
        d.ibwrt.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_long]
        d.ibwrt.restype = ctypes.c_int
        d.ibrd.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_long]
        d.ibrd.restype = ctypes.c_int
        d.ibonl.argtypes = [ctypes.c_int, ctypes.c_int]
        d.ibclr.argtypes = [ctypes.c_int]
        # number of bytes transferred by the last call
        self._cnt = None
        for fn in ("ThreadIbcntl", "ThreadIbcnt"):
            if hasattr(d, fn):
                self._cnt = getattr(d, fn)
                self._cnt.restype = ctypes.c_long
                break

    def _bytes_read(self, bufsize: int, buf) -> int:
        if self._cnt is not None:
            return int(self._cnt())
        try:
            return ctypes.c_long.in_dll(self.dll, "user_ibcnt").value
        except ValueError:
            raw = buf.raw
            return raw.find(b"\x00") if b"\x00" in raw else bufsize

    def _write(self, cmd: str) -> None:
        data = (cmd + "\n").encode("ascii")
        sta = self.dll.ibwrt(self.ud, data, len(data))
        if sta & _ERR:
            raise TransportError(f"ibwrt failed (ibsta=0x{sta & 0xFFFF:04X}){' timeout' if sta & _TIMO else ''}")

    def _read(self) -> str:
        size = 4096
        buf = ctypes.create_string_buffer(size)
        sta = self.dll.ibrd(self.ud, buf, size)
        if sta & _ERR:
            raise TransportError(f"ibrd failed (ibsta=0x{sta & 0xFFFF:04X}){' timeout' if sta & _TIMO else ''}")
        n = self._bytes_read(size, buf)
        return buf.raw[:n].decode("ascii", errors="replace")

    def clear(self) -> None:
        self.dll.ibclr(self.ud)

    def close(self) -> None:
        try:
            self.dll.ibonl(self.ud, 0)
        except Exception:
            pass

    @classmethod
    def scan(cls, board: int = 0, dll_path: str = "") -> list[tuple[int, str]]:
        """Try *IDN? on primary addresses 1..30 and return the ones that answer."""
        found = []
        for addr in range(1, 31):
            try:
                t = cls(addr, board, timeout_s=0.3, dll_path=dll_path)
                idn = t.query("*IDN?")
                t.close()
                if idn:
                    found.append((addr, idn))
            except TransportError:
                continue
            time.sleep(0.02)
        return found
