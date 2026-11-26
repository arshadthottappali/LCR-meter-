"""Research pipeline tests: no network, no real AI (scripted fake LLM + mocked sources)."""
import json
import math

import pytest

from lcr_app.core.storage import Run
from lcr_app.core.sweep import SweepConfig, SweepRunner
from lcr_app.instrument.hioki3532 import Hioki3532
from lcr_app.instrument.simulator import SimulatedTransport
from lcr_app.research import agent as agent_mod
from lcr_app.research.agent import ResearchAgent, ResearchSettings
from lcr_app.research.dielectric import analyze_run, arrhenius
from lcr_app.research.importer import ImportSpec, import_file, sniff
from lcr_app.research.knowledge import KnowledgeBase, material_key
from lcr_app.research.llm import extract_json, pick_default_model
from lcr_app.research.report import build_report
from lcr_app.research.sample import EPS0, SAMPLE_TYPES, SampleInfo
from lcr_app.research.sources import Paper, merge_papers

ABSTRACT = ("Thin films of testium were spin coated on ITO. The dielectric constant of 79 at 1 kHz decreases to "
            "5.2 at 1 MHz due to a Debye relaxation. The dc conductivity was 1.2 × 10^-7 S/m at room temperature.")


def debye_run(name="debye", temp=None):
    m = Hioki3532(SimulatedTransport("Dielectric sample (Debye relaxation)", realtime=False, noise=5e-4))
    pts = SweepRunner(m, SweepConfig(start_hz=42, stop_hz=5e6, points=41, settle_ms=0)).run()
    meta = {"temperature_c": temp} if temp is not None else {}
    return Run(name, [p.as_row() for p in pts], meta)


def film_sample():
    d = 1e-6
    return SampleInfo(values={"material_name": "testium", "sample_type": SAMPLE_TYPES["sandwich"],
                              "electrode_shape": "Area given", "electrode_area": 10e-12 * d / EPS0,
                              "thickness": d, "temperature_c": 23, "material_class": "Polymer"})


def test_permittivity_and_fit():
    r = analyze_run(debye_run(), film_sample())
    assert r["has_permittivity"]
    rf = r["relaxation_fit"]
    assert rf["relaxation_in_window"]
    assert rf["eps_inf"] == pytest.approx(5, rel=0.05)
    assert rf["delta_eps"] == pytest.approx(75, rel=0.05)
    assert rf["tau_s"] == pytest.approx(1e-5, rel=0.05)
    t1k = next(t for t in r["at_standard_freqs"] if t["f_Hz"] == 1e3)
    assert t1k["eps1"] == pytest.approx(79.6, rel=0.02)
    json.dumps(r)  # cacheable


def test_no_geometry_gives_capacitance_only():
    s = SampleInfo(values={"material_name": "x", "sample_type": SAMPLE_TYPES["unknown"]})
    r = analyze_run(debye_run(), s)
    assert not r["has_permittivity"]
    assert any(c["code"] == "no_geometry" for c in r["checks"])


def test_series_resistance_check():
    # 10 nm film on ITO: 2.7 nF in series with 100 ohm
    rows = []
    from lcr_app.core.params import derive
    import cmath
    for k in range(40):
        f = 100 * 10 ** (k * 4.7 / 39)
        z = 100 + 1 / (1j * 2 * math.pi * f * 2.7e-9) * (1 / (1 + 0.001j))
        rows.append({"freq_Hz": f, **derive(abs(z), math.degrees(cmath.phase(z)), f)})
    r = analyze_run(Run("thin", rows, {}), SampleInfo(values={"sample_type": SAMPLE_TYPES["sandwich"],
                                                              "electrode_area": 1e-6, "thickness": 10e-9}))
    sr = [c for c in r["checks"] if c["code"] == "series_resistance"]
    assert sr and sr[0]["Rs_ohm"] == pytest.approx(100, rel=0.15)


