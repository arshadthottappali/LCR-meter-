import re
from pathlib import Path

MANUAL = Path(__file__).resolve().parent.parent / "lcr_app" / "help" / "manual.html"


def test_manual_links_resolve():
    html = MANUAL.read_text(encoding="utf-8")
    anchors = set(re.findall(r'<a name="([^"]+)"', html))
    links = set(re.findall(r'<a href="#([^"]+)"', html))
    assert links, "manual has no internal links"
    assert links <= anchors, f"broken links: {links - anchors}"
    # anchors the Help menu jumps to
    assert {"top", "quick", "conn", "cal", "params", "signal", "trouble"} <= anchors
