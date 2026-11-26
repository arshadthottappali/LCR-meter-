"""Report assembly: Markdown (source) → HTML (in-app view, self-contained copy) → Word.

Numbers, tables and figures are written by code from the analysis results.
AI-written text is inserted only into clearly labelled sections, and every
citation in it is mapped to a verified reference (or removed).
"""
from __future__ import annotations

import base64
import json
import math
import re
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..core.params import si_format
from .dielectric import STANDARD_FREQS, _interp_log, temperature_series
from .figures import make_figures
from .sample import SampleInfo

AI_NOTE = ("> **AI-assisted draft.** Sections marked 🤖 were written by an AI model from the measurement summary "
           "and the retrieved paper abstracts. Measured numbers, tables and figures were produced by "
           "deterministic code. Check every statement and reference before using this report.")
CSS = """
body{font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:920px;margin:24px auto;padding:0 16px;
line-height:1.5;color:#1d1d1f;background:#fff}
h1{font-size:1.6em;border-bottom:2px solid #0072B2;padding-bottom:4px}h2{font-size:1.25em;margin-top:1.6em;
color:#0b4f7c}h3{font-size:1.05em}table{border-collapse:collapse;margin:8px 0;font-size:.88em}
th,td{border:1px solid #ccd;padding:3px 7px;text-align:left}th{background:#eef3f8}
img{max-width:100%}blockquote{border-left:4px solid #E69F00;background:#fff8ea;margin:8px 0;padding:6px 12px}
code,pre{background:#f4f4f6;font-size:.85em}pre{padding:8px;white-space:pre-wrap}
.cap{font-size:.85em;color:#555;margin-top:0}
"""


def _f(v, unit="", d=4):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return si_format(float(v), unit, d) if unit else f"{float(v):.{d}g}"


# ------------------------------------------------------------- citations
class Citations:
    def __init__(self, research) -> None:
        self.r = research
        self.order: list[str] = []  # paper keys in order of first citation

    def _num(self, pid: str) -> str | None:
        if not self.r:
            return None
        key = self.r.pid_map.get(pid)
        if not key or key not in self.r.papers:
            return None
        status = self.r.ref_status.get(key, "unchecked")
        if status == "mismatch":
            return None
        if key not in self.order:
            self.order.append(key)
        return str(self.order.index(key) + 1)

    def fix(self, text: str) -> str:
        if not text:
            return ""

        def repl_group(m):
            nums, removed = [], False
            for pid in re.findall(r"P\d+", m.group(1)):
                n = self._num(pid)
                if n:
                    nums.append(n)
                else:
                    removed = True
            if nums:
                return "[" + ", ".join(dict.fromkeys(nums)) + "]"
            return "[citation removed – not verified]" if removed else ""

        text = re.sub(r"\[((?:P\d+|P\?)(?:\s*[,;]\s*(?:P\d+|P\?))*)\]", repl_group, text)
        text = re.sub(r"(?<![\w\[])P(\d+)\b", lambda m: (f"[{self._num('P' + m.group(1))}]"
                                                         if self._num("P" + m.group(1)) else ""), text)
        allowed = {p.doi for k, p in (self.r.papers.items() if self.r else []) if k in self.order and p.doi}
        text = re.sub(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?\b(10\.\d{4,9}/[^\s\])]+)",
                      lambda m: m.group(0) if m.group(1).lower().rstrip(".,;") in allowed else "", text)
        return text

    def reference_list(self) -> list[str]:
        out = []
        for i, key in enumerate(self.order, 1):
            p = self.r.papers[key]
            st = self.r.ref_status.get(key, "unchecked")
            flag = {"verified": "", "no-doi": " *(no DOI)*", "unchecked": " *(DOI not checked – Crossref unreachable)*"}
            out.append(f"{i}. {p.citation()}{flag.get(st, '')}")
        return out

    def bibtex(self) -> str:
        entries = []
        for i, key in enumerate(self.order, 1):
            p = self.r.papers[key]
            authors = " and ".join(p.authors[:10]) or "Unknown"
            fields = {"title": p.title, "author": authors, "year": p.year or "", "journal": p.venue, "doi": p.doi,
                      "url": p.url}
            body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
            entries.append(f"@article{{ref{i},\n{body}\n}}")
        return "\n\n".join(entries)


