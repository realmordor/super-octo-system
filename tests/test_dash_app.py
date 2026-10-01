"""
Unit tests for scripts/dash_app.py (the phone-style dashboard).

Run with:  uv run pytest tests/test_dash_app.py
"""

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import dash_app as a  # noqa: E402

d = a.d
NOW = datetime.datetime(2026, 9, 15, 11, 50)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    d._services.clear()
    d._station_messages.clear()
    monkeypatch.setattr(a, "COOKED_FILE", tmp_path / ".cooked.json")
    monkeypatch.setattr(a, "SHOPPING_FILE", tmp_path / ".shopping.json")
    yield
    d._services.clear()
    d._station_messages.clear()


def _pport(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Pport xmlns="http://www.thalesgroup.com/rtti/PushPort/v16"'
        ' xmlns:ns2="http://www.thalesgroup.com/rtti/PushPort/Schedules/v3"'
        ' xmlns:ns5="http://www.thalesgroup.com/rtti/PushPort/Forecasts/v3"'
        f' ts="2026-09-15T12:00:00+01:00" version="16.0"><uR>{body}</uR></Pport>'
    ).encode()


def _add_train(rid: str, hdw: str = "12:00", fpk: str = "12:15", mrgt: str = "12:30", cancelled: bool = False) -> None:
    """A train from Welwyn Garden City calling at Hadley Wood and Finsbury Park, ending at Moorgate."""
    can = ' can="true"' if cancelled else ""
    d._apply_pushport(
        _pport(
            f'<schedule rid="{rid}" uid="A1" ssd="2026-09-15" toc="GN">'
            '<ns2:OR wtd="11:40" ptd="11:40" tpl="WLWYNGC" act="TB"/>'
            f'<ns2:IP wta="{hdw}" wtd="{hdw}:30" tpl="HADLYWD" pta="{hdw}" ptd="{hdw}"{can}/>'
            f'<ns2:IP wta="{fpk}" wtd="{fpk}:30" tpl="FNPK" pta="{fpk}" ptd="{fpk}"/>'
            f'<ns2:DT wta="{mrgt}" tpl="MRGT" pta="{mrgt}"/>'
            "</schedule>"
        ),
        d._services,
    )


# ── Journeys ──────────────────────────────────────────────────────────────────


def test_journeys_include_trains_that_call_at_the_destination_on_the_way():
    _add_train("A")
    [journey] = a.get_journeys("HDW", "FPK", NOW)
    assert journey["time"] == "12:00"
    assert journey["arrives"] == datetime.datetime(2026, 9, 15, 12, 15)
    assert journey["minutes"] == 15
    assert journey["destination"] == "Moorgate"


def test_journeys_use_the_expected_arrival_time():
    _add_train("A")
    d._apply_pushport(
        _pport(
            '<TS rid="A" uid="A1" ssd="2026-09-15">'
            '<ns5:Location tpl="MRGT" wta="12:30" pta="12:30"><ns5:arr et="12:36"/></ns5:Location></TS>'
        ),
        d._services,
    )
    [journey] = a.get_journeys("HDW", "MOG", NOW)
    assert journey["arrives"] == datetime.datetime(2026, 9, 15, 12, 36)
    assert journey["minutes"] == 36


def test_journeys_leave_out_trains_going_the_other_way():
    _add_train("A")
    assert a.get_journeys("FPK", "HDW", NOW) == []


def test_journeys_are_soonest_first():
    _add_train("LATE", hdw="12:30", fpk="12:45", mrgt="13:00")
    _add_train("SOON")
    assert [j["time"] for j in a.get_journeys("HDW", "MOG", NOW)] == ["12:00", "12:30"]


# ── Leave by ──────────────────────────────────────────────────────────────────


def test_leave_by_skips_trains_too_soon_to_walk_for_and_cancelled_ones():
    _add_train("TOO_SOON", hdw="11:55", fpk="12:10", mrgt="12:25")
    _add_train("CANCELLED", hdw="12:05", fpk="12:20", mrgt="12:35", cancelled=True)
    _add_train("CATCHABLE", hdw="12:10", fpk="12:25", mrgt="12:40")
    leave = a._leave_by(a.get_journeys("HDW", "MOG", NOW), NOW)
    text = str(leave)
    assert "Leave in 10 min" in text
    assert "for the 12:10" in text


def test_leave_by_is_empty_when_no_train_can_be_caught():
    _add_train("TOO_SOON", hdw="11:55", fpk="12:10", mrgt="12:25")
    assert a._leave_by(a.get_journeys("HDW", "MOG", NOW), NOW) is None


# ── Shopping list ─────────────────────────────────────────────────────────────


