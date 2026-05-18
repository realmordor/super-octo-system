"""
Unit tests for scripts/dash_dashboard.py.

Run with:  uv run pytest tests/test_dash_dashboard.py
"""

from unittest.mock import MagicMock, patch
import sys
import urllib.parse

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
    d._departures_cache.clear()
    d._calendar_cache.clear()
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


@pytest.fixture
def mock_services():
    def make_service(std, etd, dest_name, dest_crs, platform=None):
        svc = MagicMock()
        svc.std = std
        svc.etd = etd
        svc.platform = platform
        svc.destination.location = [MagicMock(locationName=dest_name, crs=dest_crs)]
        return svc

    return [
        make_service("08:00", "On time", "Moorgate", "MOG", "1"),
        make_service("08:15", "Delayed", "Moorgate", "MOG", "2"),
        make_service("08:30", "On time", "Finsbury Park", "FPK", "1"),
    ]


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
    assert list(result["A"]) == ["data1", "data2"]


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


# ── update_trains callback ────────────────────────────────────────────────────


def test_update_trains_returns_error_when_no_token(mock_services):
    with patch.dict("os.environ", {}, clear=True):
        with patch("scripts.dash_dashboard.ctx") as mock_ctx:
            mock_ctx.triggered_id = None
            result = d.update_trains(1, 0, "HDW", "MOG")
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"


def test_update_trains_returns_table_with_matching_services(mock_services):
    import datetime

    generated = datetime.datetime(2026, 5, 16, 8, 0, tzinfo=datetime.timezone.utc)
    with patch.dict("os.environ", {"DARWIN_LITE_TOKEN": "fake"}):
        with patch.object(
            d, "get_departures", return_value=("Hadley Wood", generated, mock_services)
        ):
            with patch("scripts.dash_dashboard.ctx") as mock_ctx:
                mock_ctx.triggered_id = None
                result = d.update_trains(1, 0, "HDW", "MOG")
    assert isinstance(result, html.Div)


def test_update_trains_warns_when_no_matching_destination(mock_services):
    import datetime

    generated = datetime.datetime(2026, 5, 16, 8, 0, tzinfo=datetime.timezone.utc)
    with patch.dict("os.environ", {"DARWIN_LITE_TOKEN": "fake"}):
        with patch.object(
            d, "get_departures", return_value=("Hadley Wood", generated, mock_services)
        ):
            with patch("scripts.dash_dashboard.ctx") as mock_ctx:
                mock_ctx.triggered_id = None
                result = d.update_trains(1, 0, "HDW", "AAP")
    assert isinstance(result, dbc.Alert)
    assert result.color == "warning"


def test_update_trains_returns_error_on_api_failure():
    with patch.dict("os.environ", {"DARWIN_LITE_TOKEN": "fake"}):
        with patch.object(d, "get_departures", side_effect=Exception("SOAP error")):
            with patch("scripts.dash_dashboard.ctx") as mock_ctx:
                mock_ctx.triggered_id = None
                result = d.update_trains(1, 0, "HDW", "MOG")
    assert isinstance(result, dbc.Alert)
    assert result.color == "danger"


def test_update_trains_clears_cache_on_refresh(mock_services):
    import datetime

    generated = datetime.datetime(2026, 5, 16, 8, 0, tzinfo=datetime.timezone.utc)
    token = "fake"
    d._departures_cache[("HDW", token)] = ("Hadley Wood", generated, mock_services)
    with patch.dict("os.environ", {"DARWIN_LITE_TOKEN": token}):
        with patch.object(
            d, "get_departures", return_value=("Hadley Wood", generated, mock_services)
        ):
            with patch("scripts.dash_dashboard.ctx") as mock_ctx:
                mock_ctx.triggered_id = "btn-refresh-trains"
                d.update_trains(1, 1, "HDW", "MOG")
    assert ("HDW", token) not in d._departures_cache


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