# ---------------------------------------------------------------- tables
def _md_table(header: list[str], rows: list[list[str]]) -> str:
    def esc(c):
        return str(c).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(esc(h) for h in header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _results_tables(results: list[dict]) -> str:
    parts = []
    for r in results:
        has_eps = r.get("has_permittivity")
        hdr = ["f", "Cp", "D", "|Z|", "θ (°)"] + (["ε′", "ε″", "tan δ", "σ′ (S/m)"] if has_eps else [])
        rows = []
        for t in r.get("at_standard_freqs", []):
            row = [_f(t["f_Hz"], "Hz"), _f(t["Cp_F"], "F"), _f(t["D"]), _f(t["Z_ohm"], "Ω"), _f(t["theta_deg"], d=4)]
            if has_eps:
                row += [_f(t.get("eps1")), _f(t.get("eps2")), _f(t.get("tan_d")), _f(t.get("sigma_S_per_m"), d=3)]
            rows.append(row)
        t_txt = f", T = {r['temperature_c']:g} °C" if r.get("temperature_c") is not None else ""
        parts.append(f"**Run “{r['run']}”** ({r['n_points']} points, {_f(r['f_min_Hz'], 'Hz')} – "
                     f"{_f(r['f_max_Hz'], 'Hz')}{t_txt})\n\n" + _md_table(hdr, rows))
    return "\n\n".join(parts)


def _fit_table(results: list[dict]) -> str:
    rows = []
    for r in results:
        rf = r.get("relaxation_fit")
        if rf and rf.get("relaxation_in_window"):
            rows.append([r["run"], rf["model"], _f(rf["eps_inf"]), _f(rf["delta_eps"]), _f(rf["f_peak_Hz"], "Hz"),
                         f"{rf['alpha_shape_a']:.2f} / {rf['beta_shape_b']:.2f}", _f(rf["sigma_dc_S_per_m"], d=3),
                         f"{rf['r2_log']:.3f}"])
        elif r.get("has_permittivity"):
            rows.append([r["run"], "no relaxation in window", "—", "—", "—", "—", "—", "—"])
    out = ""
    if rows:
        out += ("Relaxation fit, ε* = ε∞ + Δε / [1 + (jωτ)^a]^b − jσ_dc/(ε₀ω) "
                "(Cole–Cole when b = 1):\n\n" + _md_table(["Run", "Model", "ε∞", "Δε", "f_peak", "a / b",
                                                           "σ_dc (S/m)", "R² (log)"], rows))
    jrows = []
    for r in results:
        jf = r.get("jonscher_fit")
        if jf and jf.get("good_fit"):
            sdc = (_f(jf["sigma_dc_S_per_m"], d=3) if jf["dc_plateau_resolved"]
                   else "< " + _f(jf["sigma_dc_upper_limit_S_per_m"], d=3))
            jrows.append([r["run"], sdc, f"{jf['n']:.3f}", f"{jf['r2_log']:.3f}"])
    if jrows:
        out += ("\n\nJonscher power law σ′(ω) = σ_dc + A·ωⁿ:\n\n"
                + _md_table(["Run", "σ_dc (S/m)", "n", "R² (log)"], jrows))
    peaks = [(r["run"], p) for r in results for p in r.get("peaks", [])]
    if peaks:
        out += "\n\nLoss peaks detected:\n\n" + _md_table(
            ["Run", "Quantity", "f_peak", "Value"], [[rn, p["quantity"], _f(p["f_peak_Hz"], "Hz"), _f(p["value"])]
                                                     for rn, p in peaks])
    ts = temperature_series(results)
    for k, v in ts.items():
        out += (f"\n\nArrhenius analysis of {k.replace('_', ' ')}: E_a = {v['Ea_eV']:.3f} eV "
                f"(R² = {v['r2']:.3f}, {v['n_temperatures']} temperatures).")
    return out or "No fits could be made (permittivity not available or too few points)."


def _lit_vs_measured(research, results: list[dict], cites: Citations) -> str:
    if not research or not research.lit_points:
        return ""
    qmap = {"eps1": ("ε′", "eps1"), "eps2": ("ε″", "eps2"), "tan_d": ("tan δ", "tan_d"), "sigma_ac": ("σ′ (S/m)", "sigma")}
    rows = []
    import numpy as np

    for p in research.lit_points:
        lbl, arr_key = qmap.get(p["quantity"], (p["quantity"], None))
        ref = cites.fix(f"[{p['pid']}]")
        if not ref or "removed" in ref:
            continue
        for r in results:
            arr = r.get("arrays", {})
            if arr_key not in arr:
                continue
            f = np.asarray(arr["f"], dtype=float)
            y = np.asarray([math.nan if v is None else v for v in arr[arr_key]], dtype=float)
            m = _interp_log(f, y, p["f_Hz"])
            if math.isnan(m) or not p.get("value"):
                continue
            ratio = m / float(p["value"])
            verdict = "similar" if 0.7 <= ratio <= 1.43 else ("higher" if ratio > 1 else "lower")
            rows.append([lbl, _f(p["f_Hz"], "Hz"), f"{_f(p['value'])} {ref} ({p.get('form') or '?'}"
                         + (f", {p['T_C']:g} °C" if p.get("T_C") is not None else "") + ")",
                         f"{_f(m)} ({r['run']})", f"{ratio:.2f}", verdict])
    if not rows:
        return ""
    return ("Values reported in the literature at a stated frequency, compared with this measurement at the same "
            "frequency (calculated by code; 'similar' = within ±30 %):\n\n"
            + _md_table(["Quantity", "f", "Literature", "This work", "Ratio", "Verdict"], rows))


# ------------------------------------------------------------------ main
def build_report(project_dir: str | Path, sample: SampleInfo, results: list[dict], research=None,
                 runs_meta: list[dict] | None = None) -> dict[str, Path]:
    project_dir = Path(project_dir)
    fig_dir = project_dir / "figures"
    cites = Citations(research)
    sec = research.report_sections if research else {}
    cmp_ = research.comparison if research else {}
    ai = "🤖 " if research else ""
    name = sample.name or "sample"
    now = datetime.now()

    md: list[str] = [f"# Dielectric characterisation of {name}", "",
                     f"*{now:%d %B %Y} · LCR Sweep v{__version__}"
                     + (f" · AI model: {research.model}" if research else " · local analysis only (no AI)") + "*", ""]
    if research:
        md += [AI_NOTE, ""]

    # 1 summary
    md += ["## 1. Summary" + (" 🤖" if research else ""), ""]
    if research:
        md += [cites.fix(sec.get("summary", "")), ""]
    else:
        md += [_local_summary(results, sample), ""]

    # 2 sample & methods
    md += ["## 2. Sample and methods", "", "### Sample", ""]
    md += [sample.as_markdown()]
    mol = (research.molecule if research else None) or sample.pubchem
    if mol:
        md += ["", f"PubChem record: CID [{mol.get('cid')}]({mol.get('url', '')}), formula "
                   f"{mol.get('MolecularFormula', '—')}, M = {mol.get('MolecularWeight', '—')} g/mol"
                   + (f", IUPAC name: {mol.get('IUPACName')}" if mol.get("IUPACName") else "") + "."]
    md += ["", "### Measurement", ""]
    if runs_meta:
        rows = [[m.get("name", ""), m.get("instrument", "—"), m.get("level", "—"), m.get("speed", "—"),
                 m.get("averaging", "—"), m.get("source", "—")] for m in runs_meta]
        md += [_md_table(["Run", "Instrument", "Level", "Speed", "Averaging", "Data file"], rows), ""]
    md += ["Permittivity was calculated from the parallel capacitance and conductance: ε′ = Cp/C₀, "
           "ε″ = G/(ωC₀), tan δ = ε″/ε′, σ′ = ωε₀ε″ and M* = 1/ε*, with the geometric capacitance C₀ given above. "
           "Fits use non-linear least squares on logarithmic residuals.", ""]

    # 3 results
    md += ["## 3. Results", "", _results_tables(results), "", "### Fits", "", _fit_table(results), "",
           "@@FIGURES@@", ""]

    # 4 data quality
    md += ["## 4. Data quality checks", ""]
    any_check = False
    for r in results:
        for c in r.get("checks", []):
            icon = "⚠️" if c["level"] == "warn" else "ℹ️"
            md.append(f"- {icon} **{r['run']}:** {c['message']}")
            any_check = True
    if not any_check:
        md.append("No problems detected.")
    md.append("")

    if research:
        md += [f"## 5. Literature context {ai}", "", cites.fix(research.literature_summary), ""]
        md += [f"## 6. Comparison with the literature", ""]
        t = _lit_vs_measured(research, results, cites)
        if t:
            md += [t, ""]
        comps = cmp_.get("comparisons") or []
        if comps:
            rows = [[c.get("quantity", ""), _f(c.get("f_Hz"), "Hz") if c.get("f_Hz") else "—",
                     cites.fix(str(c.get("measured", ""))), cites.fix(str(c.get("literature", ""))),
                     c.get("agreement", ""), cites.fix(str(c.get("explanation", "")))] for c in comps]
            md += [f"Assessment {ai}:", "", _md_table(["Quantity", "f", "This work", "Literature", "Agreement",
                                                         "Explanation"], rows), ""]
        for title, key in (("Similarities", "similarities"), ("Expected but not confirmed by the retrieved "
                                                                "literature", "expected_but_unverified")):
            items = cmp_.get(key) or []
            if items:
                md += [f"**{title}** {ai}", ""] + [f"- {cites.fix(str(x))}" for x in items] + [""]
        diffs = cmp_.get("differences") or []
        if diffs:
            md += [f"**Differences and possible reasons** {ai}", ""]
            for d in diffs:
                if isinstance(d, dict):
                    md.append(f"- {cites.fix(str(d.get('text', '')))}")
                    md += [f"    - {cites.fix(str(r))}" for r in d.get("possible_reasons", [])]
                else:
                    md.append(f"- {cites.fix(str(d))}")
            md.append("")
        md += [f"## 7. Discussion {ai}", "", cites.fix(sec.get("discussion", "")), ""]
        md += [f"## 8. Conclusions and next steps {ai}", "", cites.fix(sec.get("conclusion", "")), ""]
        for title, key in (("Suggested measurements", "suggested_measurements"), ("Open questions", "open_questions")):
            items = cmp_.get(key) or []
            if items:
                md += [f"**{title}**", ""] + [f"- {cites.fix(str(x))}" for x in items] + [""]
        md += ["## References", ""] + (cites.reference_list() or ["No references."]) + [""]
        md += ["## Appendix: AI transparency", "",
               f"- Model: {research.model}; requests: {research.calls}; tokens: {research.tokens}; "
               f"started {research.started}.",
               f"- Literature sources: OpenAlex, Semantic Scholar (search), Crossref (DOI verification), PubChem.",
               f"- Search queries: {'; '.join(research.queries) or '—'}",
               f"- Papers retrieved: {len(research.papers)}; with extracted values: "
               f"{sum(1 for e in research.extractions.values() if e.get('values'))}.",
               "- Literature values are only used when the AI quoted the sentence from the abstract and the "
               "quote was found there by code.", ""]
        md += [f"- Note: {n}" for n in research.notes]
        md += ["", "<details><summary>Information that was sent to the AI</summary>", "", "```",
               research.shared_text, "```", "", "</details>", ""]

    # figures last, so literature markers use the final reference numbers
    lit_fig_pts = []
    if research:
        for p in research.lit_points:
            key = research.pid_map.get(p["pid"])
            if key in cites.order:
                lit_fig_pts.append({"ref": f"[{cites.order.index(key) + 1}]", "f_Hz": p["f_Hz"],
                                    p["quantity"]: p["value"]})
    figs = make_figures(results, fig_dir, lit_fig_pts)
    fig_md = []
    for i, fg in enumerate(figs, 1):
        fig_md += [f"![Figure {i}](figures/{fg['file']})", "", f"*Figure {i}. {fg['caption']}*", ""]
    text = "\n".join(md).replace("@@FIGURES@@", "\n".join(fig_md))
    paths = {"md": project_dir / "report.md", "html": project_dir / "report.html",
             "standalone": project_dir / "report_standalone.html", "docx": project_dir / "report.docx"}
    project_dir.mkdir(parents=True, exist_ok=True)
    paths["md"].write_text(text, encoding="utf-8")
    html = to_html(text, f"Dielectric characterisation of {name}")
    paths["html"].write_text(html, encoding="utf-8")
    paths["standalone"].write_text(_inline_images(html, project_dir), encoding="utf-8")
    if research and cites.order:
        paths["bib"] = project_dir / "references.bib"
        paths["bib"].write_text(cites.bibtex(), encoding="utf-8")
    try:
        to_docx(text, paths["docx"], project_dir)
    except Exception as e:  # Word export is optional
        paths.pop("docx")
        (project_dir / "docx_error.txt").write_text(str(e), encoding="utf-8")
    (project_dir / "analysis.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k != "arrays"} for r in results], indent=1, ensure_ascii=False),
        encoding="utf-8")
    return paths


def _local_summary(results: list[dict], sample: SampleInfo) -> str:
    out = []
    for r in results:
        t = next((x for x in r.get("at_standard_freqs", []) if x["f_Hz"] == 1e3), None) or \
            (r.get("at_standard_freqs") or [None])[0]
        if not t:
            continue
        if t.get("eps1") is not None:
            out.append(f"Run “{r['run']}”: ε′ = {_f(t['eps1'])} and tan δ = {_f(t['tan_d'])} at {_f(t['f_Hz'], 'Hz')}.")
        else:
            out.append(f"Run “{r['run']}”: Cp = {_f(t['Cp_F'], 'F')} and D = {_f(t['D'])} at {_f(t['f_Hz'], 'Hz')}.")
        rf = r.get("relaxation_fit")
        if rf and rf.get("relaxation_in_window"):
            out.append(f"A dielectric relaxation is found at {_f(rf['f_peak_Hz'], 'Hz')} (Δε = {_f(rf['delta_eps'])}).")
        warns = [c for c in r.get("checks", []) if c["level"] == "warn"]
        if warns:
            out.append(f"{len(warns)} data-quality warning(s), see section 4.")
    return " ".join(out) or "No data."


# --------------------------------------------------------------------- HTML
def to_html(md_text: str, title: str) -> str:
    import markdown

    body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists", "md_in_html"])
    body = re.sub(r"<p><em>(Figure \d+\..*?)</em></p>", r'<p class="cap"><em>\1</em></p>', body)
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")


def _inline_images(html: str, base: Path) -> str:
    def rep(m):
        p = base / m.group(1)
        if not p.exists():
            return m.group(0)
        return f'src="data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"'

    return re.sub(r'src="(figures/[^"]+)"', rep, html)


# --------------------------------------------------------------------- Word
def _add_runs(par, text: str) -> None:
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)  # links -> text
    for part in re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            par.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            par.add_run(part[1:-1]).italic = True
        else:
            par.add_run(part)


