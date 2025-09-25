from super_octo_system import met_office


def test_get_met_office_forecast(monkeypatch):
    class DummyResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"test": "ok"}

    def dummy_get(url, headers, params):
        return DummyResponse()

    monkeypatch.setattr(met_office.requests, "get", dummy_get)
    monkeypatch.setenv("MET_OFFICE_API_KEY", "dummy")
    result = met_office.get_met_office_forecast(51.6512, -0.1442)
    assert result == {"test": "ok"}