def test_arrhenius():
    ea = 0.5
    pts = [(t, 1e3 * math.exp(-ea / (8.617e-5 * (t + 273.15)))) for t in (20, 40, 60, 80)]
    assert arrhenius(pts)["Ea_eV"] == pytest.approx(ea, rel=1e-3)


def test_generic_import(tmp_path):
    p = tmp_path / "other.txt"
    lines = ["Freq(kHz);Cp(nF);D"]
    for f in (0.1, 1, 10, 100):
        lines.append(f"{f};2,5;0,01")  # decimal commas, semicolons
    p.write_text("\n".join(lines))
    delim, rows, first = sniff(p)
    assert delim == ";" and first == 1
    run = import_file(p, ImportSpec(kind="Cp_D", freq_unit="kHz", a_unit="nF", delimiter=delim, skip_rows=first))
    assert len(run.rows) == 4
    assert run.rows[0]["freq_Hz"] == pytest.approx(100)
    assert run.rows[2]["Cp"] == pytest.approx(2.5e-9, rel=1e-6)
    assert run.rows[2]["D"] == pytest.approx(0.01, rel=1e-6)


def test_helpers():
    assert extract_json('Here:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert pick_default_model(["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite",
                               "gemini-2.5-pro", "gemini-3.0-flash-preview"]) == "gemini-2.5-flash"
    m = merge_papers([Paper("10.1/a", "Title A", "10.1/a", abstract="short"),
                      Paper("s2:x", "Title A", "", abstract="a longer abstract")])
    assert list(m) == ["10.1/a"] and m["10.1/a"].abstract == "a longer abstract"
    assert material_key("PEDOT:PSS") == "name-pedot-pss"


class FakeLLM:
    model = "fake-model"

    def __init__(self):
        self.calls = 0
        self.tokens = 0
        self.tasks = []

    def generate_json(self, task, system, prompt, schema=None, temperature=0.2):
        self.calls += 1
        self.tasks.append(task)
        if task.startswith("Plan"):
            return {"queries": ["testium dielectric relaxation"]}
        if task.startswith("Extract"):
            return {"papers": [
                {"id": "0", "relevance": 3, "material": "testium", "sample_form": "thin film",
                 "conditions": "ITO/Al, 1 kHz–1 MHz", "key_finding": "Debye relaxation.",
                 "values": [{"quantity": "eps1", "value": 79, "unit": "", "f_Hz": 1000,
                             "quote": "The dielectric constant of 79 at 1 kHz"},
                            {"quantity": "eps1", "value": 999, "unit": "", "f_Hz": 1e6,
                             "quote": "invented sentence 999"}]},
                {"id": "1", "relevance": 0, "values": []}]}
        if task.startswith("Write report"):
            return {"summary": "ε′ ≈ 80 at 1 kHz agrees with [P1]. Also see [P7] and doi 10.9999/fake.",
                    "discussion": "Similar to P1. A made-up citation [P99].",
                    "conclusion": "- Measure at several temperatures."}
        raise AssertionError(task)

    def generate(self, task, system, prompt, temperature=0.3):
        self.calls += 1
        self.tasks.append(task)
        assert "[P1]" in prompt
        return "Testium films show ε′ ≈ 79 at 1 kHz [P1]."

    def tool_loop(self, task, system, prompt, tools, max_steps=8, on_call=None, temperature=0.3):
        self.calls += 1
        self.tasks.append(task)
        v = tools["get_measured_value"][1](quantity="eps1", f_Hz=1000)
        assert v["results"][0]["value"] == pytest.approx(79.6, rel=0.03)
        a = tools["ask_researcher"][1](question="Was the film dried?")
        assert a["answer"] == "yes, 2 h at 80 °C"
        return json.dumps({"comparisons": [{"quantity": "eps1", "f_Hz": 1000, "measured": "79.6",
                                            "literature": "79 [P1]", "agreement": "similar",
                                            "explanation": "Same material and form [P1]."}],
                           "similarities": ["Relaxation near 16 kHz [P1]"],
                           "differences": [{"text": "none", "possible_reasons": ["—"]}],
                           "open_questions": ["Humidity effect?"]})


