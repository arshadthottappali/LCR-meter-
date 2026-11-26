"""SVG diagrams of the sample geometry. The element that belongs to the
field being edited is highlighted (orange), so it is clear what is asked for."""
from __future__ import annotations

HL = "#E69F00"
ELEC = "#B8B8B8"
ELEC_TOP = "#C9A227"
FILM = "#7EC8F0"
SUB = "#D9D4CC"
TXT = "#333333"


def _style(el: str, hl: str, base_fill: str, stroke: str = "#555") -> str:
    if el == hl:
        return f'fill="{base_fill}" stroke="{HL}" stroke-width="3"'
    return f'fill="{base_fill}" stroke="{stroke}" stroke-width="1"'


def _arrow(x1, y1, x2, y2, label, el, hl, lx, ly, anchor="middle") -> str:
    c = HL if el == hl else "#444"
    w = 2.5 if el == hl else 1.2
    fw = "bold" if el == hl else "normal"
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{c}" stroke-width="{w}" '
            f'marker-start="url(#a{"h" if el == hl else ""})" marker-end="url(#a{"h" if el == hl else ""})"/>'
            f'<text x="{lx}" y="{ly}" font-size="12" fill="{c}" font-weight="{fw}" text-anchor="{anchor}">{label}</text>')


def _defs() -> str:
    m = ""
    for suffix, color in (("", "#444"), ("h", HL)):
        m += (f'<marker id="a{suffix}" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" '
              f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')
    return f"<defs>{m}</defs>"


def _wrap(body: str, w=380, h=220) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'font-family="Segoe UI, Arial, sans-serif">{_defs()}<rect width="{w}" height="{h}" fill="white"/>'
            f"{body}</svg>")


def _label(x, y, text, el="", hl="", size=11, anchor="start") -> str:
    c = HL if el and el == hl else TXT
    fw = "bold" if el and el == hl else "normal"
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{c}" font-weight="{fw}" text-anchor="{anchor}">{text}</text>'


def sandwich(hl: str = "") -> str:
    b = _label(8, 16, "Cross-section", size=12) + _label(250, 16, "Top view", size=12)
    # probe needle (stray capacitance)
    pc = HL if hl == "stray" else "#777"
    b += f'<line x1="110" y1="28" x2="110" y2="84" stroke="{pc}" stroke-width="{3 if hl == "stray" else 1.5}"/>'
    b += _label(116, 40, "probe", "stray", hl, 10)
    b += f'<rect x="10" y="160" width="200" height="24" {_style("substrate", hl, SUB)}/>'
    b += f'<rect x="10" y="148" width="200" height="12" {_style("bottom", hl, ELEC)}/>'
    b += f'<rect x="10" y="108" width="200" height="40" {_style("film", hl, FILM)}/>'
    b += f'<rect x="70" y="96" width="80" height="12" {_style("top", hl, ELEC_TOP)}/>'
    b += _arrow(70, 82, 150, 82, "Ø  or  L", "diameter" if hl != "length" else "length", hl, 110, 76)
    b += _arrow(196, 108, 196, 148, "d", "thickness", hl, 204, 133, "start")
    b += _label(14, 130, "film", "film", hl)
    b += _label(14, 158, "bottom electrode", "bottom", hl, 9)
    b += _label(14, 177, "substrate", "substrate", hl, 10)
    b += _label(14, 104, "top electrode →", "top", hl, 9)
    # top view: bottom strip + top electrode, overlap hatched
    b += f'<rect x="255" y="40" width="40" height="150" {_style("bottom", hl, ELEC)}/>'
    if hl == "width":
        b += f'<rect x="225" y="95" width="120" height="40" fill="{ELEC_TOP}" stroke="{HL}" stroke-width="3" opacity="0.85"/>'
        b += _arrow(352, 95, 352, 135, "W", "width", hl, 358, 119, "start")
    else:
        b += f'<circle cx="275" cy="115" r="26" {_style("top", hl, ELEC_TOP)} opacity="0.9"/>'
    ac = HL if hl in ("area", "diameter", "length", "width") else "#333"
    b += (f'<rect x="262" y="102" width="26" height="26" fill="none" stroke="{ac}" '
          f'stroke-dasharray="3,2" stroke-width="{2.5 if ac == HL else 1}"/>')
    b += _label(300, 70, "overlap", "area", hl, 10) + _label(300, 82, "area A", "area", hl, 10)
    b += _label(226, 206, "A = overlap of top &amp; bottom", "area", hl, 9)
    return _wrap(b)


