import math

import pytest

from lcr_app.core.params import derive, parse_si
from lcr_app.core.storage import Run, load_csv, save_csv
from lcr_app.core.sweep import SweepConfig, SweepRunner
from lcr_app.instrument.hioki3532 import Hioki3532, MeasureSettings
from lcr_app.instrument.simulator import SimulatedTransport


def test_pure_capacitor():
    f, c = 1000.0, 100e-9
    z = 1 / (2 * math.pi * f * c)
    p = derive(z, -90.0, f)
    assert p["Cs"] == pytest.approx(c, rel=1e-9)
    assert p["Cp"] == pytest.approx(c, rel=1e-9)
    assert abs(p["Rs"]) < 1e-9 * z


def test_series_rl():
    f, r, l = 10e3, 5.0, 1e-3
    zc = complex(r, 2 * math.pi * f * l)
    p = derive(abs(zc), math.degrees(math.atan2(zc.imag, zc.real)), f)
    assert p["Rs"] == pytest.approx(r)
    assert p["Ls"] == pytest.approx(l)
    assert p["Q"] == pytest.approx(2 * math.pi * f * l / r)
    assert p["D"] == pytest.approx(1 / p["Q"])
    # parallel equivalent
    y = 1 / zc
    assert p["G"] == pytest.approx(y.real)
    assert p["Rp"] == pytest.approx(1 / y.real)
    assert p["Lp"] == pytest.approx(-1 / (2 * math.pi * f * y.imag))


def test_nan_safe():
    p = derive(float("nan"), 10, 1000)
    assert math.isnan(p["Cs"])


@pytest.mark.parametrize("txt,val", [("1k", 1e3), ("2.5M", 2.5e6), ("100m", 0.1), ("10u", 1e-5),
                                     ("1e3", 1e3), ("42", 42), ("1 kHz", 1e3), ("10 µA", 1e-5)])
def test_parse_si(txt, val):
    assert parse_si(txt) == pytest.approx(val)


def test_frequencies():
    cfg = SweepConfig(start_hz=100, stop_hz=1e6, points=5, spacing="log")
    assert cfg.frequencies() == [100, 1000, 10000, 100000, 1e6]
    cfg = SweepConfig(start_hz=1e3, stop_hz=2e3, points=3, spacing="linear")
    assert cfg.frequencies() == [1000, 1500, 2000]


def test_sweep_with_simulator(tmp_path):
    t = SimulatedTransport(realtime=False, noise=0)
    meter = Hioki3532(t)
    assert "3532" in meter.idn()
    cfg = SweepConfig(start_hz=100, stop_hz=100e3, points=7, settle_ms=0, readings_per_point=2,
                      meas=MeasureSettings())
    pts = SweepRunner(meter, cfg).run()
    assert len(pts) == 7
    for p in pts[:4]:  # well below self resonance: ~100 nF
        assert p.values["Cs"] == pytest.approx(100e-9, rel=0.05)
    run = Run("s", [p.as_row() for p in pts], {"sample": "s"})
    path = save_csv(run, tmp_path / "x.csv")
    back = load_csv(path)
    assert len(back.rows) == 7
    assert back.rows[3]["Cs"] == pytest.approx(pts[3].values["Cs"], rel=1e-6)


def test_overflow_with_several_readings_per_point():
    """Open fixture at low frequency: the meter overflows (NaN). With readings_per_point > 1
    this used to crash with 'inf or nan encountered in data' (statistics.stdev)."""

    class Overflowing(Hioki3532):
        calls = 0

        def measure_z_theta(self):
            Overflowing.calls += 1
            if self.t.freq < 1000:  # always overflow
                return math.nan, math.nan
            if Overflowing.calls % 3 == 0:  # occasional overflow near the range limit
                return math.nan, math.nan
            return 1e6, -89.0

    meter = Overflowing(SimulatedTransport(realtime=False))
    cfg = SweepConfig(start_hz=100, stop_hz=100e3, points=5, settle_ms=0, readings_per_point=3,
                      meas=MeasureSettings())
    pts = SweepRunner(meter, cfg).run()
    assert len(pts) == 5
    assert math.isnan(pts[0].z_raw) and math.isnan(pts[0].z_std)
    assert pts[-1].z_raw == pytest.approx(1e6)


def test_verify_profile_on_simulator():
    meter = Hioki3532(SimulatedTransport(realtime=False))
    res = meter.verify_profile(MeasureSettings())
    assert all(ok for _, _, ok in res), res


@pytest.mark.parametrize("req,actual", [(42.0, 42.0), (53.06, 53.1), (999.96, 1000.0), (1234.5, 1235.0), (1234.4, 1234.0),
                                        (23128.0, 23130.0), (189570.0, 189600.0), (1229900.0, 1230000.0),
                                        (5e6, 5e6)])
def test_meter_frequency_resolution(req, actual):
    from lcr_app.core.sweep import meter_frequency

    assert meter_frequency(req) == actual


def test_sweep_frequencies_are_settable_and_unique():
    from lcr_app.core.sweep import meter_frequency

    cfg = SweepConfig(start_hz=42, stop_hz=5e6, points=201, spacing="log")
    f = cfg.frequencies()
    assert f == [meter_frequency(x) for x in f] and len(f) == len(set(f))
    assert f[0] == 42 and f[-1] == 5e6


def test_configure_rejects_unsupported_averaging():
    meter = Hioki3532(SimulatedTransport(realtime=False))
    with pytest.raises(ValueError):
        meter.configure(MeasureSettings(averaging=3))
    meter.configure(MeasureSettings(averaging=16))  # supported value works


def test_configure_stops_when_meter_rejects_a_setting():
    from lcr_app.instrument.transport import TransportError

    meter = Hioki3532(SimulatedTransport(realtime=False))  # gets its own copy of the profile
    meter.p["set_speed"] = ":BOGUS {speed}"  # simulator flags unknown commands as command errors
    with pytest.raises(TransportError, match="rejected"):
        meter.configure(MeasureSettings())
