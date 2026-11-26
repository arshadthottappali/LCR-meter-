"""The research agent: literature search → value extraction → comparison → report text.

Design rules (enforced in code, not only in prompts):
* The local analysis (numbers, fits, checks) is deterministic Python; the AI
  never computes the measured values, it only receives them.
* The AI can only cite papers our search tools returned. Citations are short
  ids [P1], [P2] … that the report code maps to verified references; unknown
  ids and inline DOIs are removed.
* A literature value is kept only if the AI quotes the sentence it came from
  and that sentence really occurs in the paper's abstract.
* Everything is cached per material (knowledge.py), so repeating a report
  re-uses searches, extractions and the literature summary.
* Before anything is sent to Gemini the user sees it and must approve.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from .dielectric import summary_text, temperature_series
from .knowledge import CrossrefCache, KnowledgeBase, material_key
from .llm import LLMCancelled, LLMError, extract_json
from .sample import SampleInfo
from .sources import (Paper, SourceError, _norm_title, crossref_verify, pubchem_lookup, search_openalex,
                      search_semantic_scholar)

QUANTITIES = {
    "eps1": "real relative permittivity ε′ (dielectric constant)",
    "eps2": "imaginary permittivity ε″",
    "tan_d": "loss tangent tan δ / dissipation factor",
    "sigma_dc": "dc conductivity (S/m)",
    "sigma_ac": "ac conductivity at a stated frequency (S/m)",
    "f_relax": "relaxation / loss-peak frequency (Hz)",
    "Ea": "activation energy (eV)",
    "other": "other quantitative result",
}
UNIT_TO_SI = {"s/m": 1.0, "s/cm": 100.0, "ms/cm": 0.1, "µs/cm": 1e-4, "us/cm": 1e-4, "hz": 1.0, "khz": 1e3,
              "mhz": 1e6, "ghz": 1e9, "ev": 1.0, "mev": 1e-3, "kj/mol": 0.0103643, "": 1.0}


@dataclass
class ResearchSettings:
    model: str = ""
    papers_per_query: int = 10
    max_queries: int = 6
    max_extract_papers: int = 20
    max_agent_steps: int = 8
    max_questions: int = 3
    use_openalex: bool = True
    use_semantic_scholar: bool = True
    openalex_email: str = ""
    openalex_api_key: str = ""
    s2_api_key: str = ""
    force_refresh: bool = False


class UserIO(Protocol):
    def status(self, msg: str) -> None: ...

    def log(self, kind: str, msg: str) -> None: ...

    def ask(self, question: str) -> str: ...

    def approve(self, title: str, text: str) -> bool: ...


@dataclass
class ResearchResult:
    material_key: str
    molecule: dict
    papers: dict[str, Paper]
    pid_map: dict[str, str]  # "P1" -> paper key
    extractions: dict[str, dict]
    literature_summary: str
    comparison: dict
    report_sections: dict[str, str]
    lit_points: list[dict]
    shared_text: str
    queries: list[str]
    model: str
    calls: int
    tokens: int
    started: str
    notes: list[str] = field(default_factory=list)
    ref_status: dict[str, str] = field(default_factory=dict)  # paper key -> verified | mismatch | unchecked | no-doi


SYSTEM_BASE = (
    "You are a careful materials scientist specialised in dielectric and impedance spectroscopy of thin films. "
    "You help a researcher compare their own measurement with published literature.\n"
    "Strict rules:\n"
    "1. Measured numbers come ONLY from the data summary given to you. Never invent or recompute them.\n"
    "2. Literature facts come ONLY from the paper records given to you or returned by your tools. Cite them with "
    "their ids in square brackets, e.g. [P3] or [P3, P7]. Never cite anything else, never write DOIs or author "
    "names as citations.\n"
    "3. If the information is not available, say so explicitly ('not reported in the retrieved literature').\n"
    "4. Distinguish clearly between measured facts, literature facts and your interpretation (use words such as "
    "'likely', 'possibly' for interpretation).\n"
    "5. Consider sample form (thin film vs bulk vs solution), thickness, electrodes, frequency, temperature, "
    "humidity, purity and processing whenever you compare values.\n"
)


class ResearchAgent:
    def __init__(self, llm, sample: SampleInfo, results: list[dict], project_dir: str | Path,
                 knowledge_root: str | Path, settings: ResearchSettings, io: UserIO,
                 save_sample: Callable[[], None] | None = None) -> None:
        self.llm = llm
        self.sample = sample
        self.results = results
        self.project_dir = Path(project_dir)
        self.knowledge_root = Path(knowledge_root)
        self.s = settings
        self.io = io
        self.save_sample = save_sample or (lambda: None)
        self.questions_asked = 0
        self.new_searches = 0
        self.notes: list[str] = []

    # ================================================================ helpers
    def _log(self, kind: str, msg: str) -> None:
        self.io.log(kind, msg)

    def measured_summary(self) -> str:
        parts = [summary_text(r) for r in self.results]
        ts = temperature_series(self.results)
        if ts:
            for k, v in ts.items():
                parts.append(f"Temperature series ({k}): Arrhenius Ea = {v['Ea_eV']:.3f} eV, R² = {v['r2']:.3f}, "
                             f"{v['n_temperatures']} temperatures.")
        return "\n\n".join(parts)

    def material_names(self) -> list[str]:
        names = [self.sample.name]
        syn = self.sample.get("synonyms", "")
        names += [s.strip() for s in str(syn).split(",") if s.strip()]
        return [n for n in dict.fromkeys(names) if n]

    # ================================================================== steps
    def identify_material(self) -> tuple[str, dict]:
        mol = self.sample.pubchem or {}
        if not mol and (self.sample.name or self.sample.get("cas") or self.sample.get("smiles")):
            self.io.status("Looking up the molecule in PubChem …")
            try:
                mol = pubchem_lookup(self.sample.name, str(self.sample.get("cas", "") or ""),
                                     str(self.sample.get("smiles", "") or ""))
                self.sample.pubchem = mol
                self.save_sample()
                self._log("source", f"PubChem: CID {mol.get('cid')} {mol.get('MolecularFormula', '')}")
            except SourceError as e:
                self._log("info", f"PubChem: {e}")
                mol = {}
        key = material_key(self.sample.name, mol.get("InChIKey", ""))
        kb = KnowledgeBase(self.knowledge_root, key)
        if mol:
            kb.save_molecule(mol)
        return key, mol

    def plan_queries(self, mol: dict) -> list[str]:
        schema = {"type": "object", "properties": {
            "queries": {"type": "array", "items": {"type": "string"}},
            "comparable_materials": {"type": "array", "items": {"type": "string"}}},
            "required": ["queries"]}
        prompt = (
            f"Material under study: {', '.join(self.material_names())}\n"
            f"PubChem: {json.dumps({k: mol.get(k) for k in ('MolecularFormula', 'IUPACName') if mol.get(k)})}\n\n"
            f"Sample description:\n{self.sample.as_text()}\n\n"
            f"Measured summary:\n{self.measured_summary()}\n\n"
            f"Write {self.s.max_queries} short literature search queries (3–8 words each, English keywords, no "
            "boolean operators) for a scholarly search engine to find papers that report dielectric / impedance "
            "properties of this material (thin films first, then bulk), its relaxation processes and conduction "
            "mechanisms, plus 1–2 queries for closely related comparable materials if the material itself is "
            "rarely studied. Also list comparable materials.")
        data = self.llm.generate_json("Plan literature searches", SYSTEM_BASE, prompt, schema)
        qs = [q.strip() for q in data.get("queries", []) if isinstance(q, str) and q.strip()]
        base = f"{self.sample.name} dielectric permittivity thin film"
        if base not in qs:
            qs.insert(0, base)
        return qs[: self.s.max_queries + 1]

    def run_search(self, kb: KnowledgeBase, query: str) -> list[Paper]:
        found: list[Paper] = []
        for src, fn in (("openalex", self.s.use_openalex), ("s2", self.s.use_semantic_scholar)):
            if not fn:
                continue
            if kb.searched(query, src) and not self.s.force_refresh:
                self._log("cache", f"Already searched ({src}): {query}")
                continue
            try:
                if src == "openalex":
                    res = search_openalex(query, self.s.papers_per_query, self.s.openalex_email,
                                          self.s.openalex_api_key)
                else:
                    res = search_semantic_scholar(query, self.s.papers_per_query, self.s.s2_api_key)
                kb.record_search(query, src, len(res))
                found += res
                self._log("search", f"{'OpenAlex' if src == 'openalex' else 'Semantic Scholar'}: '{query}' → "
                                    f"{len(res)} papers")
            except SourceError as e:
                self._log("warn", f"Search failed ({src}): {e}")
        if found:
            kb.add_papers(found)
        return found

    def rank_papers(self, papers: dict[str, Paper]) -> list[Paper]:
        names = [n.lower() for n in self.material_names()]
        kw = ["dielectric", "permittivity", "impedance", "relaxation", "loss", "conductivity", "capacitance",
              "thin film", "film", "spectroscopy", "tan", "modulus", "polarization", "polarisation"]

        def score(p: Paper) -> float:
            t = f"{p.title} {p.abstract} {p.tldr}".lower()
            s = 5.0 * sum(n in t for n in names) + sum(k in t for k in kw)
            s += math.log10(1 + (p.cited_by or 0))
            s += 2.0 if p.abstract else -4.0
            return s

        return sorted(papers.values(), key=score, reverse=True)

    def extract_values(self, kb: KnowledgeBase, papers: list[Paper]) -> dict[str, dict]:
        done = kb.extractions()
        todo = [p for p in papers if p.key not in done and (p.abstract or p.tldr)][: self.s.max_extract_papers]
        if not todo:
            self._log("cache", "All selected papers were already analysed (cached).")
            return done
        schema = {"type": "object", "properties": {"papers": {"type": "array", "items": {
            "type": "object", "properties": {
                "id": {"type": "string"}, "relevance": {"type": "integer"},
                "material": {"type": "string"}, "sample_form": {"type": "string"},
                "conditions": {"type": "string"}, "key_finding": {"type": "string"},
                "values": {"type": "array", "items": {"type": "object", "properties": {
                    "quantity": {"type": "string", "enum": list(QUANTITIES)},
                    "value": {"type": "number"}, "unit": {"type": "string"},
                    "f_Hz": {"type": "number"}, "T_C": {"type": "number"},
                    "quote": {"type": "string"}}, "required": ["quantity", "value", "quote"]}}},
            "required": ["id", "relevance", "values"]}}}, "required": ["papers"]}
        for i in range(0, len(todo), 5):
            batch = todo[i:i + 5]
            self.io.status(f"Reading abstracts {i + 1}–{i + len(batch)} of {len(todo)} …")
            recs = "\n\n".join(f"[{k}] {p.title} ({p.year})\nAbstract: {p.abstract or p.tldr}"
                               for k, p in enumerate(batch))
            prompt = (
                f"Material of interest: {', '.join(self.material_names())} "
                f"({self.sample.get('material_class', 'unknown class')}).\n"
                "For each paper below return: id (the number in brackets), relevance (0 = unrelated, 1 = related "
                "topic, 2 = same material class / related material with dielectric data, 3 = same material with "
                "dielectric/impedance data), material actually studied, sample_form (thin film / bulk / pellet / "
                "solution / composite / theory), conditions (thickness, electrodes, frequency range, temperature), "
                "key_finding (one sentence) and values: every NUMERICAL dielectric result stated in the abstract "
                f"with quantity one of {list(QUANTITIES)}, value, unit, frequency f_Hz and temperature T_C if stated, "
                "and quote = the exact sentence fragment from the abstract containing the number (copy it "
                "verbatim). Do not infer values that are not written in the abstract.\n\n" + recs)
            try:
                data = self.llm.generate_json("Extract values from abstracts", SYSTEM_BASE, prompt, schema)
            except LLMCancelled:
                raise
            except LLMError as e:
                self._log("warn", f"Extraction failed for a batch: {e}")
                continue
            for item in data.get("papers", []):
                try:
                    p = batch[int(item.get("id"))]
                except (TypeError, ValueError, IndexError):
                    continue
                item["values"] = self._verify_quotes(p, item.get("values", []))
                item.pop("id", None)
                kb.save_extraction(p.key, item)
        return kb.extractions()

    def _verify_quotes(self, p: Paper, values: list[dict]) -> list[dict]:
        text = _norm_ws(f"{p.abstract} {p.tldr}")
        kept = []
        for v in values:
            q = _norm_ws(str(v.get("quote", "")))
            num_ok = _number_in(v.get("value"), q)
            if q and q in text and num_ok:
                v["verified_quote"] = True
                v["value_si"] = _to_si(v.get("quantity"), v.get("value"), v.get("unit", ""))
                kept.append(v)
            else:
                self._log("check", f"Dropped a value from '{p.title[:50]}…': quote not found in the abstract.")
        return kept

    def literature_summary(self, kb: KnowledgeBase, ranked: list[Paper], pid: dict[str, str],
                           extractions: dict[str, dict]) -> str:
        records = self.paper_records(ranked, pid, extractions, min_rel=1, max_n=25)
        prompt = (
            f"Write a concise literature summary (250–400 words, Markdown, no headings) about the dielectric and "
            f"electrical properties of {', '.join(self.material_names())} and closely related materials, based "
            "ONLY on these records. Group by theme (typical permittivity values, loss and relaxation processes, "
            "conduction, effect of film thickness/processing/humidity). Cite with [P#]. State clearly what is "
            f"missing in the literature.\n\nRecords:\n{records}")
        return self.llm.generate("Write literature summary", SYSTEM_BASE, prompt)

    def paper_records(self, ranked: list[Paper], pid: dict[str, str], extractions: dict[str, dict],
                      min_rel: int = 1, max_n: int = 25) -> str:
        inv = {v: k for k, v in pid.items()}
        out = []
        for p in ranked:
            e = extractions.get(p.key, {})
            if e and int(e.get("relevance", 0) or 0) < min_rel:
                continue
            if p.key not in inv:
                continue
            vals = "; ".join(f"{v['quantity']}={v['value']} {v.get('unit', '')}"
                             + (f" at {v['f_Hz']:g} Hz" if v.get("f_Hz") else "")
                             + (f", {v['T_C']:g} °C" if v.get("T_C") is not None else "")
                             for v in e.get("values", []))
            out.append(f"[{inv[p.key]}] {p.title} ({p.year}). Material: {e.get('material', '?')}; form: "
                       f"{e.get('sample_form', '?')}; conditions: {e.get('conditions', '?')}. "
                       f"Finding: {e.get('key_finding', '')} Values: {vals or 'none stated'}")
            if len(out) >= max_n:
                break
        return "\n".join(out) or "(no relevant papers found)"

    # ---------------------------------------------------------- comparison
    def compare(self, kb: KnowledgeBase, ranked: list[Paper], pid: dict[str, str],
                extractions: dict[str, dict], lit_summary: str) -> dict:
        tools = {
            "search_literature": ({"description": "Search scholarly databases for more papers when the given "
                                                  "records are not enough to judge a similarity or difference. "
                                                  "Returns new paper records with ids.",
                                   "parameters": {"type": "object", "properties": {
                                       "query": {"type": "string", "description": "3–8 keywords"}},
                                       "required": ["query"]}}, lambda query: self._tool_search(kb, pid, query)),
            "get_paper": ({"description": "Full abstract and extracted values of a paper by id (e.g. P4).",
                           "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                                          "required": ["id"]}},
                          lambda id: self._tool_paper(kb, pid, id)),
            "get_measured_value": ({"description": "Measured value of a quantity (eps1, eps2, tan_d, sigma, Cp, D, "
                                                   "Z, theta) at a frequency in Hz, for each run.",
                                    "parameters": {"type": "object", "properties": {
                                        "quantity": {"type": "string"}, "f_Hz": {"type": "number"}},
                                        "required": ["quantity", "f_Hz"]}}, self._tool_measured),
            "ask_researcher": ({"description": "Ask the researcher ONE short question about the sample or the "
                                               "experiment when the answer is essential to explain a difference "
                                               "(e.g. drying, electrode material, humidity). Use sparingly.",
                                "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                                               "required": ["question"]}}, self._tool_ask),
        }
        records = self.paper_records(ranked, pid, extractions, min_rel=1, max_n=30)
        prompt = (
            f"MATERIAL: {', '.join(self.material_names())}\n\nSAMPLE:\n{self.sample.as_text()}\n\n"
            f"MEASURED DATA SUMMARY:\n{self.measured_summary()}\n\nLITERATURE SUMMARY:\n{lit_summary}\n\n"
            f"PAPER RECORDS:\n{records}\n\n"
            "TASK: Compare the measurement with the literature like a critical research supervisor.\n"
            "- Where do the results agree with the literature, and why would they be expected to agree?\n"
            "- Where do they differ, by how much, and what are the plausible reasons (film vs bulk, thickness, "
            "electrodes/interfaces, frequency or temperature range, humidity, processing, purity, measurement "
            "artefacts flagged in the data-quality checks)?\n"
            "- What would be expected but cannot be confirmed from the retrieved literature?\n"
            "If the records are insufficient, use search_literature (max 2–3 searches). Ask the researcher only "
            "if an answer is essential. Then answer with ONLY a JSON object:\n"
            '{"comparisons":[{"quantity":"eps1|eps2|tan_d|sigma_dc|f_relax|Ea|other","f_Hz":number|null,'
            '"measured":"text","literature":"text with [P#]","agreement":"similar|different|not comparable",'
            '"explanation":"text with [P#]"}],'
            '"similarities":["text with [P#]"],"differences":[{"text":"…","possible_reasons":["…"]}],'
            '"expected_but_unverified":["…"],"open_questions":["…"],"suggested_measurements":["…"]}')
        text = self.llm.tool_loop("Compare with literature", SYSTEM_BASE, prompt, tools,
                                  max_steps=self.s.max_agent_steps,
                                  on_call=lambda n, a: self._log("tool", f"{n}({json.dumps(a, ensure_ascii=False)})"))
        try:
            data = extract_json(text)
        except ValueError:
            self._log("info", "Converting the comparison to structured form …")
            data = self.llm.generate_json("Structure the comparison", SYSTEM_BASE,
                                          "Convert this comparison into the JSON format described before "
                                          "(keys comparisons, similarities, differences, expected_but_unverified, "
                                          f"open_questions, suggested_measurements):\n\n{text}")
        return data if isinstance(data, dict) else {"comparisons": [], "notes": str(data)}

    def _tool_search(self, kb: KnowledgeBase, pid: dict[str, str], query: str) -> dict:
        if self.new_searches >= 3:
            return {"error": "search budget used up; answer with the records you have"}
        self.new_searches += 1
        found = self.run_search(kb, query)
        papers = kb.papers()
        new = []
        for p in found:
            k = p.doi or next((pp.key for pp in papers.values() if pp.doi and _norm_title(pp.title)[:80]
                               == _norm_title(p.title)[:80]), f"t:{_norm_title(p.title)[:80]}")
            if k not in papers:
                continue
            if k not in pid.values():
                pid[f"P{len(pid) + 1}"] = k
            new.append(k)
        inv = {v: k for k, v in pid.items()}
        recs = []
        for k in dict.fromkeys(new):
            p = papers.get(k)
            if p:
                recs.append({"id": inv[k], "title": p.title, "year": p.year,
                             "abstract": (p.abstract or p.tldr)[:900]})
        return {"papers": recs[:6]}

    def _tool_paper(self, kb: KnowledgeBase, pid: dict[str, str], id: str) -> dict:
        key = pid.get(id.strip().strip("[]"))
        p = kb.papers().get(key) if key else None
        if not p:
            return {"error": f"unknown id {id}"}
        return {"title": p.title, "year": p.year, "venue": p.venue, "abstract": p.abstract or p.tldr,
                "extracted": kb.extractions().get(key, {})}

    def _tool_measured(self, quantity: str, f_Hz: float) -> dict:
        from .dielectric import _interp_log

        q = {"sigma_dc": "sigma", "sigma_ac": "sigma", "permittivity": "eps1"}.get(quantity, quantity)
        out = []
        for r in self.results:
            arr = r.get("arrays", {})
            if q not in arr:
                out.append({"run": r["run"], "error": f"'{quantity}' not available"})
                continue
            f = np.asarray(arr["f"], dtype=float)
            y = np.asarray([math.nan if v is None else v for v in arr[q]], dtype=float)
            val = _interp_log(f, y, float(f_Hz), log_y=q not in ("theta",))
            out.append({"run": r["run"], "value": None if math.isnan(val) else round(val, 6),
                        "note": "" if not math.isnan(val) else "frequency outside measured range"})
        return {"quantity": quantity, "f_Hz": f_Hz, "results": out}

    def _tool_ask(self, question: str) -> dict:
        if self.questions_asked >= self.s.max_questions:
            return {"answer": "(question limit reached – state the uncertainty instead)"}
        self.questions_asked += 1
        self._log("question", question)
        ans = self.io.ask(question)
        self.sample.qa.append({"q": question, "a": ans, "date": datetime.now().isoformat(timespec="seconds")})
        self.save_sample()
        return {"answer": ans or "(the researcher did not answer – treat as unknown)"}

    # ------------------------------------------------------------- writing
    def write_sections(self, lit_summary: str, comparison: dict) -> dict[str, str]:
        schema = {"type": "object", "properties": {
            "summary": {"type": "string"}, "discussion": {"type": "string"}, "conclusion": {"type": "string"}},
            "required": ["summary", "discussion", "conclusion"]}
        prompt = (
            f"Write three parts of a short scientific report (Markdown inside the JSON strings, no headings) for "
            f"the material {', '.join(self.material_names())}.\n"
            "summary: 4–6 sentences: what was measured, key numbers, main agreement/disagreement with literature.\n"
            "discussion: 300–500 words: similarities and why expected; differences and their likely causes; what "
            "remains uncertain; reference the data-quality checks where relevant. Cite with [P#].\n"
            "conclusion: 3–5 bullet points including the most useful next measurement.\n\n"
            f"SAMPLE:\n{self.sample.as_text()}\n\nMEASURED:\n{self.measured_summary()}\n\n"
            f"LITERATURE SUMMARY:\n{lit_summary}\n\nCOMPARISON (JSON):\n{json.dumps(comparison, ensure_ascii=False)}")
        return self.llm.generate_json("Write report text", SYSTEM_BASE, prompt, schema, temperature=0.4)

    # ================================================================== main
    def run(self) -> ResearchResult:
        started = datetime.now().isoformat(timespec="seconds")
        key, mol = self.identify_material()
        kb = KnowledgeBase(self.knowledge_root, key)
        self._log("info", f"Knowledge cache: {kb.dir}")

        shared = ("The following will be sent to Google Gemini (free tier) in several requests, together with "
                  "titles and abstracts of public papers found by the search:\n\n"
                  f"=== Sample description ===\n{self.sample.as_text()}\n\n"
                  f"=== Measurement summary (numbers only, no raw data files) ===\n{self.measured_summary()}")
        if not self.io.approve("Send to Gemini?", shared):
            raise LLMCancelled("Not approved by the user")

        if self.s.force_refresh:
            kb.clear_literature()
        lit_text, lit_meta = kb.literature_summary()
        reuse = bool(lit_text) and kb.summary_is_fresh() and not self.s.force_refresh and kb.papers()
        queries = [s["query"] for s in kb.search_log()]
        if reuse:
            self._log("cache", f"Re-using the literature summary from {lit_meta.get('date', '?')} "
                               f"({len(kb.papers())} papers cached). Use 'Search literature again' to refresh.")
            ranked = self.rank_papers(kb.papers())
            extractions = kb.extractions()
        else:
            self.io.status("Planning literature searches …")
            queries = self.plan_queries(mol)
            for q in queries:
                self.io.status(f"Searching: {q}")
                self.run_search(kb, q)
            papers = kb.papers()
            if not papers:
                self.notes.append("No papers could be retrieved (search services unreachable or no results).")
            ranked = self.rank_papers(papers)
            extractions = self.extract_values(kb, ranked[: self.s.max_extract_papers])

        # keep relevant papers, give them short ids
        ranked = [p for p in ranked if int(extractions.get(p.key, {}).get("relevance", 1) or 0) >= 1]
        pid = {f"P{i + 1}": p.key for i, p in enumerate(ranked[:40])}

        if not reuse:
            self.io.status("Writing the literature summary …")
            lit_text = self.literature_summary(kb, ranked, pid, extractions)
            lit_text = _pids_to_keys(lit_text, pid)
            kb.save_literature_summary(lit_text, {"model": getattr(self.llm, "model", ""), "queries": queries})
        # summary is stored with paper keys -> convert to the current P-ids
        lit_for_ai = _keys_to_pids(lit_text, pid)

        self.io.status("Comparing your data with the literature …")
        comparison = self.compare(kb, ranked, pid, kb.extractions(), lit_for_ai)

        self.io.status("Writing the report …")
        sections = self.write_sections(lit_for_ai, comparison)
        sections["literature"] = lit_for_ai

        # verify DOIs of everything that may be cited
        self.io.status("Verifying references with Crossref …")
        papers = kb.papers()
        cache = CrossrefCache(self.knowledge_root)
        cited = _cited_ids(json.dumps(comparison, ensure_ascii=False) + json.dumps(sections, ensure_ascii=False))
        ref_status: dict[str, str] = {}
        for pid_ in sorted(cited, key=lambda x: int(x[1:])):
            p = papers.get(pid.get(pid_, ""))
            if not p:
                continue
            if not p.doi:
                ref_status[p.key] = "no-doi"
                continue
            if p.verified:
                ref_status[p.key] = "verified"
                continue
            rec = cache.get(p.doi)
            if rec is None:
                rec = crossref_verify(p.doi, p.title)
                if "Network error" not in rec.get("reason", ""):
                    cache.put(p.doi, rec)
            if rec.get("verified"):
                ref_status[p.key] = "verified"
                p.verified = True
                kb.update_paper(p)
            elif "Network error" in rec.get("reason", ""):
                ref_status[p.key] = "unchecked"
                self._log("warn", f"Crossref not reachable – reference {pid_} is marked as unchecked.")
            else:
                ref_status[p.key] = "mismatch"
                self._log("check", f"Reference {pid_} removed: {rec.get('reason')}")

        lit_points = self._lit_points(papers, pid, kb.extractions())
        return ResearchResult(
            material_key=key, molecule=mol, papers=kb.papers(), pid_map=pid, extractions=kb.extractions(),
            literature_summary=lit_for_ai, comparison=comparison, report_sections=sections, lit_points=lit_points,
            shared_text=shared, queries=queries, model=getattr(self.llm, "model", ""),
            calls=getattr(self.llm, "calls", 0), tokens=getattr(self.llm, "tokens", 0), started=started,
            notes=self.notes, ref_status=ref_status)

    def _lit_points(self, papers, pid, extractions) -> list[dict]:
        """Literature values with a stated frequency, for the comparison figure/table."""
        inv = {v: k for k, v in pid.items()}
        pts = []
        for key, e in extractions.items():
            if key not in inv or int(e.get("relevance", 0) or 0) < 2:
                continue
            for v in e.get("values", []):
                if v.get("quantity") in ("eps1", "tan_d", "eps2", "sigma_ac") and v.get("f_Hz"):
                    pts.append({"pid": inv[key], "quantity": v["quantity"], "f_Hz": float(v["f_Hz"]),
                                "value": v.get("value_si", v.get("value")), "T_C": v.get("T_C"),
                                "form": e.get("sample_form", ""), "material": e.get("material", "")})
        return pts


# ------------------------------------------------------------------ utilities
def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("−", "-").replace("–", "-")).strip().lower()


def _number_in(value, quote: str) -> bool:
    """Is `value` written in `quote` (allowing unit prefixes and a×10^b notation)?"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    q = quote.replace(",", "").replace("−", "-").replace("–", "-")
    cands = []
    for m, e in re.findall(r"(\d+(?:\.\d+)?)\s*[x×*]\s*10\s*\^?\s*([-+]?\d+)", q):
        cands.append(float(m) * 10.0 ** int(e))
    for e in re.findall(r"(?<![\d.])10\s*\^\s*([-+]?\d+)", q):
        cands.append(10.0 ** int(e))
    cands += [float(x) for x in re.findall(r"\d+(?:\.\d+)?(?:e[-+]?\d+)?", q)]
    for n in cands:
        for scale in (1, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9, 1e-12, 100, 0.01):
            if n and math.isclose(abs(v), n * scale, rel_tol=0.02):
                return True
        if n == 0 and v == 0:
            return True
    return False


def _to_si(quantity, value, unit: str):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").strip().lower().replace(" ", "").replace("Ω", "ohm")
    if quantity in ("sigma_dc", "sigma_ac", "f_relax", "Ea"):
        return v * UNIT_TO_SI.get(u, 1.0)
    return v


def _cited_ids(text: str) -> set[str]:
    return set(re.findall(r"\bP\d+\b", text))


def _pids_to_keys(text: str, pid: dict[str, str]) -> str:
    return re.sub(r"\bP(\d+)\b", lambda m: f"{{{{{pid[m.group(0)]}}}}}" if m.group(0) in pid else m.group(0), text)


def _keys_to_pids(text: str, pid: dict[str, str]) -> str:
    inv = {v: k for k, v in pid.items()}
    return re.sub(r"\{\{([^}]+)\}\}", lambda m: inv.get(m.group(1), "P?"), text)