def pellet(hl: str = "") -> str:
    b = _label(8, 16, "Pellet / disc between electrodes", size=12)
    b += f'<ellipse cx="190" cy="70" rx="120" ry="22" {_style("top", hl, ELEC_TOP)}/>'
    b += f'<rect x="70" y="70" width="240" height="70" {_style("film", hl, FILM)}/>'
    b += f'<ellipse cx="190" cy="140" rx="120" ry="22" {_style("bottom", hl, ELEC)}/>'
    b += f'<ellipse cx="190" cy="70" rx="120" ry="22" {_style("top", hl, ELEC_TOP)}/>'
    b += _arrow(70, 190, 310, 190, "electrode diameter Ø", "diameter", hl, 190, 210)
    b += _arrow(330, 70, 330, 140, "d", "thickness", hl, 338, 110, "start")
    b += _label(150, 110, "sample", "film", hl)
    return _wrap(b)


def ide(hl: str = "") -> str:
    b = _label(8, 16, "Interdigitated electrodes (top view)", size=12)
    b += f'<rect x="40" y="30" width="260" height="14" {_style("fingers", hl, ELEC_TOP)}/>'
    b += f'<rect x="40" y="176" width="260" height="14" {_style("fingers", hl, ELEC_TOP)}/>'
    for k in range(6):
        x = 55 + k * 40
        if k % 2 == 0:
            b += f'<rect x="{x}" y="44" width="14" height="112" {_style("fingers", hl, ELEC_TOP)}/>'
        else:
            b += f'<rect x="{x}" y="64" width="14" height="112" {_style("fingers", hl, ELEC_TOP)}/>'
    b += f'<rect x="40" y="44" width="260" height="132" fill="{FILM}" opacity="0.25" stroke="none"/>'
    b += _arrow(69, 120, 95, 120, "g", "gap", hl, 82, 114)
    b += _arrow(55, 166, 69, 166, "w", "finger_width", hl, 62, 160)
    b += _arrow(320, 64, 320, 156, "L", "ide_length", hl, 328, 114, "start")
    b += _label(40, 208, "N pairs = number of finger pairs", "fingers", hl, 10)
    b += _label(230, 208, "C₀ = bare IDE", "c0", hl, 10)
    return _wrap(b)


def liquid(hl: str = "") -> str:
    b = _label(8, 16, "Liquid cell", size=12)
    b += f'<rect x="90" y="40" width="200" height="150" fill="none" {_style("cell", hl, "none")}/>'
    b += f'<rect x="92" y="80" width="196" height="108" fill="{FILM}" opacity="0.6"/>'
    b += f'<rect x="130" y="50" width="12" height="130" {_style("top", hl, ELEC)}/>'
    b += f'<rect x="238" y="50" width="12" height="130" {_style("bottom", hl, ELEC)}/>'
    b += _label(150, 130, "liquid / solution")
    b += _label(60, 210, "C₀ = capacitance of the EMPTY cell (same cables)", "c0", hl, 10)
    return _wrap(b)


def unknown(hl: str = "") -> str:
    b = _label(8, 16, "Unknown geometry", size=12)
    b += '<rect x="110" y="50" width="160" height="110" fill="#f4f4f4" stroke="#999" stroke-dasharray="6,4"/>'
    b += '<text x="190" y="120" font-size="48" text-anchor="middle" fill="#999">?</text>'
    b += _label(40, 195, "Only Cp, D, |Z| and θ can be analysed.", size=11)
    b += _label(40, 211, "Choose a sample type to get permittivity.", size=11)
    return _wrap(b)


DIAGRAMS = {"sandwich": sandwich, "pellet": pellet, "ide": ide, "liquid": liquid, "unknown": unknown}


def svg_for(sample_type: str, highlight: str = "") -> str:
    return DIAGRAMS.get(sample_type, unknown)(highlight)
