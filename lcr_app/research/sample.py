"""Sample description used by the Research tab.

The form is generated from SCHEMA, so adding a field here adds it to the GUI,
to sample_info.json and to the report automatically.

Values are stored as plain JSON (numbers in SI units: metres, farads, ...).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EPS0 = 8.8541878128e-12  # F/m

SAMPLE_TYPES = {
    "sandwich": "Thin film – sandwich (MIM: electrode / film / electrode)",
    "pellet": "Pellet / bulk disc between two electrodes",
    "ide": "Thin film on interdigitated electrodes (IDE)",
    "liquid": "Liquid / solution in a measuring cell",
    "unknown": "Unknown geometry (capacitance only, no permittivity)",
}

MATERIAL_CLASSES = [
    "Small organic molecule", "Polymer", "Polymer blend / composite", "Metal complex / MOF",
    "Perovskite / hybrid", "Inorganic oxide / ceramic", "Liquid crystal", "Ionic liquid / electrolyte",
    "Biological material", "Nanomaterial (2D, NPs, CNT…)", "Other",
]
THICKNESS_METHODS = ["Profilometer (stylus)", "Ellipsometry", "AFM step", "SEM cross-section", "QCM during deposition",
                     "Micrometer / caliper", "Estimated from process", "Unknown"]
PREP_METHODS = ["Spin coating", "Drop casting", "Blade / bar coating", "Dip coating", "Thermal evaporation",
                "Sputtering", "Spray coating", "Electrodeposition", "Langmuir–Blodgett", "Pressed pellet",
                "Other", "Unknown"]
ATMOSPHERES = ["Ambient air", "Nitrogen", "Argon", "Vacuum", "Dry air", "Other"]
YES_NO = ["Yes", "No", "Unknown"]


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"  # text | number | choice | multiline
    unit: str = ""  # SI unit shown; numbers are stored in SI
    help: str = ""
    choices: list[str] = field(default_factory=list)
    types: tuple[str, ...] = ()  # sample types where shown; empty = always
    highlight: str = ""  # id of the diagram element to highlight
    required: tuple[str, ...] = ()  # sample types for which it is needed for the analysis
    weight: int = 1  # completeness weight
    placeholder: str = ""


@dataclass
class Section:
    title: str
    fields: list[Field]
    diagram: bool = False


SCHEMA: list[Section] = [
    Section("Material", [
        Field("material_name", "Material / molecule name", help="Common name, e.g. 'PVDF', 'pentacene', 'PEDOT:PSS'. "
              "Used for the PubChem look-up and the literature search.", required=tuple(SAMPLE_TYPES), weight=4),
        Field("formula", "Chemical formula", help="Filled in automatically by the PubChem look-up if found."),
        Field("cas", "CAS number", help="The most reliable identifier for the look-up, e.g. 24937-79-9."),
        Field("smiles", "SMILES", help="Structure string (optional)."),
        Field("material_class", "Material class", "choice", choices=MATERIAL_CLASSES,
              help="Helps the literature search find comparable materials.", weight=2),
        Field("synonyms", "Other names / abbreviations", help="Comma separated. Improves the search."),
        Field("source_purity", "Supplier / purity / batch", placeholder="e.g. Sigma-Aldrich, 99 %, lot 123"),
        Field("additives", "Dopants / additives / blend ratio", placeholder="e.g. 5 wt% LiTFSI"),
    ]),
    Section("Geometry", [
        Field("sample_type", "Sample type", "choice", choices=list(SAMPLE_TYPES.values()),
              help="Decides how capacitance is converted to permittivity.", weight=3),
        # sandwich / pellet
        Field("electrode_shape", "Top electrode shape", "choice", choices=["Circle", "Rectangle", "Area given"],
              types=("sandwich", "pellet"), highlight="top"),
        Field("electrode_diameter", "Electrode diameter", "number", "m", types=("sandwich", "pellet"),
              highlight="diameter", placeholder="e.g. 2m (= 2 mm)",
              help="Diameter of the OVERLAP of top and bottom electrode (the region where the film is "
                   "between both electrodes). Measure it under a microscope if possible."),
        Field("electrode_length", "Overlap length", "number", "m", types=("sandwich", "pellet"), highlight="length",
              help="For rectangular / crossbar electrodes: length of the overlap."),
        Field("electrode_width", "Overlap width", "number", "m", types=("sandwich", "pellet"), highlight="width",
              help="For crossbar electrodes: the width of the top strip × width of the bottom strip."),
        Field("electrode_area", "Overlap area (if known directly)", "number", "m²", types=("sandwich", "pellet"),
              highlight="area", help="Overrides diameter / length × width. Unit m²: 1 mm² = 1e-6."),
        Field("thickness", "Film / sample thickness d", "number", "m", types=("sandwich", "pellet"),
              highlight="thickness", required=("sandwich", "pellet"), weight=4, placeholder="e.g. 120n or 1.5u",
              help="Thickness between the electrodes. Permittivity is directly proportional to d, so its "
                   "uncertainty goes 1:1 into ε′."),
        Field("thickness_method", "Thickness measured by", "choice", choices=THICKNESS_METHODS,
              types=("sandwich", "pellet"), highlight="thickness", weight=2),
        Field("thickness_uncertainty", "Thickness uncertainty", "number", "%", types=("sandwich", "pellet"),
              highlight="thickness", placeholder="e.g. 5"),
        Field("top_electrode", "Top electrode material", types=("sandwich", "pellet"), highlight="top",
              placeholder="e.g. Al, Au, Ag paste", weight=2),
        Field("bottom_electrode", "Bottom electrode material", types=("sandwich",), highlight="bottom",
              placeholder="e.g. ITO, Au, Al", weight=2,
              help="ITO has a sheet resistance of 10–100 Ω/□ which can limit the high-frequency response."),
        Field("substrate", "Substrate", types=("sandwich", "ide"), highlight="substrate", placeholder="e.g. glass, Si/SiO₂"),
        # IDE
        Field("ide_pairs", "Number of finger pairs", "number", "", types=("ide",), highlight="fingers"),
        Field("ide_length", "Finger overlap length", "number", "m", types=("ide",), highlight="ide_length"),
        Field("ide_gap", "Gap between fingers", "number", "m", types=("ide",), highlight="gap"),
        Field("ide_width", "Finger width", "number", "m", types=("ide",), highlight="finger_width"),
        Field("empty_capacitance", "Empty-cell capacitance C₀ (bare IDE / empty cell)", "number", "F",
              types=("ide", "liquid"), required=("ide", "liquid"), weight=4, highlight="c0",
              help="Measure the bare IDE (or the empty liquid cell) at the same frequency first. "
                   "ε′ ≈ Cp / C₀. For an IDE this gives an effective permittivity of film + substrate."),
        Field("cell_description", "Cell / electrode description", types=("liquid",), highlight="cell"),
    ], diagram=True),
    Section("Film preparation", [
        Field("prep_method", "Deposition method", "choice", choices=PREP_METHODS, weight=2),
        Field("solvent", "Solvent", placeholder="e.g. chlorobenzene"),
        Field("concentration", "Concentration", placeholder="e.g. 20 mg/mL"),
        Field("process_params", "Spin speed / rate / pressure", placeholder="e.g. 2000 rpm, 60 s"),
        Field("anneal", "Annealing / drying", placeholder="e.g. 120 °C, 10 min, N₂"),
        Field("prep_atmosphere", "Preparation atmosphere", "choice", choices=ATMOSPHERES),
        Field("prep_date", "Preparation date", placeholder="YYYY-MM-DD"),
        Field("storage", "Storage before measurement", placeholder="e.g. 2 days in glovebox"),
    ]),
    Section("Measurement conditions", [
        Field("temperature_c", "Temperature", "number", "°C", weight=2, placeholder="e.g. 23",
              help="Default temperature for the runs. Runs can also have their own temperature (table)."),
        Field("humidity", "Relative humidity", "number", "%", placeholder="e.g. 45",
              help="Water uptake strongly changes ε′ and the low-frequency loss of many films."),
        Field("meas_atmosphere", "Atmosphere", "choice", choices=ATMOSPHERES, weight=1),
        Field("illumination", "Light", "choice", choices=["Dark", "Room light", "Illuminated", "Unknown"]),
        Field("fixture", "Fixture / probes", placeholder="e.g. 9262 fixture, probe station with 2 needles"),
        Field("open_short_done", "Open/short compensation done", "choice", choices=YES_NO, weight=2),
        Field("stray_capacitance", "Open-circuit capacitance (probes lifted)", "number", "F", highlight="stray",
              placeholder="e.g. 0.5p", help="Measure with the probes lifted from the sample. "
              "If the sample capacitance is not ≫ this value, ε′ is uncertain."),
        Field("bias", "DC bias", placeholder="e.g. none, or 0.5 V"),
    ]),
    Section("Aim of the study", [
        Field("aim", "Aim / research question", "multiline", weight=3,
              placeholder="e.g. Determine ε′ of spin-coated PVDF and check whether annealing increases it."),
        Field("hypothesis", "Hypothesis / expectation", "multiline",
              placeholder="e.g. ε′ ≈ 8–12 at 1 kHz; relaxation of CF₂ dipoles above 1 MHz."),
        Field("notes", "Other notes", "multiline"),
    ]),
]

ALL_FIELDS = {f.key: f for s in SCHEMA for f in s.fields}
TYPE_FROM_LABEL = {v: k for k, v in SAMPLE_TYPES.items()}


@dataclass
class SampleInfo:
    values: dict = field(default_factory=dict)
    pubchem: dict = field(default_factory=dict)  # result of the look-up
    qa: list = field(default_factory=list)  # questions asked by the agent and the user's answers
    updated: str = ""

    # ------------------------------------------------------------ basics
    def get(self, key: str, default=None):
        v = self.values.get(key, default)
        return default if v in ("", None) else v

    def num(self, key: str) -> float:
        v = self.values.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return math.nan

    @property
    def sample_type(self) -> str:
        return TYPE_FROM_LABEL.get(self.values.get("sample_type", ""), "sandwich")

    @property
    def name(self) -> str:
        return str(self.get("material_name", "") or "")

    # --------------------------------------------------------- geometry
    def area(self) -> float:
        a = self.num("electrode_area")
        if a > 0:
            return a
        shape = self.values.get("electrode_shape", "Circle")
        if shape == "Rectangle":
            l, w = self.num("electrode_length"), self.num("electrode_width")
            return l * w if l > 0 and w > 0 else math.nan
        d = self.num("electrode_diameter")
        return math.pi * d * d / 4 if d > 0 else math.nan

    def geometric_capacitance(self) -> float:
        """C0 such that eps* = C*/C0. NaN if not enough information."""
        t = self.sample_type
        if t in ("sandwich", "pellet"):
            a, d = self.area(), self.num("thickness")
            if a > 0 and d > 0:
                return EPS0 * a / d
            return math.nan
        if t in ("ide", "liquid"):
            c0 = self.num("empty_capacitance")
            return c0 if c0 > 0 else math.nan
        return math.nan

    def geometry_text(self) -> str:
        t = self.sample_type
        if t in ("sandwich", "pellet"):
            a, d = self.area(), self.num("thickness")
            parts = []
            if a > 0:
                parts.append(f"electrode overlap area A = {a * 1e6:.4g} mm²")
            if d > 0:
                parts.append(f"thickness d = {d * 1e9:.4g} nm" if d < 1e-5 else f"thickness d = {d * 1e6:.4g} µm")
            c0 = self.geometric_capacitance()
            if c0 > 0:
                parts.append(f"C₀ = ε₀A/d = {c0:.4g} F")
            return ", ".join(parts) or "geometry incomplete"
        if t in ("ide", "liquid"):
            c0 = self.num("empty_capacitance")
            return f"empty-cell capacitance C₀ = {c0:.4g} F" if c0 > 0 else "C₀ not given"
        return "unknown geometry"

    # ------------------------------------------------------ completeness
    def visible_fields(self) -> list[Field]:
        t = self.sample_type
        return [f for f in ALL_FIELDS.values() if not f.types or t in f.types]

    def completeness(self) -> tuple[float, list[str]]:
        """Return (fraction 0..1, list of missing labels that are required for the analysis)."""
        t = self.sample_type
        total = got = 0
        missing_required = []
        for f in self.visible_fields():
            total += f.weight
            filled = self.values.get(f.key) not in ("", None)
            if f.key in ("electrode_diameter", "electrode_length", "electrode_width", "electrode_area"):
                filled = self.area() > 0
            if filled:
                got += f.weight
            elif t in f.required:
                missing_required.append(f.label)
        if t in ("sandwich", "pellet") and not self.area() > 0:
            missing_required.append("Electrode overlap area (diameter or length × width)")
        return (got / total if total else 0.0), missing_required

    # ------------------------------------------------------------- text
    def as_text(self, include_empty: bool = False) -> str:
        """Human readable description (used in the report and sent to the AI)."""
        lines = []
        for sec in SCHEMA:
            sec_lines = []
            for f in sec.fields:
                if f.types and self.sample_type not in f.types:
                    continue
                v = self.values.get(f.key)
                if v in ("", None) and not include_empty:
                    continue
                if f.kind == "number" and v not in ("", None):
                    v = format_value(float(v), f.unit)
                sec_lines.append(f"- {f.label}: {v}")
            if sec_lines:
                lines.append(f"{sec.title}:")
                lines.extend(sec_lines)
        if self.sample_type in ("sandwich", "pellet", "ide", "liquid"):
            lines.append(f"Derived geometry: {self.geometry_text()}")
        if self.qa:
            lines.append("Answers given by the researcher to earlier questions:")
            lines.extend(f"- Q: {q.get('q')}  A: {q.get('a')}" for q in self.qa if q.get("a"))
        return "\n".join(lines)

    def as_markdown(self) -> str:
        """Sample description as Markdown tables (for the report)."""
        out = []
        for sec in SCHEMA:
            rows = []
            for f in sec.fields:
                if f.types and self.sample_type not in f.types:
                    continue
                v = self.values.get(f.key)
                if v in ("", None):
                    continue
                if f.kind == "number":
                    v = format_value(float(v), f.unit)
                rows.append(f"| {f.label} | {str(v).replace('|', '/').replace(chr(10), ' ')} |")
            if sec.title == "Geometry" and self.sample_type in ("sandwich", "pellet", "ide", "liquid"):
                rows.append(f"| Derived geometry | {self.geometry_text()} |")
            if rows:
                out += [f"**{sec.title}**", "", "| Property | Value |", "|---|---|", *rows, ""]
        if self.qa:
            out += ["**Answers given to questions from the AI**", ""]
            out += [f"- *{q.get('q')}* — {q.get('a') or '(not answered)'}" for q in self.qa] + [""]
        return "\n".join(out)

    # ---------------------------------------------------------------- io
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated = datetime.now().isoformat(timespec="seconds")
        data = {"schema_version": 1, "updated": self.updated, "values": self.values,
                "pubchem": self.pubchem, "qa": self.qa}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SampleInfo":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(values=data.get("values", {}), pubchem=data.get("pubchem", {}), qa=data.get("qa", []),
                   updated=data.get("updated", ""))


def format_value(v: float, unit: str) -> str:
    if unit in ("", "%", "°C", "m²"):
        if unit == "m²":
            return f"{v * 1e6:.4g} mm²"
        return f"{v:g} {unit}".strip()
    from ..core.params import si_format

    return si_format(v, unit)
