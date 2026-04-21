from unittest.mock import MagicMock, patch
import sys
import urllib.parse

import pandas as pd
import pytest

# Mock streamlit before importing dashboard so cache_data decorators don't
# require a running Streamlit server.
_st = MagicMock()
_st.cache_data = lambda **_: (lambda f: f)
sys.modules.setdefault("streamlit", _st)
sys.modules.setdefault("streamlit_autorefresh", MagicMock())
sys.modules.setdefault("streamlit_calendar", MagicMock())

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import scripts.dashboard as d  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


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


def test_get_schedule_summary_strips_brackets_from_entry():
    df = pd.DataFrame({"[Breakfast] Monday": ["Porridge"], "Timestamp": ["2024-01-01"]})
    with patch("pandas.read_csv", return_value=df):
        result = d.get_schedule_summary()
    if "Entry" in result.columns:
        assert not result["Entry"].str.contains(r"[\[\]\(\)]", regex=True).any()


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


# ── get_ingredients ───────────────────────────────────────────────────────────


def test_get_ingredients_filters_to_menu_recipes(ingredients_df):
    with patch.object(d, "get_menu_recipe_names", return_value={"Recipe A"}):
        with patch("pandas.read_csv", return_value=ingredients_df):
            result = d.get_ingredients()
    assert "Onion" in result
    assert "Garlic" in result
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


def test_get_ingredients_returns_empty_for_empty_sheet():
    with patch.object(d, "get_menu_recipe_names", return_value={"Recipe A"}):
        with patch(
            "pandas.read_csv",
            return_value=pd.DataFrame(columns=["r", "k", "d", "a", "u"]),
        ):
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
        result = d.get_recipes_for_ingredient("Onion")
    assert result["Recipe A"] == "2 medium"


def test_get_recipes_for_ingredient_amount_only():
    df = pd.DataFrame(
        {
            "recipe": ["Recipe A"],
            "key": ["salt"],
            "display": ["Salt"],
            "amount": [1],
            "unit": [None],
        }
    )
    with patch("pandas.read_csv", return_value=df):
        result = d.get_recipes_for_ingredient("Salt")
    assert result["Recipe A"] == "1"


def test_get_recipes_for_ingredient_no_amount_or_unit():
    df = pd.DataFrame(
        {
            "recipe": ["Recipe A"],
            "key": ["salt"],
            "display": ["Salt"],
            "amount": [None],
            "unit": [None],
        }
    )
    with patch("pandas.read_csv", return_value=df):
        result = d.get_recipes_for_ingredient("Salt")
    assert result["Recipe A"] == "Amount not specified"


def test_get_recipes_for_ingredient_deduplicates_recipes(ingredients_df):
    with patch("pandas.read_csv", return_value=ingredients_df):
        result = d.get_recipes_for_ingredient("Onion")
    assert list(result.keys()).count("Recipe A") == 1


# ── get_weather ───────────────────────────────────────────────────────────────


def test_get_weather_raises_when_no_api_key():
    with patch.dict("os.environ", {}, clear=True):
        with patch("os.getenv", return_value=""):
            with pytest.raises(ValueError, match="MET_OFFICE_API_KEY"):
                d.get_weather(51.0, -0.1)
