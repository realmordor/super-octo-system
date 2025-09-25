import os
import importlib.util

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "../scripts/dashboard.py")


def test_dashboard_import():
    spec = importlib.util.spec_from_file_location("dashboard", DASHBOARD_PATH)
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)
    assert hasattr(dashboard, "main"), "dashboard.py should have a main() function"


# Optional: test main() does not raise errors when called (mocking Streamlit)
def test_dashboard_main_runs(monkeypatch):
    spec = importlib.util.spec_from_file_location("dashboard", DASHBOARD_PATH)
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)
    # Mock streamlit methods to avoid UI popups
    monkeypatch.setattr(dashboard.st, "title", lambda x: None)
    monkeypatch.setattr(dashboard.st, "error", lambda x: None)
    monkeypatch.setattr(
        dashboard.st, "selectbox", lambda *a, **k: list(dashboard.crs_codes.keys())[0]
    )
    monkeypatch.setattr(dashboard.st, "slider", lambda *a, **k: 10)
    monkeypatch.setattr(dashboard.st, "text_input", lambda *a, **k: "")
    monkeypatch.setattr(dashboard.st, "button", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.st, "subheader", lambda x: None)
    monkeypatch.setattr(dashboard.st, "dataframe", lambda x: None)
    os.environ["DARWIN_LITE_TOKEN"] = "dummy"
    dashboard.main()
