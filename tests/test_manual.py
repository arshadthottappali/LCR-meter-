import re
from pathlib import Path

MANUAL = Path(__file__).resolve().parent.parent / "lcr_app" / "help" / "manual.html"
HTML = MANUAL.read_text(encoding="utf-8")


def test_manual_links_resolve():
    ids = set(re.findall(r'\bid="([^"]+)"', HTML))
    # href="#res" etc. are SVG symbol references; "${...}" are templates filled in by the search script
    links = {l for l in re.findall(r'href="#([^"]+)"', HTML) if "${" not in l} - {"res", "ind", "cap"}
    assert links, "manual has no internal links"
    assert links <= ids, f"broken links: {links - ids}"
    # sections the Help menu and the Help tab jump to
    assert {"top", "quick", "conn", "cal", "params", "signal", "trouble"} <= ids


def test_manual_has_all_sections_and_works_offline():
    from lcr_app.gui.main_window import manual_sections

    secs = manual_sections()
    titles = [s["title"] for s in secs]
    for n in range(1, 20):
        assert any(t.startswith(f"{n}. ") for t in titles), f"section {n} missing"
    assert all(s["text"].strip() for s in secs)
    # no resources from the internet (the manual must work offline)
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="https?:', HTML)
