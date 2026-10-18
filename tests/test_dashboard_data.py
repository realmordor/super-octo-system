"""
Unit tests for scripts/dashboard_data.py.

Run with:  uv run pytest tests/test_dashboard_data.py
"""

from unittest.mock import MagicMock, patch
import datetime
import gzip
import queue
import sys
import urllib.parse

import pandas as pd
import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import scripts.dashboard_data as d  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_caches():
    """Reset TTL caches before each test to prevent state leakage."""
    d._weather_cache.clear()
    d._menu_cache.clear()
    d._calendar_cache.clear()
    d._services.clear()
    d._darwin_status.update(ready=False, connected=False, last_message=None, error=None)
    yield


@pytest.fixture
def menu_sheet():
    return pd.DataFrame(
        {
            "label": ["remove", "overwrite", "final"],
            "Recipe A": [False, None, "Recipe A"],
            "Recipe B": [True, None, None],
            "Recipe C": [False, "Recipe C Override", "Recipe C Override"],
        }
    )


NOW = datetime.datetime(2026, 9, 15, 11, 50)


def _pport(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Pport xmlns="http://www.thalesgroup.com/rtti/PushPort/v16"'
        ' xmlns:ns2="http://www.thalesgroup.com/rtti/PushPort/Schedules/v3"'
        ' xmlns:ns5="http://www.thalesgroup.com/rtti/PushPort/Forecasts/v3"'
        f' ts="2026-09-15T12:00:00+01:00" version="16.0"><uR>{body}</uR></Pport>'
    ).encode()


def _schedule(
    rid: str = "RID1",
    ssd: str = "2026-09-15",
    origin: str = "11:40",
    ptd: str = "12:00",
    arrive: str = "12:40",
    dest: str = "MRGT",
    cancelled: bool = False,
    deleted: bool = False,
) -> bytes:
    deleted_attr = ' deleted="true"' if deleted else ""
    can_attr = ' can="true"' if cancelled else ""
    return _pport(
        f'<schedule rid="{rid}" uid="A1" ssd="{ssd}" toc="GN"{deleted_attr}>'
        f'<ns2:OR wtd="{origin}" ptd="{origin}" tpl="WLWYNGC" act="TB"/>'
        f'<ns2:PP wtp="{origin}:30" tpl="KNEBWTH"/>'
        f'<ns2:IP wta="{ptd}" wtd="{ptd}:30" tpl="HADLYWD" pta="{ptd}" ptd="{ptd}"{can_attr}/>'
        f'<ns2:DT wta="{arrive}" tpl="{dest}" pta="{arrive}"/>'
        "</schedule>"
    )


def _ts(rid: str = "RID1", ptd: str = "12:00", dep: str | None = None, plat: str | None = None) -> bytes:
    return _pport(
        f'<TS rid="{rid}" uid="A1" ssd="2026-09-15">'
        f'<ns5:Location tpl="HADLYWD" wta="{ptd}" wtd="{ptd}:30" pta="{ptd}" ptd="{ptd}">'
        f"{dep or ''}{plat or ''}</ns5:Location></TS>"
    )


# ── _sheet_url ────────────────────────────────────────────────────────────────


def test_sheet_url_by_name():
    url = d._sheet_url("SHEET123", name="My Sheet")
    assert "SHEET123" in url
    assert f"sheet={urllib.parse.quote('My Sheet')}" in url
    assert "gid" not in url


def test_sheet_url_by_gid():
    url = d._sheet_url("SHEET123", gid="99999")
    assert "SHEET123" in url
    assert "gid=99999" in url
    assert "sheet=" not in url


def test_sheet_url_encodes_special_characters():
    url = d._sheet_url("SHEET123", name="Form Responses 1")
    assert "Form%20Responses%201" in url or "Form+Responses+1" in url


def test_sheet_url_base_points_to_google_sheets():
    url = d._sheet_url("SHEET123", gid="0")
    assert url.startswith("https://docs.google.com/spreadsheets/d/")
    assert "export?format=csv" in url


# ── get_menu_recipe_names ─────────────────────────────────────────────────────


def test_get_menu_recipe_names_returns_final_row_values(menu_sheet):
    with patch.object(d, "_load_menu_sheet", return_value=menu_sheet):
        assert d.get_menu_recipe_names() == {"Recipe A", "Recipe C Override"}


def test_get_menu_recipe_names_excludes_removed_recipes(menu_sheet):
    with patch.object(d, "_load_menu_sheet", return_value=menu_sheet):
        assert "Recipe B" not in d.get_menu_recipe_names()


def test_get_menu_recipe_names_empty_when_no_final_row():
    df = pd.DataFrame({"label": ["remove", "overwrite"], "Recipe A": [False, None]})
    with patch.object(d, "_load_menu_sheet", return_value=df):
        assert d.get_menu_recipe_names() == set()


