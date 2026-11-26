"""Deterministic dielectric analysis (no AI involved).

From the measured impedance and the sample geometry this computes
    ε′, ε″, tan δ, σ′ (ac conductivity), M′, M″ (electric modulus)
and fits
    * Havriliak–Negami / Cole–Cole relaxation + dc conductivity term
    * Jonscher universal power law  σ′(ω) = σ_dc + A·ωⁿ
It also runs data-quality checks that matter for thin films
(electrode polarisation, electrode series resistance, stray capacitance,
leakage, short circuits, instrument range).

All results are plain dicts so they can be cached as JSON, put into the
report and summarised for the AI.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from ..core.storage import Run
from .sample import EPS0, SampleInfo

STANDARD_FREQS = [100.0, 1e3, 1e4, 1e5, 1e6]
K_B_EV = 8.617333262e-5  # Boltzmann constant in eV/K
ANALYSIS_VERSION = 2


# ----------------------------------------------------------------- helpers
def _arr(run: Run, key: str) -> np.ndarray:
    return np.array(run.column(key), dtype=float)


def _fmt(v: float, unit: str = "", digits: int = 4) -> str:
    from ..core.params import si_format

    return si_format(v, unit, digits)


def _interp_log(f: np.ndarray, y: np.ndarray, f0: float, log_y: bool = True) -> float:
    ok = np.isfinite(f) & np.isfinite(y) & (f > 0)
    if log_y:
        ok &= y > 0
    if ok.sum() < 2 or not (f[ok].min() * 0.999 <= f0 <= f[ok].max() * 1.001):
        return math.nan
    x = np.log10(f[ok])
    yy = np.log10(y[ok]) if log_y else y[ok]
    order = np.argsort(x)
    v = float(np.interp(math.log10(f0), x[order], yy[order]))
    return 10 ** v if log_y else v


def _slope(f: np.ndarray, y: np.ndarray, fmin: float, fmax: float) -> float:
    """d log y / d log f over [fmin, fmax]."""
    ok = (f >= fmin) & (f <= fmax) & np.isfinite(y) & (y > 0)
    if ok.sum() < 3:
        return math.nan
    return float(np.polyfit(np.log10(f[ok]), np.log10(y[ok]), 1)[0])


def _r2_log(y: np.ndarray, yfit: np.ndarray) -> float:
    ok = np.isfinite(y) & np.isfinite(yfit) & (y > 0) & (yfit > 0)
    if ok.sum() < 3:
        return math.nan
    a, b = np.log10(y[ok]), np.log10(yfit[ok])
    ss_res = float(np.sum((a - b) ** 2))
    ss_tot = float(np.sum((a - a.mean()) ** 2))
    return 1 - ss_res / ss_tot if ss_tot > 0 else math.nan


def _clean(obj):
    """Make results JSON-safe (NaN -> None, numpy -> python)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


def run_temperature(run: Run, sample: SampleInfo) -> float:
    """Temperature of a run in °C: per-run value first, then sample default."""
    for key in ("temperature_c", "temperature"):
        v = run.meta.get(key)
        try:
            return float(str(v).replace("°C", "").replace("C", "").strip())
        except (TypeError, ValueError):
            continue
    return sample.num("temperature_c")


# ------------------------------------------------------------------ models
def _hn_eps_parts(w, p):
    eps_inf, d_eps, tau, a, b, sigma = p
    z = eps_inf + d_eps / (1 + (1j * w * tau) ** a) ** b
    e1 = z.real
    e2 = -z.imag + sigma / (EPS0 * w)
    return e1, e2


