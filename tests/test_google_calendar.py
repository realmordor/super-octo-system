from super_octo_system import google_calendar


def test_get_upcoming_events(monkeypatch):
    # Mock the Google API response
    class MockService:
        def events(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {
                "items": [
                    {
                        "start": {"dateTime": "2025-09-25T10:00:00Z"},
                        "summary": "Test Event 1",
                    },
                    {
                        "start": {"dateTime": "2025-09-25T12:00:00Z"},
                        "summary": "Test Event 2",
                    },
                ]
            }

    def mock_build(service_name, version, credentials=None):
        return MockService()

    class MockFlow:
        @staticmethod
        def from_client_secrets_file(filename, scopes):
            class Runner:
                def run_local_server(self, port=0):
                    class Creds:
                        def to_json(self):
                            return "{}"

                        @property
                        def valid(self):
                            return True

                    return Creds()

            return Runner()

    monkeypatch.setattr(google_calendar, "build", mock_build)
    monkeypatch.setattr(google_calendar, "Credentials", lambda *a, **kw: None)
    monkeypatch.setattr(google_calendar, "InstalledAppFlow", MockFlow)
    monkeypatch.setattr(google_calendar, "Request", lambda *a, **kw: None)
    events = google_calendar.get_upcoming_events(max_results=2)
    assert len(events) == 2
    assert events[0]["summary"] == "Test Event 1"
    assert events[1]["summary"] == "Test Event 2"
