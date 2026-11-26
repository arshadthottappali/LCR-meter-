from lcr_app.instrument.diagnostics import DRIVER_LINKS, check_drivers


def test_report_runs_and_renders():
    rep = check_drivers(list_visa=False)
    assert rep.checks and rep.advice
    html = rep.to_html("Connection failed: test")
    assert "Connection failed: test" in html
    for _, _, url in DRIVER_LINKS:
        assert url in html