class IO:
    def __init__(self):
        self.logs = []
        self.approved = []

    def status(self, msg):
        pass

    def log(self, kind, msg):
        self.logs.append((kind, msg))

    def ask(self, q):
        return "yes, 2 h at 80 °C"

    def approve(self, title, text):
        self.approved.append(text)
        return True


@pytest.fixture
def offline(monkeypatch):
    papers = [Paper("10.1000/testium", "Dielectric relaxation in testium thin films", "10.1000/testium", 2021,
                    ["A. Author", "B. Author"], "J. Test. Mater.", ABSTRACT, cited_by=12, sources=["OpenAlex"]),
              Paper("10.1000/other", "Unrelated cooking paper", "10.1000/other", 2019, ["C. Cook"], "Food",
                    "Pasta.", sources=["OpenAlex"])]
    monkeypatch.setattr(agent_mod, "search_openalex", lambda q, n, e, k: [Paper(**p.to_dict()) for p in papers])
    monkeypatch.setattr(agent_mod, "search_semantic_scholar", lambda q, n, k: [])
    monkeypatch.setattr(agent_mod, "pubchem_lookup", lambda *a: (_ for _ in ()).throw(agent_mod.SourceError("nf")))
    monkeypatch.setattr(agent_mod, "crossref_verify", lambda doi, title: {"verified": True})


def test_full_pipeline(tmp_path, offline):
    sample = film_sample()
    results = [analyze_run(debye_run(), sample)]
    llm, io = FakeLLM(), IO()
    ag = ResearchAgent(llm, sample, results, tmp_path / "proj", tmp_path / "knowledge", ResearchSettings(), io)
    res = ag.run()
    assert io.approved and "testium" in io.approved[0]
    # the invented value was dropped, the quoted one kept
    vals = [v for e in res.extractions.values() for v in e.get("values", [])]
    assert [v["value"] for v in vals] == [79]
    assert sample.qa and sample.qa[0]["a"].startswith("yes")
    paths = build_report(tmp_path / "proj", sample, results, res)
    md = paths["md"].read_text(encoding="utf-8")
    assert "[1]" in md and "Dielectric relaxation in testium thin films" in md
    assert "P99" not in md and "10.9999/fake" not in md and "P7" not in md
    assert "citation removed" in md
    assert "similar" in md  # code-made literature table
    assert paths["html"].exists() and paths["docx"].exists() and paths["bib"].exists()
    assert "data:image/png;base64" in paths["standalone"].read_text(encoding="utf-8")

    # second run re-uses the cache: no planning / extraction / summary calls
    llm2 = FakeLLM()
    ResearchAgent(llm2, sample, results, tmp_path / "proj", tmp_path / "knowledge", ResearchSettings(), IO()).run()
    assert not any(t.startswith(("Plan", "Extract", "Write literature")) for t in llm2.tasks)
    assert KnowledgeBase(tmp_path / "knowledge", "name-testium").literature_summary()[0]


def test_report_without_ai(tmp_path):
    sample = film_sample()
    results = [analyze_run(debye_run(), sample)]
    paths = build_report(tmp_path / "p", sample, results, None)
    md = paths["md"].read_text(encoding="utf-8")
    assert "local analysis only" in md and "Cole–Cole" in md and "References" not in md


