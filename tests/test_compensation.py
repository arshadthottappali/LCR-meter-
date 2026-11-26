import math

import pytest

from lcr_app.core.compensation import Compensation, to_complex, to_polar

FREQS = [100.0, 1e3, 10e3, 100e3, 1e6]


def fixture(f: float, zdut: complex) -> complex:
    """Fixture model: series lead impedance, then 12 pF stray across the clips."""
    w = 2 * math.pi * f
    zs = 0.05 + 1j * w * 30e-9
    yo = 1j * w * 12e-12 + 1e-10
    return zs + 1 / (yo + 1 / zdut) if zdut != 0 else zs


def make_cal() -> Compensation:
    c = Compensation(settings={"level_mode": "V", "level_value": 1.0}, freqs=list(FREQS))
    big = 1e30  # "nothing connected"
    c.open_z = [list(to_polar(fixture(f, big))) for f in FREQS]
    c.short_z = [list(to_polar(fixture(f, 0))) for f in FREQS]
    return c


@pytest.mark.parametrize("cap", [10e-12, 1e-9, 100e-9])
def test_recovers_capacitor(cap):
    c = make_cal()
    for i, f in enumerate(FREQS):
        dut = 1 / (1j * 2 * math.pi * f * cap)
        zabs, th = c.correct(i, f, *to_polar(fixture(f, dut)))
        assert to_complex(zabs, th) == pytest.approx(dut, rel=1e-6)


def test_recovers_small_resistor():
    c = make_cal()
    for i, f in enumerate(FREQS):
        zabs, th = c.correct(i, f, *to_polar(fixture(f, 0.5 + 0j)))
        assert to_complex(zabs, th) == pytest.approx(0.5 + 0j, abs=1e-6)


def test_open_overflow_and_short_only():
    c = make_cal()
    c.open_z[0] = [math.nan, math.nan]  # meter overflow on open at low f
    zabs, th = c.correct(0, FREQS[0], 1000.0, 0.0)
    assert math.isfinite(zabs)
    c.open_z = []  # short only
    assert c.describe() == "short"
    zs = to_complex(*c.short_z[1])
    zabs, th = c.correct(1, FREQS[1], *to_polar(100 + zs))
    assert to_complex(zabs, th) == pytest.approx(100 + 0j, abs=1e-9)


def test_mismatch_and_wrong_frequency():
    c = make_cal()
    assert c.mismatch(FREQS, {"level_mode": "V", "level_value": 1.0}) == []
    assert len(c.mismatch(FREQS[:3], {"level_mode": "V", "level_value": 0.5})) == 2
    with pytest.raises(ValueError):
        c.correct(1, 2000.0, 1.0, 0.0)


def test_save_load_roundtrip(tmp_path):
    c = make_cal()
    c.open_z[0] = [math.nan, math.nan]
    back = Compensation.load(c.save(tmp_path / "cal.json"))
    assert back.complete and back.freqs == FREQS
    assert math.isnan(back.open_z[0][0])
    assert back.short_z[2] == pytest.approx(c.short_z[2])
