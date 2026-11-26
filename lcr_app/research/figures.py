"""Static figures for the report (matplotlib, no GUI needed)."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000"]
LIT_MARKERS = ["s", "^", "D", "v", "P", "X", "*", "h"]

plt.rcParams.update({
    "figure.dpi": 150, "font.size": 9, "axes.grid": True, "grid.alpha": 0.3, "axes.spines.top": False,
    "axes.spines.right": False, "legend.frameon": False, "legend.fontsize": 8,
})


def _a(res: dict, key: str) -> np.ndarray:
    return np.asarray(res["arrays"].get(key, []), dtype=float)


def _pos(x, y):
    ok = np.isfinite(x) & np.isfinite(y) & (y > 0)
    return x[ok], y[ok]


def make_figures(results: list[dict], out_dir: str | Path, literature_points: list[dict] | None = None) -> list[dict]:
    """Create all figures. Returns [{'file': name, 'caption': text}].

    literature_points: [{'ref': '[3]', 'f_Hz': 1e3, 'eps1': 7.5, 'tan_d': 0.02}, ...]
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    figs = []
    has_eps = any(r.get("has_permittivity") for r in results)
    lit = literature_points or []

    if has_eps:
        # 1. permittivity
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.0))
        for i, r in enumerate(results):
            if not r.get("has_permittivity"):
                continue
            c = PALETTE[i % len(PALETTE)]
            f = _a(r, "f")
            ax1.loglog(*_pos(f, _a(r, "eps1")), "o", ms=3, color=c, label=r["run"])
            ax2.loglog(*_pos(f, _a(r, "eps2")), "o", ms=3, color=c, label=r["run"])
            rf = r.get("relaxation_fit")
            if rf and rf.get("relaxation_in_window"):
                ff = np.asarray(rf["f_fit"])
                ax1.loglog(ff, rf["eps1_fit"], "-", color=c, lw=1, alpha=0.8)
                ax2.loglog(ff, rf["eps2_fit"], "-", color=c, lw=1, alpha=0.8, label=f"{rf['model']} fit")
        for k, p in enumerate([p for p in lit if p.get("eps1")]):
            ax1.loglog(p["f_Hz"], p["eps1"], LIT_MARKERS[k % len(LIT_MARKERS)], mfc="none", mec="k", ms=7,
                       label=f"lit. {p['ref']}")
        ax1.set(xlabel="Frequency (Hz)", ylabel="ε′", title="Real permittivity")
        ax2.set(xlabel="Frequency (Hz)", ylabel="ε″", title="Imaginary permittivity")
        ax1.legend()
        ax2.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "fig_permittivity.png")
        plt.close(fig)
        figs.append({"file": "fig_permittivity.png",
                     "caption": "Real (ε′) and imaginary (ε″) permittivity versus frequency. Lines: relaxation fit; "
                                "open black markers: values reported in the literature (reference number)."})

        # 2. loss tangent and modulus
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.0))
        for i, r in enumerate(results):
            if not r.get("has_permittivity"):
                continue
            c = PALETTE[i % len(PALETTE)]
            f = _a(r, "f")
            ax1.loglog(*_pos(f, _a(r, "tan_d")), "o-", ms=3, lw=0.8, color=c, label=r["run"])
            ax2.semilogx(*_pos(f, _a(r, "M2")), "o-", ms=3, lw=0.8, color=c, label=r["run"])
        for k, p in enumerate([p for p in lit if p.get("tan_d")]):
            ax1.loglog(p["f_Hz"], p["tan_d"], LIT_MARKERS[k % len(LIT_MARKERS)], mfc="none", mec="k", ms=7,
                       label=f"lit. {p['ref']}")
        ax1.set(xlabel="Frequency (Hz)", ylabel="tan δ", title="Loss tangent")
        ax2.set(xlabel="Frequency (Hz)", ylabel="M″", title="Electric modulus (imaginary)")
        ax1.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "fig_loss_modulus.png")
        plt.close(fig)
        figs.append({"file": "fig_loss_modulus.png",
                     "caption": "Loss tangent tan δ = ε″/ε′ and imaginary electric modulus M″. A peak in M″ at higher "
                                "frequency than in ε″ is typical of conduction-related relaxation."})

        # 3. conductivity
        fig, ax = plt.subplots(figsize=(3.6, 3.0))
        for i, r in enumerate(results):
            if not r.get("has_permittivity"):
                continue
            c = PALETTE[i % len(PALETTE)]
            ax.loglog(*_pos(_a(r, "f"), _a(r, "sigma")), "o", ms=3, color=c, label=r["run"])
            jf = r.get("jonscher_fit")
            if jf and jf.get("good_fit"):
                ax.loglog(jf["f_fit"], jf["sigma_fit"], "-", lw=1, color=c, label=f"Jonscher, n = {jf['n']:.2f}")
        ax.set(xlabel="Frequency (Hz)", ylabel="σ′ (S/m)", title="AC conductivity")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "fig_conductivity.png")
        plt.close(fig)
        figs.append({"file": "fig_conductivity.png",
                     "caption": "Real part of the ac conductivity σ′ = ωε₀ε″ with the Jonscher power-law fit "
                                "(shown only when it describes the data, R² > 0.95)."})

        # 4. Cole–Cole plot
        fig, ax = plt.subplots(figsize=(3.6, 3.0))
        for i, r in enumerate(results):
            if not r.get("has_permittivity"):
                continue
            c = PALETTE[i % len(PALETTE)]
            ax.plot(_a(r, "eps1"), _a(r, "eps2"), "o", ms=3, color=c, label=r["run"])
            rf = r.get("relaxation_fit")
            if rf and rf.get("relaxation_in_window"):
                ax.plot(rf["eps1_fit"], rf["eps2_fit"], "-", lw=1, color=c)
        ax.set(xlabel="ε′", ylabel="ε″", title="Cole–Cole plot")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "fig_colecole.png")
        plt.close(fig)
        figs.append({"file": "fig_colecole.png",
                     "caption": "Cole–Cole representation ε″ vs ε′. A symmetric depressed arc indicates a "
                                "distribution of relaxation times (Cole–Cole a < 1)."})
    else:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.0))
        for i, r in enumerate(results):
            c = PALETTE[i % len(PALETTE)]
            f = _a(r, "f")
            ax1.loglog(*_pos(f, _a(r, "Cp")), "o", ms=3, color=c, label=r["run"])
            ax2.loglog(*_pos(f, _a(r, "D")), "o", ms=3, color=c, label=r["run"])
        ax1.set(xlabel="Frequency (Hz)", ylabel="Cp (F)", title="Parallel capacitance")
        ax2.set(xlabel="Frequency (Hz)", ylabel="D", title="Dissipation factor")
        ax1.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "fig_capacitance.png")
        plt.close(fig)
        figs.append({"file": "fig_capacitance.png", "caption": "Parallel capacitance and dissipation factor "
                     "(no permittivity: the sample geometry is incomplete)."})

    # impedance (always)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.0))
    for i, r in enumerate(results):
        c = PALETTE[i % len(PALETTE)]
        f = _a(r, "f")
        ax1.loglog(*_pos(f, _a(r, "Z")), "o", ms=3, color=c, label=r["run"])
        ax2.semilogx(f, _a(r, "theta"), "o", ms=3, color=c, label=r["run"])
    ax1.set(xlabel="Frequency (Hz)", ylabel="|Z| (Ω)", title="Impedance magnitude")
    ax2.set(xlabel="Frequency (Hz)", ylabel="θ (°)", title="Phase angle")
    ax1.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "fig_impedance.png")
    plt.close(fig)
    figs.append({"file": "fig_impedance.png", "caption": "Measured impedance magnitude and phase (raw data)."})
    return figs