def fit_hn(f: np.ndarray, e1: np.ndarray, e2: np.ndarray, cole_cole: bool = False) -> dict | None:
    from scipy.optimize import least_squares

    ok = np.isfinite(f) & np.isfinite(e1) & np.isfinite(e2) & (e1 > 0) & (e2 > 0)
    if ok.sum() < 8:
        return None
    f, e1, e2 = f[ok], e1[ok], e2[ok]
    w = 2 * np.pi * f

    def unpack(x):
        eps_inf, d_eps, tau, a = 10 ** x[0], 10 ** x[1], 10 ** x[2], x[3]
        b = 1.0 if cole_cole else x[4]
        sigma = 10 ** x[-1]
        return eps_inf, d_eps, tau, a, b, sigma

    def resid(x):
        m1, m2 = _hn_eps_parts(w, unpack(x))
        if np.any(m1 <= 0) or np.any(m2 <= 0):
            return np.full(2 * len(w), 10.0)
        return np.concatenate([np.log10(m1 / e1), np.log10(m2 / e2)])

    e_hi = max(float(np.min(e1)) * 0.9, 1e-3)
    d0 = max(float(np.max(e1) - np.min(e1)), 0.05 * e_hi)
    sig0 = max(float(np.min(e2 * EPS0 * w)) * 0.5, 1e-20)
    best = None
    lo_t, hi_t = -math.log10(w.max()) - 2, -math.log10(w.min()) + 2
    for log_tau in np.linspace(lo_t, hi_t, 7):
        x0 = [math.log10(e_hi), math.log10(d0), log_tau, 0.8] + ([] if cole_cole else [0.8]) + [math.log10(sig0)]
        lb = [-3, -4, lo_t - 1, 0.05] + ([] if cole_cole else [0.05]) + [-22]
        ub = [6, 7, hi_t + 1, 1.0] + ([] if cole_cole else [1.0]) + [2]
        try:
            r = least_squares(resid, x0, bounds=(lb, ub), max_nfev=3000)
        except ValueError:
            continue
        if best is None or r.cost < best.cost:
            best = r
    if best is None:
        return None
    p = unpack(best.x)
    m1, m2 = _hn_eps_parts(w, p)
    eps_inf, d_eps, tau, a, b, sigma = p
    # peak frequency of the HN loss (exact expression)
    s1 = math.sin(math.pi * a * b / (2 * (b + 1)))
    s2 = math.sin(math.pi * a / (2 * (b + 1)))
    f_peak = (1 / (2 * math.pi * tau)) * ((s1 / s2) ** (1 / a) if s2 > 0 else 1.0)
    in_window = f.min() * 1.5 <= f_peak <= f.max() / 1.5
    significant = d_eps > 0.05 * eps_inf
    r2 = (_r2_log(e1, m1) + _r2_log(e2, m2)) / 2
    return {
        "model": "Cole–Cole" if cole_cole else "Havriliak–Negami",
        "eps_inf": eps_inf, "delta_eps": d_eps, "tau_s": tau, "f_peak_Hz": f_peak,
        "alpha_shape_a": a, "beta_shape_b": b, "sigma_dc_S_per_m": sigma,
        "r2_log": r2, "relaxation_in_window": bool(in_window and significant),
        "f_fit": f.tolist(), "eps1_fit": m1.tolist(), "eps2_fit": m2.tolist(),
    }


def fit_jonscher(f: np.ndarray, sigma: np.ndarray) -> dict | None:
    from scipy.optimize import least_squares

    ok = np.isfinite(f) & np.isfinite(sigma) & (sigma > 0)
    if ok.sum() < 6:
        return None
    f, s = f[ok], sigma[ok]
    w = 2 * np.pi * f

    def model(x):
        return 10 ** x[0] + 10 ** x[1] * w ** x[2]

    def resid(x):
        return np.log10(model(x) / s)

    n0 = max(min(_slope(f, s, f.max() / 30, f.max()), 1.2), 0.1) if f.max() / f.min() > 30 else 0.8
    x0 = [math.log10(s.min()), math.log10(s.max() / w.max() ** n0), n0]
    try:
        r = least_squares(resid, x0, bounds=([-25, -40, 0.0], [5, 5, 1.5]), max_nfev=3000)
    except ValueError:
        return None
    sdc, A, n = 10 ** r.x[0], 10 ** r.x[1], r.x[2]
    fit = model(r.x)
    # σ_dc is only resolved if the plateau is visible (σ_dc dominates at f_min)
    dc_resolved = sdc > 0.5 * float(A * w.min() ** n)
    r2 = _r2_log(s, fit)
    return {"sigma_dc_S_per_m": sdc if dc_resolved else None,
            "sigma_dc_upper_limit_S_per_m": None if dc_resolved else float(s[np.argmin(f)]),
            "A": A, "n": n, "r2_log": r2, "dc_plateau_resolved": bool(dc_resolved), "good_fit": bool(r2 > 0.95),
            "f_fit": f.tolist(), "sigma_fit": fit.tolist()}