def test_gemini_wrapper_with_mocked_client(monkeypatch):
    """Exercise GeminiLLM with the real google-genai types but a fake HTTP client."""
    from google.genai import types

    from lcr_app.research.llm import GeminiLLM

    llm = GeminiLLM("fake-key", "gemini-test")
    sent = []

    class Resp:
        def __init__(self, calls=None, text=""):
            self.function_calls = calls
            self.text = text
            self.usage_metadata = types.GenerateContentResponseUsageMetadata(total_token_count=10)
            parts = [types.Part(function_call=c) for c in (calls or [])] or [types.Part.from_text(text=text)]
            self.candidates = [types.Candidate(content=types.Content(role="model", parts=parts))]

    script = [Resp([types.FunctionCall(name="get_measured_value", args={"quantity": "eps1", "f_Hz": 1000})]),
              Resp(text='```json\n{"comparisons": []}\n```')]

    def fake_generate(model, contents, config):
        sent.append((contents, config))
        return script.pop(0)

    monkeypatch.setattr(llm.client.models, "generate_content", fake_generate)
    got = []
    tools = {"get_measured_value": ({"description": "x", "parameters": {"type": "object", "properties": {
        "quantity": {"type": "string"}, "f_Hz": {"type": "number"}}}}, lambda quantity, f_Hz: got.append(f_Hz) or {"v": 1})}
    out = llm.tool_loop("t", "sys", "prompt", tools)
    assert got == [1000] and extract_json(out) == {"comparisons": []}
    assert llm.calls == 2 and llm.tokens == 20
    # second request contains the model turn + our function response
    contents = sent[1][0]
    assert contents[-1].parts[0].function_response.name == "get_measured_value"

    # rate limit: retries after the delay given by the server
    class E(Exception):
        code = 429

    calls = []

    def flaky(model, contents, config):
        calls.append(1)
        if len(calls) == 1:
            raise E("429 RESOURCE_EXHAUSTED. Please retry in 0.01s")
        return Resp(text='{"a": 1}')

    monkeypatch.setattr(llm.client.models, "generate_content", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert llm.generate_json("j", "s", "p", {"type": "object"}) == {"a": 1}


def test_source_parsers(monkeypatch):
    from lcr_app.research import sources as S

    def fake(url, headers=None, timeout=30, retries=3, data=None):
        if "openalex" in url:
            return {"results": [{"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/ABC",
                                 "title": "PVDF films", "publication_year": 2020,
                                 "authorships": [{"author": {"display_name": "A. B"}}],
                                 "primary_location": {"source": {"display_name": "J. Mat"}},
                                 "abstract_inverted_index": {"PVDF": [0], "has": [1], "eps": [2]},
                                 "cited_by_count": 5, "open_access": {"oa_url": None}}]}
        if "semanticscholar" in url:
            return {"total": 1, "data": [{"paperId": "x1", "title": "PVDF films", "year": 2020,
                                          "externalIds": {"DOI": "10.1/abc"}, "abstract": None,
                                          "tldr": {"text": "short"}, "authors": [{"name": "A. B"}],
                                          "venue": "", "citationCount": 7}]}
        if "crossref" in url:
            return {"message": {"title": ["PVDF films"], "issued": {"date-parts": [[2020, 1]]},
                                "author": [{"family": "B", "given": "A."}], "container-title": ["J. Mat"]}}
        if "synonyms" in url:
            return {"InformationList": {"Information": [{"Synonym": ["PVDF", "24937-79-9"]}]}}
        if "pubchem" in url:
            return {"PropertyTable": {"Properties": [{"CID": 123, "MolecularFormula": "C2H2F2", "InChIKey": "ABC"}]}}
        raise AssertionError(url)

    monkeypatch.setattr(S, "http_json", fake)
    oa = S.search_openalex("pvdf")
    assert oa[0].doi == "10.1/abc" and oa[0].abstract == "PVDF has eps"
    s2 = S.search_semantic_scholar("pvdf")
    merged = S.merge_papers(oa + s2)
    assert len(merged) == 1 and merged["10.1/abc"].cited_by == 7 and merged["10.1/abc"].tldr == "short"
    assert S.crossref_verify("10.1/abc", "PVDF films")["verified"]
    assert not S.crossref_verify("10.1/abc", "Something completely different about cooking")["verified"]
    mol = S.pubchem_lookup("pvdf")
    assert mol["cid"] == 123 and mol["cas"] == "24937-79-9"