def test_get_menu_recipe_names_strips_whitespace():
    df = pd.DataFrame({"label": ["final"], "Recipe A": ["  Recipe A  "]})
    with patch.object(d, "_load_menu_sheet", return_value=df):
        assert "Recipe A" in d.get_menu_recipe_names()


def test_get_menu_recipe_names_all_nan_in_final_row():
    df = pd.DataFrame({"label": ["final"], "Recipe A": [None], "Recipe B": [None]})
    with patch.object(d, "_load_menu_sheet", return_value=df):
        assert d.get_menu_recipe_names() == set()


# ── get_weekly_menu ───────────────────────────────────────────────────────────


def test_get_weekly_menu_skips_first_three_rows():
    df = pd.DataFrame(
        {"A": ["remove", "overwrite", "final", "data1", "data2"], "B": [1, 2, 3, 4, 5]}
    )
    with patch.object(d, "_load_menu_sheet", return_value=df):
        result = d.get_weekly_menu()
    # Column "A" is renamed to "final" (the value in the final row for that column)
    assert list(result["final"]) == ["data1", "data2"]


def test_get_weekly_menu_drops_all_empty_columns():
    df = pd.DataFrame({"A": ["r", "o", "f", "x"], "B": [None, None, None, None]})
    with patch.object(d, "_load_menu_sheet", return_value=df):
        assert "B" not in d.get_weekly_menu().columns


def test_get_weekly_menu_resets_index():
    df = pd.DataFrame({"A": ["r", "o", "f", "x", "y"]})
    with patch.object(d, "_load_menu_sheet", return_value=df):
        result = d.get_weekly_menu()
    assert list(result.index) == list(range(len(result)))


# ── get_weather ───────────────────────────────────────────────────────────────


def test_get_weather_raises_when_no_api_key():
    with patch("os.getenv", return_value=""):
        with pytest.raises(ValueError, match="MET_OFFICE_API_KEY"):
            d.get_weather(51.0, -0.1)


def test_get_weather_caches_result():
    mock_response = MagicMock()
    mock_response.json.return_value = {"features": []}
    with patch("os.getenv", return_value="fake-key"):
        with patch("requests.get", return_value=mock_response) as mock_get:
            d.get_weather(51.0, -0.1)
            d.get_weather(51.0, -0.1)
    mock_get.assert_called_once()


# ── Darwin Push Port parsing ──────────────────────────────────────────────────


def test_apply_pushport_ignores_messages_without_tracked_stations():
    services: dict = {}
    msg = _schedule().replace(b"HADLYWD", b"KNGX").replace(b"WLWYNGC", b"STEVNGE").replace(b"MRGT", b"CAMBDGE")
    d._apply_pushport(msg, services)
    assert services == {}


def test_apply_pushport_records_public_calls_at_tracked_stations():
    services: dict = {}
    d._apply_pushport(_schedule(), services)
    call = services["RID1"]["calls"]["HADLYWD"]
    assert call["ptd"] == "12:00"
    assert call["std"] == datetime.datetime(2026, 9, 15, 12, 0)
    assert services["RID1"]["dest"] == "MRGT"


def test_apply_pushport_dates_calls_after_midnight_on_the_next_day():
    services: dict = {}
    d._apply_pushport(_schedule(origin="23:40", ptd="00:05", arrive="00:45"), services)
    assert services["RID1"]["calls"]["HADLYWD"]["std"] == datetime.datetime(2026, 9, 16, 0, 5)


def test_apply_pushport_removes_deleted_and_deactivated_services():
    services: dict = {}
    d._apply_pushport(_schedule(rid="A"), services)
    d._apply_pushport(_schedule(rid="B"), services)
    d._apply_pushport(_schedule(rid="A", deleted=True), services)
    d._apply_pushport(_pport('<deactivated rid="B"/>'), services)
    assert services == {}


def test_apply_pushport_merges_partial_forecast_updates():
    services: dict = {}
    d._apply_pushport(_schedule(), services)
    d._apply_pushport(_ts(dep='<ns5:dep et="12:03"/>', plat="<ns5:plat>2</ns5:plat>"), services)
    d._apply_pushport(_ts(dep='<ns5:dep et="12:05"/>'), services)
    assert services["RID1"]["fc"]["HADLYWD"] == {"et": "12:05", "at": None, "delayed": False, "plat": "2"}


def test_apply_pushport_schedule_update_keeps_forecasts_received_first():
    services: dict = {}
    d._apply_pushport(_ts(dep='<ns5:dep et="12:04"/>'), services)
    d._apply_pushport(_schedule(), services)
    assert services["RID1"]["fc"]["HADLYWD"]["et"] == "12:04"
    assert "HADLYWD" in services["RID1"]["calls"]


def test_apply_pushport_records_arrivals_at_tracked_stations():
    services: dict = {}
    d._apply_pushport(_schedule(arrive="12:40"), services)
    arrs = services["RID1"]["arrs"]
    assert arrs["MRGT"] == {"pta": "12:40", "sta": datetime.datetime(2026, 9, 15, 12, 40)}
    assert arrs["HADLYWD"]["pta"] == "12:00"