def test_shopping_list_adds_up_the_same_ingredient_across_recipes(monkeypatch):
    monkeypatch.setattr(
        a,
        "get_recipe_ingredients",
        lambda: {
            "Quiche": [
                {"name": "Butter", "amount": "100 g", "qty": 100.0, "unit": "g"},
                {"name": "Water", "amount": "9 tbsp", "qty": 9.0, "unit": "tbsp"},
            ],
            "Gratin": [
                {"name": "Butter", "amount": "40 g", "qty": 40.0, "unit": "g"},
                {"name": "Butter", "amount": "1 tbsp", "qty": 1.0, "unit": "tbsp"},
            ],
            "Not this week": [{"name": "Butter", "amount": "500 g", "qty": 500.0, "unit": "g"}],
        },
    )
    items = a.get_shopping_list(["Quiche", "Gratin"])
    assert [(i["name"], i["amount"], i["recipes"]) for i in items] == [
        ("Butter", "140 g", 2),
        ("Butter", "1 tbsp", 1),
    ]


def test_shopping_items_can_be_added_ticked_and_removed():
    a.add_shopping_item("  Milk ")
    a.add_shopping_item("Bread")
    milk, bread = a.get_shopping()
    assert (milk["name"], milk["done"]) == ("Milk", False)
    a.tick_shopping_item(milk["id"], True)
    assert [i["done"] for i in a.get_shopping()] == [True, False]
    a.remove_shopping_items({bread["id"]})
    assert [i["name"] for i in a.get_shopping()] == ["Milk"]


def test_adding_an_item_already_on_the_list_unticks_it_instead_of_repeating_it():
    a.add_shopping_item("Milk")
    a.tick_shopping_item(a.get_shopping()[0]["id"], True)
    a.add_shopping_item("milk")
    assert [(i["name"], i["done"]) for i in a.get_shopping()] == [("Milk", False)]


def test_blank_items_are_not_added():
    a.add_shopping_item("   ")
    assert a.get_shopping() == []


def test_clear_ticked_removes_only_ticked_items():
    for name in ("Milk", "Bread", "Eggs"):
        a.add_shopping_item(name)
    milk, _, eggs = a.get_shopping()
    a.tick_shopping_item(milk["id"], True)
    a.tick_shopping_item(eggs["id"], True)
    a.remove_shopping_items()
    assert [i["name"] for i in a.get_shopping()] == ["Bread"]


def test_shopping_list_survives_a_new_menu():
    a.add_shopping_item("Milk")
    a.set_tick(["Quiche"], "cooked", "Quiche", True)
    a.get_cooked(["Curry"])  # new menu: cooked ticks go
    assert [i["name"] for i in a.get_shopping()] == ["Milk"]


@pytest.mark.parametrize(
    ("amount", "unit", "expected"),
    [(2.0, "tin", "2 tin"), (1.5, "medium", "1½ medium"), (0.25, "tsp", "¼ tsp"), (0, "g", ""), (1.2, "kg", "1.2 kg")],
)
def test_format_amount(amount, unit, expected):
    assert a._format_amount(amount, unit) == expected


# ── Ticks (cooked recipes) ────────────────────────────────────────────────────


def test_ticks_are_kept_per_kind():
    menu = ["Quiche", "Gratin"]
    a.set_tick(menu, "cooked", "Quiche", True)
    a.set_tick(menu, "other", "Gratin", True)
    assert a.get_cooked(menu) == {"Quiche"}
    assert a.get_ticks(menu, "other") == {"Gratin"}


def test_unticking_keeps_other_ticks():
    menu = ["Quiche", "Gratin"]
    a.set_tick(menu, "cooked", "Quiche", True)
    a.set_tick(menu, "cooked", "Gratin", True)
    a.set_tick(menu, "cooked", "Quiche", False)
    assert a.get_cooked(menu) == {"Gratin"}


def test_a_new_menu_deletes_all_ticks_even_if_old_recipes_return():
    week1, week2 = ["Quiche", "Gratin"], ["Quiche", "Curry"]
    a.set_tick(week1, "cooked", "Quiche", True)
    assert a.get_cooked(week2) == set()
    assert not a.COOKED_FILE.exists()
    assert a.get_cooked(week1) == set()


def test_ticks_file_keeps_the_existing_format():
    a.set_tick(["Quiche"], "cooked", "Quiche", True)
    assert json.loads(a.COOKED_FILE.read_text()) == {"menu": ["Quiche"], "cooked": ["Quiche"]}


# ── Timeline ──────────────────────────────────────────────────────────────────


def test_timeline_leaves_out_all_day_events_that_ended_at_midnight():
    events = [
        {"start": "2026-09-14", "end": "2026-09-15", "summary": "Yesterday's bins"},
        {"start": "2026-09-15", "end": "2026-09-16", "summary": "Today's bins"},
    ]
    text = str(a._timeline(events, NOW.date(), NOW))
    assert "Today's bins" in text
    assert "Yesterday's bins" not in text


def test_timeline_fades_finished_events_today_only():
    events = [{"start": "2026-09-15T08:00:00", "end": "2026-09-15T09:00:00", "summary": "School run"}]
    assert "tl-row past" in str(a._timeline(events, NOW.date(), NOW))
    tomorrow = [{"start": "2026-09-16T08:00:00", "end": "2026-09-16T09:00:00", "summary": "School run"}]
    assert "tl-row past" not in str(a._timeline(tomorrow, NOW.date() + datetime.timedelta(days=1), NOW))


def test_timeline_says_when_nothing_is_planned():
    assert "Nothing planned" in str(a._timeline([], NOW.date(), NOW))
