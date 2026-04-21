"""
Integration tests — hit real external APIs.

Run with:  uv run pytest -m integration
Skip with: uv run pytest -m "not integration"  (default test run)
"""

from unittest.mock import MagicMock
import os
import sys

import pytest

_st = MagicMock()
_st.cache_data = lambda **_: (lambda f: f)
sys.modules.setdefault("streamlit", _st)
sys.modules.setdefault("streamlit_autorefresh", MagicMock())
sys.modules.setdefault("streamlit_calendar", MagicMock())

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import scripts.dashboard as d  # noqa: E402

pytestmark = pytest.mark.integration


# ── Google Sheets (public — no auth required) ─────────────────────────────────


def test_integration_weekly_menu_returns_dataframe():
    result = d.get_weekly_menu()
    assert not result.empty
    assert result.shape[1] > 0


def test_integration_menu_recipe_names_returns_strings():
    names = d.get_menu_recipe_names()
    assert isinstance(names, set)
    assert all(isinstance(n, str) and n for n in names)


def test_integration_ingredients_are_sorted_strings():
    ingredients = d.get_ingredients()
    assert isinstance(ingredients, list)
    assert all(isinstance(i, str) and i for i in ingredients)
    assert ingredients == sorted(ingredients)


def test_integration_ingredients_match_menu_recipes():
    menu_recipes = d.get_menu_recipe_names()
    ingredients = d.get_ingredients()
    if menu_recipes:
        assert len(ingredients) > 0, "Expected ingredients when menu recipes exist"


def test_integration_schedule_summary_has_day_columns():
    result = d.get_schedule_summary()
    assert not result.empty
    day_cols = [c for c in result.columns if c in d.DAYS]
    assert len(day_cols) > 0


def test_integration_recipes_for_ingredient_returns_dict():
    ingredients = d.get_ingredients()
    if not ingredients:
        pytest.skip("No ingredients available this week")
    result = d.get_recipes_for_ingredient(ingredients[0])
    assert isinstance(result, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in result.items())


# ── Met Office API (requires MET_OFFICE_API_KEY) ──────────────────────────────


@pytest.fixture
def met_office_key():
    key = os.getenv("MET_OFFICE_API_KEY")
    if not key:
        pytest.skip("MET_OFFICE_API_KEY not set")
    return key


def test_integration_weather_returns_features(met_office_key):
    import requests

    try:
        result = d.get_weather(*d.HADLEY_WOOD)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            pytest.skip("Met Office rate limit hit")
        raise
    assert "features" in result
    assert len(result["features"]) > 0


def test_integration_weather_has_time_series(met_office_key):
    import requests

    try:
        result = d.get_weather(*d.HADLEY_WOOD)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            pytest.skip("Met Office rate limit hit")
        raise
    time_series = result["features"][0]["properties"]["timeSeries"]
    assert len(time_series) > 0
    assert "feelsLikeTemperature" in time_series[0]
    assert "probOfPrecipitation" in time_series[0]


# ── National Rail API (requires DARWIN_LITE_TOKEN) ───────────────────────────


@pytest.fixture
def rail_token():
    token = os.getenv("DARWIN_LITE_TOKEN")
    if not token:
        pytest.skip("DARWIN_LITE_TOKEN not set")
    return token


def test_integration_departures_returns_location_name(rail_token):
    location, generated, services = d.get_departures("HDW", rail_token)
    assert isinstance(location, str) and location
    assert services is not None


def test_integration_departures_services_have_expected_fields(rail_token):
    _, _, services = d.get_departures("HDW", rail_token)
    if not services:
        pytest.skip("No services available right now")
    train = services[0]
    assert hasattr(train, "std")
    assert hasattr(train, "etd")
    assert hasattr(train, "destination")