def test_apply_pushport_records_arrival_forecasts_at_the_destination():
    services: dict = {}
    d._apply_pushport(_schedule(), services)
    d._apply_pushport(
        _pport(
            '<TS rid="RID1" uid="A1" ssd="2026-09-15">'
            '<ns5:Location tpl="MRGT" wta="12:40" pta="12:40"><ns5:arr et="12:44"/></ns5:Location></TS>'
        ),
        services,
    )
    assert services["RID1"]["fc"]["MRGT"] == {"arr_et": "12:44", "arr_at": None}


def _station_message(msg_id: str = "1", crs: tuple[str, ...] = ("HDW",), text: str = "Lifts out of order") -> bytes:
    stations = "".join(f'<ns7:Station crs="{c}"/>' for c in crs)
    body = f"<ns7:Msg>{text}</ns7:Msg>" if text else ""
    return _pport(
        f'<OW xmlns:ns7="http://www.thalesgroup.com/rtti/PushPort/StationMessages/v1" id="{msg_id}" cat="Station" sev="1">'
        f"{stations}{body}</OW>"
    )


def test_apply_pushport_keeps_station_messages_for_tracked_stations():
    messages: dict = {}
    d._apply_pushport(_station_message(crs=("HDW", "KGX")), {}, messages)
    assert messages == {"1": {"crs": ["HDW"], "sev": 1, "text": "Lifts out of order"}}


def test_apply_pushport_ignores_station_messages_for_other_stations():
    messages: dict = {}
    d._apply_pushport(_station_message(crs=("KGX",)), {}, messages)
    assert messages == {}


def test_apply_pushport_removes_withdrawn_station_messages():
    messages: dict = {}
    d._apply_pushport(_station_message(), {}, messages)
    d._apply_pushport(_station_message(crs=(), text=""), {}, messages)
    assert messages == {}


def test_prune_services_drops_old_services():
    services: dict = {}
    d._apply_pushport(_schedule(rid="OLD", origin="06:40", ptd="07:00", arrive="07:40"), services)
    d._apply_pushport(_schedule(rid="NEW"), services)
    d._prune_services(services, NOW)
    assert list(services) == ["NEW"]


# ── Darwin feed ───────────────────────────────────────────────────────────────


def _fake_sftp(files: dict[str, tuple[float, bytes]]) -> MagicMock:
    """files: path -> (mtime, uncompressed content)"""
    sftp = MagicMock()
    sftp.stat.side_effect = lambda path: MagicMock(st_mtime=files[path][0])
    sftp.listdir_attr.return_value = [
        MagicMock(filename=path.split("/", 1)[1], st_mtime=mtime)
        for path, (mtime, _) in files.items()
        if path.startswith("pushport/")
    ]
    sftp.getfo.side_effect = lambda path, buf: buf.write(gzip.compress(files[path][1]))
    return sftp


def _patch_sftp(sftp: MagicMock):
    cm = MagicMock()
    cm.__enter__.return_value = sftp
    return patch.object(d, "_darwin_sftp", return_value=cm)


def test_load_darwin_state_applies_snapshot_then_newer_logs_only():
    sftp = _fake_sftp({
        "snapshot/snapshot.gz": (100.0, _schedule()),
        "pushport/old.gz": (50.0, _schedule(deleted=True)),
        "pushport/new.gz": (200.0, _ts(dep='<ns5:dep et="12:04"/>')),
    })
    with _patch_sftp(sftp):
        services = d._load_darwin_state()
    assert services["RID1"]["fc"]["HADLYWD"]["et"] == "12:04"
    assert [c.args[0] for c in sftp.getfo.call_args_list] == ["snapshot/snapshot.gz", "pushport/new.gz"]


def test_darwin_listener_queues_messages_and_records_errors():
    inbox: queue.Queue = queue.Queue()
    listener = d._DarwinListener(inbox)
    listener.on_message(MagicMock(body=b"payload"))
    listener.on_error(MagicMock(headers={"message": "Invalid username or password"}))
    assert inbox.get_nowait() == b"payload"
    assert listener.error == "Invalid username or password"


def test_follow_darwin_feed_loads_state_then_applies_stream():
    conn = MagicMock()
    conn.is_connected.side_effect = [True, False, False]

    def fake_connect(listener):
        listener.inbox.put(gzip.compress(_ts(dep='<ns5:dep et="12:07"/>')))
        return conn

    snapshot_state = {}
    d._apply_pushport(_schedule(), snapshot_state)
    with (
        patch.object(d, "_connect_stomp", side_effect=fake_connect),
        patch.object(d, "_load_darwin_state", return_value=snapshot_state),
        patch.object(d, "_now_london", return_value=NOW),
    ):
        d._follow_darwin_feed()
    assert d._services["RID1"]["fc"]["HADLYWD"]["et"] == "12:07"
    assert d._darwin_status["ready"] is True
    assert d._darwin_status["connected"] is False
    assert d._darwin_status["last_message"] is not None
