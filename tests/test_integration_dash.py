"""
Integration tests for scripts/dash_dashboard.py — hit real external APIs.

Run with:  uv run pytest -m integration
Skip with: uv run pytest -m "not integration"  (default test run)
"""

import os
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import scripts.dash_dashboard as d  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clear_caches():
    d._weather_cache.clear()
    d._menu_cache.clear()
    d._calendar_cache.clear()
    yield


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


def test_integration_weather_result_is_cached(met_office_key):
    import requests
    from unittest.mock import patch

    try:
        first = d.get_weather(*d.HADLEY_WOOD)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            pytest.skip("Met Office rate limit hit")
        raise
    with patch("requests.get") as mock_get:
        second = d.get_weather(*d.HADLEY_WOOD)
    mock_get.assert_not_called()
    assert first is second


# ── Darwin Push Port (requires DARWIN_SFTP_PASS / DARWIN_STOMP_USER / DARWIN_STOMP_PASS) ──


def _require_env(*keys):
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        pytest.skip(f"{', '.join(missing)} not set")


def test_integration_darwin_sftp_state_has_hadley_wood_calls():
    _require_env("DARWIN_SFTP_PASS")
    services = d._load_darwin_state()
    assert any("HADLYWD" in svc["calls"] for svc in services.values())


def test_integration_darwin_stream_logs_in_and_receives_messages():
    import gzip
    import queue

    _require_env("DARWIN_STOMP_USER", "DARWIN_STOMP_PASS")
    inbox = queue.Queue()
    conn = d._connect_stomp(d._DarwinListener(inbox))
    try:
        body = inbox.get(timeout=60)
    finally:
        conn.disconnect()
    assert gzip.decompress(body).lstrip().startswith(b"<?xml")