def find_peaks_log(f: np.ndarray, y: np.ndarray, name: str) -> list[dict]:
    from scipy.signal import find_peaks

    ok = np.isfinite(f) & np.isfinite(y) & (y > 0)
    if ok.sum() < 7:
        return []
    order = np.argsort(f[ok])
    ff, yy = f[ok][order], np.log10(y[ok][order])
    if len(yy) >= 7:  # light smoothing
        yy = np.convolve(np.pad(yy, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    idx, props = find_peaks(yy, prominence=0.04)
    return [{"quantity": name, "f_peak_Hz": float(ff[i]), "value": float(10 ** yy[i]),
             "prominence_decades": float(props["prominences"][k])}
            for k, i in enumerate(idx) if 0 < i < len(ff) - 1]


# ----------------------------------------------------------------- analysis
def analysis_key(run: Run, sample: SampleInfo) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(run.rows, sort_keys=True, default=str).encode())
    geo = {k: sample.values.get(k) for k in ("sample_type", "electrode_shape", "electrode_diameter", "electrode_length",
                                              "electrode_width", "electrode_area", "thickness", "empty_capacitance",
                                              "stray_capacitance", "thickness_uncertainty", "temperature_c")}
    h.update(json.dumps(geo, sort_keys=True, default=str).encode())
    h.update(json.dumps(run.meta.get("temperature_c"), default=str).encode())
    h.update(str(ANALYSIS_VERSION).encode())
    return h.hexdigest()[:16]


def analyze_run(run: Run, sample: SampleInfo, cache_dir: str | Path | None = None) -> dict:
    """Full analysis of one run; cached on disk by a hash of data + geometry."""
    key = analysis_key(run, sample)
    if cache_dir:
        cp = Path(cache_dir) / f"analysis_{key}.json"
        if cp.exists():
            try:
                return json.loads(cp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    res = _analyze(run, sample)
    res["cache_key"] = key
    res = _clean(res)
    if cache_dir:
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            (Path(cache_dir) / f"analysis_{key}.json").write_text(json.dumps(res), encoding="utf-8")
        except OSError:
            pass
    return res


def _analyze(run: Run, sample: SampleInfo) -> dict:
    f = _arr(run, "freq_Hz")
    order = np.argsort(f)
    f = f[order]
    Z, th = _arr(run, "Z")[order], _arr(run, "theta")[order]
    Cp, Cs, D = _arr(run, "Cp")[order], _arr(run, "Cs")[order], _arr(run, "D")[order]
    G, B, Rs = _arr(run, "G")[order], _arr(run, "B")[order], _arr(run, "Rs")[order]
    w = 2 * np.pi * f
    c0 = sample.geometric_capacitance()
    has_eps = bool(c0 > 0)

    res: dict = {
        "run": run.name, "n_points": int(len(f)), "f_min_Hz": float(np.nanmin(f)) if len(f) else math.nan,
        "f_max_Hz": float(np.nanmax(f)) if len(f) else math.nan, "c0_F": c0, "has_permittivity": has_eps,
        "temperature_c": run_temperature(run, sample), "geometry": sample.geometry_text(),
        "sample_type": sample.sample_type,
    }
    arrays = {"f": f, "Z": Z, "theta": th, "Cp": Cp, "D": D}
    if has_eps:
        e1 = B / (w * c0)
        e2 = G / (w * c0)
        tan_d = e2 / e1
        sigma = w * EPS0 * e2
        denom = e1 ** 2 + e2 ** 2
        m1, m2 = e1 / denom, e2 / denom
        arrays.update(eps1=e1, eps2=e2, tan_d=tan_d, sigma=sigma, M1=m1, M2=m2)
        unc = sample.num("thickness_uncertainty")
        res["eps_relative_uncertainty_pct"] = unc if unc > 0 else None
    else:
        arrays.update(tan_d=D, sigma=G)  # conductance instead of conductivity when no geometry
    res["arrays"] = arrays

    # values at standard frequencies
    table = []
    for f0 in STANDARD_FREQS:
        row = {"f_Hz": f0, "Cp_F": _interp_log(f, Cp, f0), "D": _interp_log(f, D, f0),
               "Z_ohm": _interp_log(f, Z, f0), "theta_deg": _interp_log(f, th, f0, log_y=False)}
        if has_eps:
            row.update(eps1=_interp_log(f, arrays["eps1"], f0), eps2=_interp_log(f, arrays["eps2"], f0),
                       tan_d=_interp_log(f, arrays["tan_d"], f0), sigma_S_per_m=_interp_log(f, arrays["sigma"], f0))
        if not math.isnan(row["Cp_F"]):
            table.append(row)
    res["at_standard_freqs"] = table

    # peaks
    peaks = find_peaks_log(f, arrays["tan_d"], "tan δ")
    if has_eps:
        peaks += find_peaks_log(f, arrays["eps2"], "ε″") + find_peaks_log(f, arrays["M2"], "M″")
    res["peaks"] = peaks

    # data-quality checks
    checks = quality_checks(f, Z, th, Cp, Cs, D, sample, arrays if has_eps else None)
    res["checks"] = checks
    ep_end = next((c.get("f_limit_Hz") for c in checks if c["code"] == "electrode_polarization"), None)

    # fits
    if has_eps:
        sel = f > (ep_end or 0)
        res["relaxation_fit"] = fit_hn(f[sel], arrays["eps1"][sel], arrays["eps2"][sel])
        cc = fit_hn(f[sel], arrays["eps1"][sel], arrays["eps2"][sel], cole_cole=True)
        if cc and res["relaxation_fit"] and cc["r2_log"] >= res["relaxation_fit"]["r2_log"] - 0.002:
            res["relaxation_fit"] = cc  # simpler model is as good -> prefer it
        res["jonscher_fit"] = fit_jonscher(f, arrays["sigma"])
    else:
        res["relaxation_fit"] = None
        res["jonscher_fit"] = None
    if not has_eps:
        res["checks"].append({"level": "warn", "code": "no_geometry",
                              "message": "Sample geometry is incomplete, so permittivity (ε′, ε″), conductivity and "
                                         "the relaxation fits could not be calculated. Only Cp, D and |Z| are reported."})
    res["arrays"] = {k: np.asarray(v).tolist() for k, v in arrays.items()}
    return res


def quality_checks(f, Z, th, Cp, Cs, D, sample: SampleInfo, eps: dict | None) -> list[dict]:
    out: list[dict] = []
    if len(f) < 3:
        return [{"level": "warn", "code": "few_points", "message": "Too few points for an analysis."}]
    fmin, fmax = float(np.nanmin(f)), float(np.nanmax(f))

    def add(level, code, msg, **kw):
        out.append({"level": level, "code": code, "message": msg, **kw})

    # short circuit / resistive sample
    med_th = float(np.nanmedian(np.abs(th)))
    if med_th < 10:
        add("warn", "resistive", f"The phase is close to 0° (median |θ| = {med_th:.1f}°): the sample behaves like a "
            "resistor. For a dielectric film this usually means a short circuit through pinholes, or a very "
            "conductive material.")
    # leakage at low frequency
    th_low = float(np.nanmean(th[f <= fmin * 3]))
    if -80 < th_low < -10:
        add("info", "low_f_conduction", f"At the lowest frequencies θ ≈ {th_low:.1f}° (not −90°): dc conduction / "
            "leakage contributes to the loss there. This is common for organic films and shows up as rising ε″ and "
            "tan δ at low frequency.")
    # inductive behaviour
    if np.nanmax(th) > 0:
        f_ind = float(f[np.nanargmax(th > 0)])
        add("warn", "inductive", f"θ becomes positive above ≈ {_fmt(f_ind, 'Hz')}: the measurement is inductive "
            "(cable/fixture inductance or resonance). Data above this frequency does not describe the film.",
            f_limit_Hz=f_ind)
    # instrument range
    if np.nanmax(Z) > 50e6:
        add("warn", "high_impedance", f"|Z| reaches {_fmt(float(np.nanmax(Z)), 'Ω')} — close to the upper limit of "
            "the 3532-50 (accuracy degrades above ~10 MΩ, especially at low frequency). Use SLOW2 and averaging, "
            "or a larger electrode area.")
    if np.nanmin(Z) < 0.1:
        add("warn", "low_impedance", f"|Z| is as low as {_fmt(float(np.nanmin(Z)), 'Ω')}: short-circuit "
            "compensation and cable resistance dominate the accuracy.")
    # stray capacitance
    cstray = sample.num("stray_capacitance")
    cmed = float(np.nanmedian(Cp[Cp > 0])) if np.any(Cp > 0) else math.nan
    if cstray > 0 and cmed > 0:
        ratio = cmed / cstray
        if ratio < 10:
            add("warn", "stray", f"Sample capacitance (≈ {_fmt(cmed, 'F')}) is only {ratio:.1f}× the open-circuit "
                f"capacitance ({_fmt(cstray, 'F')}). ε′ may be overestimated; subtract the stray value or use a "
                "larger electrode.")
    elif cmed > 0 and cmed < 20e-12 and sample.sample_type in ("sandwich", "pellet"):
        add("info", "stray_unknown", f"The sample capacitance is small (≈ {_fmt(cmed, 'F')}). Enter the "
            "open-circuit capacitance with the probes lifted so the stray contribution can be judged.")
    if sample.get("open_short_done") in (None, "No", "Unknown"):
        add("info", "compensation", "Open/short compensation not confirmed. Without it the fixture adds stray "
            "capacitance (affects thick films) and series resistance/inductance (affects thin films at high f).")
    # electrode series resistance (thin films on ITO etc.)
    top = f >= fmax / 10
    s_d = _slope(f, D, fmax / 10, fmax)
    if top.sum() >= 3 and s_d > 0.5 and np.nanmax(D[top]) > 0.05:
        i = int(np.nanargmax(f))
        rs = float(D[i] / (2 * np.pi * f[i] * Cs[i])) if Cs[i] > 0 else math.nan
        fc = 1 / (2 * np.pi * rs * Cs[i]) if rs > 0 else math.nan
        add("warn", "series_resistance", f"D rises ∝ f^{s_d:.2f} in the top decade: a series resistance of "
            f"≈ {_fmt(rs, 'Ω')} (electrodes, e.g. ITO, contacts, cables) limits the response. RC corner "
            f"≈ {_fmt(fc, 'Hz')}; ε′/ε″ above ≈ {_fmt(fc / 10, 'Hz')} are not intrinsic to the film.",
            Rs_ohm=rs, f_corner_Hz=fc, f_limit_Hz=fc / 10)
    if eps is not None:
        e1, e2 = eps["eps1"], eps["eps2"]
        # electrode polarisation
        if fmax / fmin > 30:
            s_low = _slope(f, e1, fmin, fmin * 10)
            td_low = float(np.nanmedian(eps["tan_d"][f <= fmin * 3]))
            if s_low < -0.25 and td_low > 0.3:
                # where does the steep rise stop?
                fl = fmin * 10
                for k in range(len(f) - 3):
                    if f[k] < fmin:
                        continue
                    if _slope(f, e1, f[k], f[k] * 3) > -0.1:
                        fl = float(f[k])
                        break
                add("warn", "electrode_polarization", f"ε′ increases steeply towards low frequency (slope "
                    f"{s_low:.2f} per decade) together with high tan δ (≈ {td_low:.2g}): typical of electrode "
                    f"polarisation / interfacial (Maxwell–Wagner) effects below ≈ {_fmt(fl, 'Hz')}. These values "
                    "are not the bulk permittivity; the relaxation fit starts above this frequency.",
                    f_limit_Hz=fl)
        e1_mid = _interp_log(f, e1, 1e4) if fmin <= 1e4 <= fmax else float(np.nanmedian(e1))
        if np.nanmin(e1) < 1:
            add("warn", "eps_below_1", "ε′ < 1 somewhere — physically impossible for a material. Check thickness "
                "and area (units!), stray capacitance and compensation.")
        if e1_mid > 1e4:
            add("warn", "giant_eps", f"ε′ ≈ {e1_mid:.3g} is extremely high. Unless the material is a known "
                "high-κ ferroelectric, this usually indicates interfacial polarisation, a conductive film or a wrong "
                "thickness.")
        unc = sample.num("thickness_uncertainty")
        if unc > 0:
            add("info", "uncertainty", f"Thickness uncertainty of {unc:g} % gives the same relative uncertainty "
                "in ε′ and ε″ (they scale with d).")
    return out


def arrhenius(points: list[tuple[float, float]]) -> dict | None:
    """Fit value = A·exp(−Ea/kT) to (temperature °C, value) pairs. Needs ≥ 3 temperatures."""
    pts = [(t + 273.15, v) for t, v in points if t is not None and not math.isnan(t) and v and v > 0]
    temps = {round(t, 2) for t, _ in pts}
    if len(temps) < 3:
        return None
    x = np.array([1 / t for t, _ in pts])
    y = np.log(np.array([v for _, v in pts]))
    slope, icpt = np.polyfit(x, y, 1)
    yfit = slope * x + icpt
    r2 = 1 - np.sum((y - yfit) ** 2) / np.sum((y - y.mean()) ** 2)
    return {"Ea_eV": float(-slope * K_B_EV), "prefactor": float(math.exp(icpt)), "r2": float(r2),
            "n_temperatures": len(temps)}


def temperature_series(results: list[dict]) -> dict:
    """Arrhenius analysis across runs (σ_dc and relaxation peak frequency)."""
    out = {}
    sig = [(r.get("temperature_c"), (r.get("jonscher_fit") or {}).get("sigma_dc_S_per_m"))
           for r in results if (r.get("jonscher_fit") or {}).get("dc_plateau_resolved")]
    fp = [(r.get("temperature_c"), (r.get("relaxation_fit") or {}).get("f_peak_Hz"))
          for r in results if (r.get("relaxation_fit") or {}).get("relaxation_in_window")]
    a = arrhenius([p for p in sig if p[1]])
    if a:
        out["sigma_dc"] = a
    b = arrhenius([p for p in fp if p[1]])
    if b:
        out["relaxation_frequency"] = b
    return out


# ------------------------------------------------------------ summaries
def summary_text(res: dict) -> str:
    """Compact, numbers-only summary of one run (this is what the AI sees)."""
    L = [f"Run '{res['run']}': {res['n_points']} points, {_fmt(res['f_min_Hz'], 'Hz')} – {_fmt(res['f_max_Hz'], 'Hz')}, "
         f"temperature {res.get('temperature_c') if res.get('temperature_c') is not None else 'not given'} °C."]
    L.append(f"Geometry: {res.get('geometry')}.")
    for r in res.get("at_standard_freqs", []):
        s = f"  f = {_fmt(r['f_Hz'], 'Hz')}: Cp = {_fmt(r['Cp_F'], 'F')}, D = {r['D']:.4g}"
        if r.get("eps1") is not None:
            s += (f", ε′ = {r['eps1']:.4g}, ε″ = {r['eps2']:.4g}, tan δ = {r['tan_d']:.4g}, "
                  f"σ′ = {r['sigma_S_per_m']:.3g} S/m")
        L.append(s)
    for p in res.get("peaks", []):
        L.append(f"  Loss peak in {p['quantity']} at {_fmt(p['f_peak_Hz'], 'Hz')} (value {p['value']:.3g}).")
    rf = res.get("relaxation_fit")
    if rf:
        if rf.get("relaxation_in_window"):
            L.append(f"  {rf['model']} fit: ε∞ = {rf['eps_inf']:.3g}, Δε = {rf['delta_eps']:.3g}, "
                     f"f_peak = {_fmt(rf['f_peak_Hz'], 'Hz')} (τ = {rf['tau_s']:.3g} s), a = {rf['alpha_shape_a']:.2f}, "
                     f"b = {rf['beta_shape_b']:.2f}, σ_dc = {rf['sigma_dc_S_per_m']:.3g} S/m, R²(log) = {rf['r2_log']:.3f}.")
        else:
            L.append("  No dielectric relaxation peak inside the measured frequency window.")
    jf = res.get("jonscher_fit")
    if jf and not jf.get("good_fit"):
        L.append(f"  Jonscher power law does not describe σ′(f) well (R²(log) = {jf['r2_log']:.3f}); not used.")
    elif jf:
        sdc = (f"σ_dc = {jf['sigma_dc_S_per_m']:.3g} S/m" if jf["dc_plateau_resolved"]
               else f"σ_dc < {jf['sigma_dc_upper_limit_S_per_m']:.3g} S/m (dc plateau not reached)")
        L.append(f"  Jonscher fit σ′ = σ_dc + A·ωⁿ: {sdc}, n = {jf['n']:.3f}, R²(log) = {jf['r2_log']:.3f}.")
    for c in res.get("checks", []):
        L.append(f"  [{c['level']}] {c['message']}")
    return "\n".join(L)
