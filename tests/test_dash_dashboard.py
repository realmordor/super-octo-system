"""
Unit tests for scripts/dash_dashboard.py.

Run with:  uv run pytest tests/test_dash_dashboard.py
"""

from unittest.mock import MagicMock, patch
import datetime
import gzip
import queue
import sys
import urllib.parse

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import pytest
from dash import html

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import scripts.dash_dashboard as d  # noqa: E402


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


@pytest.fixture
def ingredients_df():
    return pd.DataFrame(
        {
            "recipe": ["Recipe A", None, None, "Recipe B", None],
            "key": ["onion", "garlic", "tomato", "chicken", "onion"],
            "display": ["Onion", "Garlic", "Tomato", "Chicken", "Onion"],
            "amount": [2, 3, 4, 1, 1],
            "unit": ["medium", "cloves", "tin", "kg", "medium"],
        }
    )


@pytest.fixture
def schedule_df():
    return pd.DataFrame(
        {
            "Timestamp": ["2024-01-01 10:00"],
            "Breakfast Monday": ["Porridge"],
            "Breakfast Tuesday": ["Toast"],
            "Lunch Monday": ["Soup"],
            "Notes": ["All good"],
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


def _board(*messages: bytes, now: datetime.datetime = NOW, dest: str | None = "MRGT") -> list[dict]:
    services: dict = {}
    for m in messages:
        d._apply_pushport(m, services)
    return d._departures_from(services, "HADLYWD", dest, now)


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


# ── get_schedule_summary ──────────────────────────────────────────────────────


def test_get_schedule_summary_groups_by_day(schedule_df):
    with patch("pandas.read_csv", return_value=schedule_df):
        result = d.get_schedule_summary()
    assert "Monday" in result.columns
    assert "Tuesday" in result.columns


def test_get_schedule_summary_drops_timestamp(schedule_df):
    with patch("pandas.read_csv", return_value=schedule_df):
        result = d.get_schedule_summary()
    assert "Timestamp" not in result.columns


def test_get_schedule_summary_returns_empty_for_empty_input():
    with patch("pandas.read_csv", return_value=pd.DataFrame()):
        assert d.get_schedule_summary().empty


def test_get_schedule_summary_uses_latest_row():
    df = pd.DataFrame(
        {
            "Timestamp": ["2024-01-01", "2024-01-08"],
            "Breakfast Monday": ["Old Porridge", "New Porridge"],
        }
    )
    with patch("pandas.read_csv", return_value=df):
        result = d.get_schedule_summary()
    assert "New Porridge" in result.values


def test_get_schedule_summary_strips_brackets_from_entry():
    df = pd.DataFrame({"[Breakfast] Monday": ["Porridge"], "Timestamp": ["2024-01-01"]})
    with patch("pandas.read_csv", return_value=df):
        result = d.get_schedule_summary()
    if "Entry" in result.columns:
        assert not result["Entry"].str.contains(r"[\[\]\(\)]", regex=True).any()


# ── get_ingredients ───────────────────────────────────────────────────────────


def test_get_ingredients_filters_to_menu_recipes(ingredients_df):
    with patch.object(d, "get_menu_recipe_names", return_value={"Recipe A"}):
        with patch("pandas.read_csv", return_value=ingredients_df):
            result = d.get_ingredients()
    assert "Onion" in result
    assert "Chicken" not in result


def test_get_ingredients_returns_sorted_unique(ingredients_df):
    with patch.object(
        d, "get_menu_recipe_names", return_value={"Recipe A", "Recipe B"}
    ):
        with patch("pandas.read_csv", return_value=ingredients_df):
            result = d.get_ingredients()
    assert result == sorted(set(result))
    assert result.count("Onion") == 1


def test_get_ingredients_returns_empty_when_no_menu_recipes(ingredients_df):
    with patch.object(d, "get_menu_recipe_names", return_value=set()):
        with patch("pandas.read_csv", return_value=ingredients_df):
            assert d.get_ingredients() == []


# ── get_recipes_for_ingredient ────────────────────────────────────────────────


def test_get_recipes_for_ingredient_finds_match(ingredients_df):
    with patch("pandas.read_csv", return_value=ingredients_df):
        result = d.get_recipes_for_ingredient("Onion")
    assert "Recipe A" in result
    assert "Recipe B" in result


def test_get_recipes_for_ingredient_case_insensitive(ingredients_df):
    with patch("pandas.read_csv", return_value=ingredients_df):
        assert "Recipe A" in d.get_recipes_for_ingredient("onion")


def test_get_recipes_for_ingredient_no_match(ingredients_df):
    with patch("pandas.read_csv", return_value=ingredients_df):
        assert d.get_recipes_for_ingredient("unicorn meat") == {}


def test_get_recipes_for_ingredient_formats_amount_and_unit(ingredients_df):
    with patch("pandas.read_csv", return_value=ingredients_df):
        assert d.get_recipes_for_ingredient("Onion")["Recipe A"] == "2 medium"


def test_get_recipes_for_ingredient_no_amount_or_unit():
    df = pd.DataFrame(
        {
            "recipe": ["R"],
            "key": ["s"],
            "display": ["Salt"],
            "amount": [None],
            "unit": [None],
        }
    )
    with patch("pandas.read_csv", return_value=df):
        assert d.get_recipes_for_ingredient("Salt")["R"] == "Amount not specified"


def test_get_recipes_for_ingredient_deduplicates_recipes(ingredients_df):
    with patch("pandas.read_csv", return_value=ingredients_df):
        result = d.get_recipes_for_ingredient("Onion")
    assert list(result.keys()).count("Recipe A") == 1


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


# ── Helpers ───────────────────────────────────────────────────────────────────


def test_table_returns_dbc_table():
    df = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
    result = d._table(df)
    assert isinstance(result, dbc.Table)


def test_error_returns_danger_alert():
    result = d._error("something went wrong")
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"


def test_warn_returns_warning_alert():
    result = d._warn("heads up")
    assert isinstance(result, dbc.Alert)
    assert result.color == "warning"


# ── toggle_section callback ───────────────────────────────────────────────────


def test_toggle_section_collapses_when_open():
    assert d.toggle_section(1, True) is False


def test_toggle_section_expands_when_closed():
    assert d.toggle_section(1, False) is True


# ── update_calendar callback ──────────────────────────────────────────────────


def test_update_calendar_returns_list_of_events():
    events = [
        {
            "summary": "Dentist",
            "start": "2026-05-20T10:00:00Z",
            "end": "2026-05-20T11:00:00Z",
        }
    ]
    with patch.object(d, "get_upcoming_events", return_value=events):
        result = d.update_calendar(1)
    assert isinstance(result, list)
    assert result[0]["title"] == "Dentist"


def test_update_calendar_returns_empty_list_on_error():
    with patch.object(d, "get_upcoming_events", side_effect=Exception("auth failed")):
        result = d.update_calendar(1)
    assert result == []


def test_update_calendar_preserves_start_and_end():
    events = [
        {"summary": "E", "start": "2026-05-20T10:00:00Z", "end": "2026-05-20T11:00:00Z"}
    ]
    with patch.object(d, "get_upcoming_events", return_value=events):
        result = d.update_calendar(1)
    assert result[0]["start"] == "2026-05-20T10:00:00Z"
    assert result[0]["end"] == "2026-05-20T11:00:00Z"


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


def test_prune_services_drops_old_services():
    services: dict = {}
    d._apply_pushport(_schedule(rid="OLD", origin="06:40", ptd="07:00", arrive="07:40"), services)
    d._apply_pushport(_schedule(rid="NEW"), services)
    d._prune_services(services, NOW)
    assert list(services) == ["NEW"]


# ── departure board ───────────────────────────────────────────────────────────


def test_departures_on_time_train():
    assert _board(_schedule(), _ts(plat="<ns5:plat>1</ns5:plat>")) == [
        {"Time": "12:00", "Destination": "Moorgate", "Platform": "1", "ETD": "On time"}
    ]


def test_departures_filters_by_destination():
    assert _board(_schedule(dest="WLWYNGC")) == []
    assert len(_board(_schedule(dest="WLWYNGC"), dest=None)) == 1


def test_departures_keeps_delayed_train_until_expected_time():
    now = datetime.datetime(2026, 9, 15, 12, 6)
    rows = _board(_schedule(), _ts(dep='<ns5:dep et="12:08"/>'), now=now)
    assert rows[0]["ETD"] == "12:08"


def test_departures_drops_departed_and_past_trains():
    departed = _board(_schedule(), _ts(dep='<ns5:dep at="11:59"/>'))
    past = _board(_schedule(origin="11:00", ptd="11:20", arrive="12:00"))
    assert departed == [] and past == []


def test_departures_shows_cancelled_delayed_and_suppressed_platform():
    cancelled = _board(_schedule(cancelled=True))[0]
    delayed = _board(_schedule(), _ts(dep='<ns5:dep et="12:00" delayed="true"/>'))[0]
    suppressed = _board(_schedule(), _ts(plat='<ns5:plat platsup="true">3</ns5:plat>'))[0]
    assert cancelled["ETD"] == "Cancelled"
    assert delayed["ETD"] == "Delayed"
    assert suppressed["Platform"] == "—"


def test_departures_sorted_by_date_across_midnight():
    now = datetime.datetime(2026, 9, 15, 23, 30)
    rows = _board(
        _schedule(rid="TOMORROW", ssd="2026-09-16", origin="05:40", ptd="06:00", arrive="06:40"),
        _schedule(rid="AFTER_MIDNIGHT", origin="23:50", ptd="00:10", arrive="00:50"),
        _schedule(rid="TONIGHT", origin="23:30", ptd="23:50", arrive="00:30"),
        now=now,
    )
    assert [r["Time"] for r in rows] == ["23:50", "00:10", "06:00"]


def test_get_departures_reads_live_state():
    d._apply_pushport(_schedule(), d._services)
    with patch.object(d, "_now_london", return_value=NOW):
        location, rows = d.get_departures("HDW", "MOG")
    assert location == "Hadley Wood"
    assert rows[0]["Time"] == "12:00"


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


# ── update_trains callback ────────────────────────────────────────────────────


@pytest.fixture
def darwin_env():
    env = {k: "x" for k in d._DARWIN_REQUIRED_ENV}
    with patch.dict("os.environ", env):
        yield


def _make_departure(time: str) -> dict:
    return {"Time": time, "Destination": "Moorgate", "Platform": "1", "ETD": "On time"}


def _live(**status):
    d._darwin_status.update(
        {"ready": True, "connected": True, "last_message": datetime.datetime.now(d._LONDON), **status}
    )


def test_update_trains_errors_when_credentials_missing():
    env = {k: "" for k in d._DARWIN_REQUIRED_ENV}
    with patch.dict("os.environ", env):
        result = d.update_trains(1, 0, "HDW", "MOG")
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"
    assert "DARWIN_STOMP_USER" in result.children


def test_update_trains_shows_loading_until_state_ready(darwin_env):
    result = d.update_trains(1, 0, "HDW", "MOG")
    assert isinstance(result, dbc.Alert)
    assert result.color == "info"


def test_update_trains_shows_feed_error_before_ready(darwin_env):
    d._darwin_status["error"] = "Invalid username or password"
    result = d.update_trains(1, 0, "HDW", "MOG")
    assert result.color == "danger"
    assert "Invalid username or password" in result.children


def test_update_trains_shows_at_most_five_rows(darwin_env):
    _live()
    departures = [_make_departure(f"1{i}:00") for i in range(8)]
    with patch.object(d, "get_departures", return_value=("Hadley Wood", departures)):
        result = d.update_trains(1, 0, "HDW", "MOG")
    caption, table = result.children
    assert "live" in caption.children
    rows = [c for c in table.children[1].children if hasattr(c, "children")]
    assert len(rows) == 5


def test_update_trains_warns_when_disconnected(darwin_env):
    _live(connected=False)
    with patch.object(d, "get_departures", return_value=("Hadley Wood", [_make_departure("15:30")])):
        result = d.update_trains(1, 0, "HDW", "MOG")
    assert result.children[0].color == "warning"
    assert "disconnected" in result.children[0].children


def test_update_trains_warns_when_updates_stall(darwin_env):
    _live(last_message=datetime.datetime.now(d._LONDON) - datetime.timedelta(minutes=5))
    with patch.object(d, "get_departures", return_value=("Hadley Wood", [_make_departure("15:30")])):
        result = d.update_trains(1, 0, "HDW", "MOG")
    assert "No live updates" in result.children[0].children


def test_trains_interval_is_fast_while_live_data_loads(darwin_env):
    assert d.update_trains_interval(1, 15_000) == 1_000
    assert d.update_trains_interval(2, 1_000) is dash.no_update


def test_trains_interval_is_normal_once_ready_or_failed(darwin_env):
    _live()
    assert d.update_trains_interval(1, 1_000) == 15_000
    assert d.update_trains_interval(2, 15_000) is dash.no_update
    d._darwin_status.update(ready=False, error="Invalid username or password")
    assert d.update_trains_interval(3, 1_000) == 15_000


def test_trains_interval_is_normal_without_credentials():
    with patch.dict("os.environ", {k: "" for k in d._DARWIN_REQUIRED_ENV}):
        assert d.update_trains_interval(1, 1_000) == 15_000


def test_report_server_started_returns_this_process_start_id():
    assert d.report_server_started(1) == d.SERVER_STARTED


def test_update_trains_returns_warning_when_empty(darwin_env):
    _live()
    with patch.object(d, "get_departures", return_value=("Hadley Wood", [])):
        result = d.update_trains(1, 0, "HDW", "MOG")
    assert result.children[-1].color == "warning"


# ── trains table colours ──────────────────────────────────────────────────────


def _row_colours(departures: list[dict], now: datetime.datetime) -> list[list[str | None]]:
    table = d._trains_table(departures, now)
    return [
        [(getattr(td, "style", None) or {}).get("color") for td in tr.children]
        for tr in table.children[1].children
    ]


ORANGE, RED = "var(--bs-warning)", "var(--bs-danger)"


def test_trains_table_next_train_within_ten_minutes_is_orange():
    departures = [_make_departure("12:00"), _make_departure("12:05")]
    colours = _row_colours(departures, datetime.datetime(2026, 9, 15, 11, 52))
    assert colours[0] == [ORANGE] * 4
    assert colours[1] == [None] * 4  # only the next train is highlighted


def test_trains_table_next_train_more_than_ten_minutes_away_is_plain():
    colours = _row_colours([_make_departure("12:00")], datetime.datetime(2026, 9, 15, 11, 49))
    assert colours[0] == [None] * 4


def test_trains_table_uses_expected_time_for_delayed_next_train():
    delayed = {**_make_departure("12:00"), "ETD": "12:15"}
    not_yet = _row_colours([delayed], datetime.datetime(2026, 9, 15, 12, 3))
    soon = _row_colours([delayed], datetime.datetime(2026, 9, 15, 12, 6))
    assert not_yet[0] == [None, None, None, RED]
    assert soon[0] == [ORANGE, ORANGE, ORANGE, RED]


def test_trains_table_delays_and_cancellations_are_red():
    departures = [
        _make_departure("13:00"),
        {**_make_departure("13:30"), "ETD": "Cancelled"},
        {**_make_departure("14:00"), "ETD": "Delayed"},
    ]
    colours = _row_colours(departures, datetime.datetime(2026, 9, 15, 12, 0))
    assert [row[3] for row in colours] == [None, RED, RED]
    assert all(c is None for row in colours for c in row[:3])


def test_trains_table_handles_next_train_after_midnight():
    colours = _row_colours([_make_departure("00:04")], datetime.datetime(2026, 9, 15, 23, 58))
    assert colours[0] == [ORANGE] * 4


# ── update_weather callback ───────────────────────────────────────────────────


def test_update_weather_returns_table_with_valid_data():
    import datetime

    today = datetime.date.today().isoformat()
    forecast = {
        "features": [
            {
                "properties": {
                    "timeSeries": [
                        {
                            "time": f"{today}T10:00:00Z",
                            "feelsLikeTemperature": 14.0,
                            "probOfPrecipitation": 20,
                        },
                    ]
                }
            }
        ]
    }
    with patch.object(d, "get_weather", return_value=forecast):
        result = d.update_weather(1)
    assert isinstance(result, dbc.Table)
    assert {"Feels Like (°C)", "Precip (%)"} <= set(_texts(result))


def _texts(component) -> list[str]:
    children = getattr(component, "children", component)
    if isinstance(children, str):
        return [children]
    if isinstance(children, (list, tuple)):
        return [t for child in children for t in _texts(child)]
    return _texts(children) if children is not None and children is not component else []


def test_update_weather_returns_error_on_exception():
    with patch.object(d, "get_weather", side_effect=Exception("429")):
        result = d.update_weather(1)
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"


def test_update_weather_returns_warning_when_no_features():
    with patch.object(d, "get_weather", return_value={"features": []}):
        result = d.update_weather(1)
    assert isinstance(result, dbc.Alert)
    assert result.color == "warning"


# ── update_menu callback ──────────────────────────────────────────────────────


def test_update_menu_returns_table():
    df = pd.DataFrame({"Day": ["Mon"], "Dinner": ["Pasta"]})
    with patch.object(d, "get_weekly_menu", return_value=df):
        result = d.update_menu(1)
    assert isinstance(result, dbc.Table)


def test_update_menu_returns_error_on_exception():
    with patch.object(d, "get_weekly_menu", side_effect=Exception("network error")):
        result = d.update_menu(1)
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"


# ── update_ingredient_options callback ───────────────────────────────────────


def test_update_ingredient_options_returns_list_of_dicts():
    with patch.object(d, "get_ingredients", return_value=["Apple", "Banana"]):
        result = d.update_ingredient_options(1)
    assert result == [
        {"label": "Apple", "value": "Apple"},
        {"label": "Banana", "value": "Banana"},
    ]


def test_update_ingredient_options_returns_empty_on_exception():
    with patch.object(d, "get_ingredients", side_effect=Exception("sheet error")):
        result = d.update_ingredient_options(1)
    assert result == []


# ── update_recipes callback ───────────────────────────────────────────────────


def test_update_recipes_returns_empty_div_when_no_ingredient():
    result = d.update_recipes(None)
    assert isinstance(result, html.Div)


def test_update_recipes_returns_list_group_with_matches():
    with patch.object(d, "get_recipes_for_ingredient", return_value={"Pasta": "200g"}):
        result = d.update_recipes("flour")
    assert isinstance(result, dbc.ListGroup)


def test_update_recipes_returns_info_alert_when_no_matches():
    with patch.object(d, "get_recipes_for_ingredient", return_value={}):
        result = d.update_recipes("unobtainium")
    assert isinstance(result, dbc.Alert)
    assert result.color == "info"


def test_update_recipes_returns_error_on_exception():
    with patch.object(d, "get_recipes_for_ingredient", side_effect=Exception("fail")):
        result = d.update_recipes("flour")
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"


# ── update_schedule callback ──────────────────────────────────────────────────


def test_update_schedule_returns_div_with_table():
    df = pd.DataFrame({"Entry": ["Breakfast"], "Monday": ["Porridge"]})
    with patch.object(d, "get_schedule_summary", return_value=df):
        result = d.update_schedule(1)
    assert isinstance(result, html.Div)


def test_update_schedule_returns_error_on_exception():
    with patch.object(d, "get_schedule_summary", side_effect=Exception("csv error")):
        result = d.update_schedule(1)
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"