def to_docx(md_text: str, path: Path, base: Path) -> None:
    from docx import Document
    from docx.shared import Cm, Pt

    doc = Document()
    doc.styles["Normal"].font.size = Pt(10)
    lines = md_text.splitlines()
    i = 0
    in_details = False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("<details"):
            in_details = True
            doc.add_paragraph().add_run("Information that was sent to the AI:").bold = True
            i += 1
            continue
        if ln.startswith("</details") or ln.startswith("<summary"):
            in_details = False if ln.startswith("</details") else in_details
            i += 1
            continue
        if ln.startswith("```"):
            j = i + 1
            block = []
            while j < len(lines) and not lines[j].startswith("```"):
                block.append(lines[j])
                j += 1
            p = doc.add_paragraph()
            r = p.add_run("\n".join(block))
            r.font.name = "Consolas"
            r.font.size = Pt(8)
            i = j + 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            doc.add_heading(m.group(2), level=min(len(m.group(1)), 3) if len(m.group(1)) > 1 else 0)
            i += 1
            continue
        m = re.match(r"^!\[[^\]]*\]\(([^)]+)\)", ln)
        if m:
            p = base / m.group(1)
            if p.exists():
                doc.add_picture(str(p), width=Cm(16))
            i += 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                t = doc.add_table(rows=len(rows), cols=len(rows[0]))
                t.style = "Table Grid"
                for r_i, row in enumerate(rows):
                    for c_i, c in enumerate(row[: len(rows[0])]):
                        cell = t.cell(r_i, c_i)
                        cell.text = ""
                        _add_runs(cell.paragraphs[0], c)
                        for run in cell.paragraphs[0].runs:
                            run.font.size = Pt(8)
                            if r_i == 0:
                                run.bold = True
            continue
        m = re.match(r"^(\s*)[-*]\s+(.*)", ln)
        if m:
            style = "List Bullet 2" if len(m.group(1)) >= 2 else "List Bullet"
            _add_runs(doc.add_paragraph(style=style), m.group(2))
            i += 1
            continue
        m = re.match(r"^\d+\.\s+(.*)", ln)
        if m:
            _add_runs(doc.add_paragraph(style="List Number"), m.group(1))
            i += 1
            continue
        if ln.startswith(">"):
            p = doc.add_paragraph()
            _add_runs(p, ln.lstrip("> "))
            p.runs and setattr(p.runs[0], "italic", True)
            i += 1
            continue
        if ln.strip():
            _add_runs(doc.add_paragraph(), ln)
        i += 1
    doc.save(str(path))
