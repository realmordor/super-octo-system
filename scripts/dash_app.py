"""Phone-style version of the dashboard for a portrait touch screen.

Runs alongside scripts/dash_dashboard.py (port 8051 instead of 8050) and reuses its
data fetching. Each section is a full-screen tab picked from a bottom bar, or by
swiping left and right; the app returns to the Today tab after a few idle minutes.
"""

import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import dash
import dash_mantine_components as dmc
import pandas as pd
import requests
from cachetools import TTLCache
from dash import ALL, Input, Output, State, clientside_callback, dcc, html
from dash import _callback as dash_callback
from dash_iconify import DashIconify
from dateutil import parser as dt_parser
from googleapiclient.discovery import build

import dash_dashboard as d

# dash_dashboard registers its clientside callbacks globally, so drop them before
# they get attached to this app (they target components that don't exist here).
dash_callback.GLOBAL_CALLBACK_LIST.clear()
dash_callback.GLOBAL_CALLBACK_MAP.clear()
dash_callback.GLOBAL_INLINE_SCRIPTS.clear()

PORT = 8051
HOME_STATION = "HDW"
WALK_MINUTES = int(os.getenv("WALK_TO_STATION_MINUTES") or 10)
# Dim the screen at night (app.js). NIGHT_DIM=off starts with it off; the moon button
# in the header switches it per screen
NIGHT_DIM = (os.getenv("NIGHT_DIM") or "on").strip().lower() not in ("off", "false", "no", "0")
# Darwin only gives station codes; names for where trains through Hadley Wood often end
# up, beyond the stations dash_dashboard tracks
DESTINATION_NAMES = {
    **d.TIPLOC_TO_NAME,
    "KNGX": "King's Cross",
    "STEVNGE": "Stevenage",
    "HRTFNTH": "Hertford North",
    "HITCHIN": "Hitchin",
    "CAMBDGE": "Cambridge",
    "PBRO": "Peterborough",
}

TABS = [
    ("today", "Today", "tabler:home"),
    ("trains", "Trains", "tabler:train"),
    ("calendar", "Calendar", "tabler:calendar"),
    ("food", "Food", "tabler:tools-kitchen-2"),
    ("schedule", "Schedule", "tabler:users"),
]

STATION_OPTIONS = [{"value": k, "label": v} for k, v in d.CRS_CODES.items()]

# Met Office significantWeatherCode -> (description, day icon, night icon)
WEATHER_CODES = {
    -1: ("Trace rain", "drizzle", "drizzle"),
    0: ("Clear", "clear-day", "clear-night"),
    1: ("Sunny", "clear-day", "clear-night"),
    2: ("Partly cloudy", "partly-cloudy-day", "partly-cloudy-night"),
    3: ("Partly cloudy", "partly-cloudy-day", "partly-cloudy-night"),
    5: ("Mist", "mist", "mist"),
    6: ("Fog", "fog", "fog"),
    7: ("Cloudy", "cloudy", "cloudy"),
    8: ("Overcast", "overcast", "overcast"),
    9: ("Light showers", "partly-cloudy-day-rain", "partly-cloudy-night-rain"),
    10: ("Light showers", "partly-cloudy-day-rain", "partly-cloudy-night-rain"),
    11: ("Drizzle", "drizzle", "drizzle"),
    12: ("Light rain", "rain", "rain"),
    13: ("Heavy showers", "partly-cloudy-day-rain", "partly-cloudy-night-rain"),
    14: ("Heavy showers", "partly-cloudy-day-rain", "partly-cloudy-night-rain"),
    15: ("Heavy rain", "rain", "rain"),
    16: ("Sleet showers", "partly-cloudy-day-sleet", "partly-cloudy-night-sleet"),
    17: ("Sleet showers", "partly-cloudy-day-sleet", "partly-cloudy-night-sleet"),
    18: ("Sleet", "sleet", "sleet"),
    19: ("Hail showers", "partly-cloudy-day-hail", "partly-cloudy-night-hail"),
    20: ("Hail showers", "partly-cloudy-day-hail", "partly-cloudy-night-hail"),
    21: ("Hail", "hail", "hail"),
    22: ("Light snow showers", "partly-cloudy-day-snow", "partly-cloudy-night-snow"),
    23: ("Light snow showers", "partly-cloudy-day-snow", "partly-cloudy-night-snow"),
    24: ("Light snow", "snow", "snow"),
    25: ("Heavy snow showers", "partly-cloudy-day-snow", "partly-cloudy-night-snow"),
    26: ("Heavy snow showers", "partly-cloudy-day-snow", "partly-cloudy-night-snow"),
    27: ("Heavy snow", "snow", "snow"),
    28: ("Thunder showers", "thunderstorms-day", "thunderstorms-night"),
    29: ("Thunder showers", "thunderstorms-day", "thunderstorms-night"),
    30: ("Thunder", "thunderstorms", "thunderstorms"),
}

_schedule_cache: TTLCache = TTLCache(maxsize=1, ttl=300)
_tube_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

TFL_STATUS_URL = "https://api.tfl.gov.uk/Line/Mode/tube,elizabeth-line/Status"
# TfL's line colours (Northern lightened so it shows on the dark background)
TFL_LINE_COLOURS = {
    "bakerloo": "#B36305", "central": "#E32017", "circle": "#FFD300", "district": "#00782A",
    "elizabeth": "#6950A1", "hammersmith-city": "#F3A9BB", "jubilee": "#A0A5A9",
    "metropolitan": "#9B0056", "northern": "#5c5c5c", "piccadilly": "#003688",
    "victoria": "#0098D4", "waterloo-city": "#95CDBA",
}

_ingredients_cache: TTLCache = TTLCache(maxsize=1, ttl=300)
_day_events_cache: TTLCache = TTLCache(maxsize=4, ttl=300)

TIMELINE_COLOURS = ["#1c7ed6", "#0c8599", "#9c36b5", "#e8590c", "#4263eb", "#c2255c", "#2f9e44"]

# Bin colours for "Black bin collection" etc. events, taken from the photos on Enfield
# Council's household waste page (brown is the food waste bin)
BIN_COLOURS = {"black": "#403f3b", "brown": "#786157", "green": "#3d902e", "blue": "#275faf"}
_BIN_EVENT = re.compile(r"\b(black|brown|green|blue) bin\b", re.IGNORECASE)

FRACTIONS = {0.25: "¼", 0.33: "⅓", 0.5: "½", 0.67: "⅔", 0.75: "¾"}

SHOPPING_FILE = Path(__file__).resolve().parent.parent / ".shopping.json"
_shopping_lock = threading.Lock()

COOKED_FILE = Path(__file__).resolve().parent.parent / ".cooked.json"
_cooked_lock = threading.Lock()

# ── Data ──────────────────────────────────────────────────────────────────────


def get_tube_status() -> list[dict]:
    """Each Tube and Elizabeth line's status, worst first: {id, name, severity
    (10 is good service), status, reason}."""
    if "status" in _tube_cache:
        return _tube_cache["status"]
    resp = requests.get(TFL_STATUS_URL, timeout=15)
    resp.raise_for_status()
    lines = []
    for line in resp.json():
        statuses = line.get("lineStatuses", [])
        descriptions = list(dict.fromkeys(st["statusSeverityDescription"] for st in statuses))
        reason = next((st["reason"] for st in statuses if st.get("reason")), "")
        lines.append(
            {
                "id": line["id"],
                "name": line["name"],
                "severity": min((st["statusSeverity"] for st in statuses), default=10),
                "status": ", ".join(descriptions) or "Unknown",
                "reason": re.sub(rf"^{re.escape(line['name'])}( Line)?:\s*", "", reason.strip(), flags=re.I),
            }
        )
    lines.sort(key=lambda l: (l["severity"] >= 10, l["severity"], l["name"]))
    _tube_cache["status"] = lines
    return lines


def get_schedule() -> tuple[str | None, dict[str, dict[str, str]]]:
    """The latest schedule form response as (week start, {entry: {day: names}})."""
    if "schedule" in _schedule_cache:
        return _schedule_cache["schedule"]
    df = pd.read_csv(d._sheet_url(d.SCHEDULE_SHEET_ID, name="Form Responses 1"))
    week_start, entries = None, {}
    if not df.empty:
        latest = df.iloc[-1]
        for col in df.columns:
            value = latest[col]
            if col.lower().startswith("start week"):
                week_start = str(value) if pd.notna(value) else None
            elif m := re.fullmatch(r"(.+?)\s*\[(\w+)\]", col):
                day_values = entries.setdefault(m[1], {})
                if pd.notna(value) and str(value).strip():
                    day_values[m[2]] = str(value).strip()
    _schedule_cache["schedule"] = (week_start, entries)
    return week_start, entries


def get_day_events(day: datetime.date) -> list[dict]:
    """All of a day's events, including ones that have finished (get_upcoming_events
    only returns events that haven't ended yet)."""
    if day in _day_events_cache:
        return _day_events_cache[day]
    service = build("calendar", "v3", credentials=d._google_creds())
    calendars = service.calendarList().list().execute().get("items", [])
    calendar_id = next((c["id"] for c in calendars if c.get("summary") == "29ClaremontRoad"), None)
    if not calendar_id:
        raise ValueError("Calendar '29ClaremontRoad' not found.")
    midnight = d._LONDON.localize(datetime.datetime.combine(day, datetime.time.min))
    items = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=midnight.isoformat(),
            timeMax=(midnight + datetime.timedelta(days=1)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    events = [
        {
            "start": d._to_london(e["start"].get("dateTime", e["start"].get("date"))),
            "end": d._to_london(e["end"].get("dateTime", e["end"].get("date"))),
            "summary": e.get("summary", "No Title"),
        }
        for e in items
    ]
    _day_events_cache[day] = events
    return events


def bin_colour(title: str) -> str | None:
    """The bin's colour for a bin collection event, or None for any other event."""
    m = _BIN_EVENT.search(title)
    return BIN_COLOURS[m[1].lower()] if m else None


def _format_amount(amount, unit) -> str:
    """2.0, "tin" -> "2 tin"; 1.5, "medium" -> "1½ medium"; 0 or blank -> "" (no amount given)."""
    try:
        n = float(amount)
    except (TypeError, ValueError):
        n = 0.0
    number = ""
    if n > 0:
        whole, frac = int(n), round(n - int(n), 2)
        if frac == 0:
            number = str(whole)
        elif frac in FRACTIONS:
            number = f"{whole or ''}{FRACTIONS[frac]}"
        else:
            number = f"{n:g}"
    if not number:
        return ""
    unit = str(unit).strip() if pd.notna(unit) else ""
    return f"{number} {unit}".strip()


def get_recipe_ingredients() -> dict[str, list[dict]]:
    """{recipe: [{name, amount (for display), qty, unit}]} from the ingredients sheet.
    The recipe name is only filled in on each recipe's first row."""
    if "ingredients" in _ingredients_cache:
        return _ingredients_cache["ingredients"]
    df = pd.read_csv(d._sheet_url(d.RECIPE_SHEET_ID, gid=d.RECIPE_INGREDIENTS_GID))
    recipes: dict[str, list[dict]] = {}
    current = None
    for row in df.itertuples(index=False):
        name, _, display, amount, unit = row[:5]
        if pd.notna(name) and str(name).strip():
            current = str(name).strip()
        if current and pd.notna(display) and str(display).strip():
            qty = pd.to_numeric(amount, errors="coerce")
            recipes.setdefault(current, []).append(
                {
                    "name": str(display).strip(),
                    "amount": _format_amount(amount, unit),
                    "qty": float(qty) if pd.notna(qty) and qty > 0 else None,
                    "unit": str(unit).strip() if pd.notna(unit) else "",
                }
            )
    _ingredients_cache["ingredients"] = recipes
    return recipes


def get_shopping_list(menu: list[str]) -> list[dict]:
    """Ingredients for all of `menu`, with amounts of the same thing in the same unit
    added up: [{key, name, amount, recipes}], by name."""
    totals: dict[tuple[str, str], dict] = {}
    for recipe, items in get_recipe_ingredients().items():
        if recipe not in menu:
            continue
        for item in items:
            if item["name"].lower() == "water":
                continue
            entry = totals.setdefault((item["name"], item["unit"]), {"qty": 0.0, "recipes": set()})
            entry["qty"] += item["qty"] or 0
            entry["recipes"].add(recipe)
    return [
        {"key": f"{name}|{unit}", "name": name, "amount": _format_amount(t["qty"], unit), "recipes": len(t["recipes"])}
        for (name, unit), t in sorted(totals.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1]))
    ]


def get_shopping() -> list[dict]:
    """The shopping list: [{id, name, done}], in the order things were added."""
    with _shopping_lock:
        try:
            return json.loads(SHOPPING_FILE.read_text()).get("items", [])
        except (FileNotFoundError, ValueError):
            return []


def update_shopping(change) -> None:
    """Apply `change` (a function that edits the list of items in place) and save."""
    with _shopping_lock:
        try:
            items = json.loads(SHOPPING_FILE.read_text()).get("items", [])
        except (FileNotFoundError, ValueError):
            items = []
        change(items)
        tmp = SHOPPING_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items}, indent=2))
        tmp.replace(SHOPPING_FILE)


def add_shopping_item(name: str) -> None:
    """Add `name`, unless it's already on the list (then it's just unticked)."""
    name = " ".join(name.split())
    if not name:
        return

    def change(items):
        for item in items:
            if item["name"].lower() == name.lower():
                item["done"] = False
                return
        # A short text id: big numbers lose precision in the browser and stop matching
        items.append({"id": uuid.uuid4().hex[:12], "name": name, "done": False})

    update_shopping(change)


def tick_shopping_item(item_id: str, done: bool) -> None:
    def change(items):
        for item in items:
            if item["id"] == item_id:
                item["done"] = done

    update_shopping(change)


def remove_shopping_items(item_ids: set[str] | None = None) -> None:
    """Remove the given items, or every ticked item when `item_ids` is None."""

    def change(items):
        items[:] = [i for i in items if not (i["done"] if item_ids is None else i["id"] in item_ids)]

    update_shopping(change)


def _read_ticks(menu: list[str]) -> dict[str, set[str]]:
    try:
        saved = json.loads(COOKED_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    if sorted(saved.get("menu", [])) != sorted(menu):
        COOKED_FILE.unlink(missing_ok=True)
        return {}
    return {kind: set(items) for kind, items in saved.items() if kind != "menu"}


def get_ticks(menu: list[str], kind: str) -> set[str]:
    """Things ticked off this week, such as "cooked" recipes. Ticks belong
    to one weekly menu: as soon as the menu changes at all the saved ticks are deleted,
    so they never come back even if the same recipes return later."""
    with _cooked_lock:
        return _read_ticks(menu).get(kind, set())


def set_tick(menu: list[str], kind: str, item: str, done: bool) -> None:
    """Tick or untick one item, keeping whatever else is already ticked."""
    with _cooked_lock:
        ticks = _read_ticks(menu)
        current = ticks.get(kind, set())
        ticks[kind] = current | {item} if done else current - {item}
        data = json.dumps({"menu": sorted(menu), **{k: sorted(v) for k, v in sorted(ticks.items())}}, indent=2)
        tmp = COOKED_FILE.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(COOKED_FILE)


def get_cooked(menu: list[str]) -> set[str]:
    return get_ticks(menu, "cooked") & set(menu)


def _week_start_date(week_start: str | None) -> datetime.date | None:
    if not week_start:
        return None
    try:
        return dt_parser.parse(week_start, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


# ── Components ────────────────────────────────────────────────────────────────


def _card(title, *children, **kwargs):
    header = [dmc.Text(title, fw=600, c="dimmed", size="sm", tt="uppercase", mb="sm")] if title else []
    return dmc.Card(header + list(children), withBorder=True, radius="lg", padding="lg", mb="md", **kwargs)


def _alert(msg: str, color: str = "red") -> dmc.Alert:
    return dmc.Alert(msg, color=color, variant="light", radius="md")


def _weather_icon(code, is_day: bool, size: int) -> DashIconify:
    _, day_icon, night_icon = WEATHER_CODES.get(code, ("", "cloudy", "cloudy"))
    return DashIconify(icon=f"meteocons:{day_icon if is_day else night_icon}-fill", width=size)


def get_journeys(depart_crs: str, dest_crs: str, now: datetime.datetime) -> list[dict]:
    """Trains from `depart_crs` that later call at `dest_crs` (not only ones that end
    there), soonest first, with the expected arrival time and journey length."""
    depart_tpl = d.TIPLOC_FROM_CRS.get(depart_crs, depart_crs)
    dest_tpl = d.TIPLOC_FROM_CRS.get(dest_crs, dest_crs)
    journeys = []
    with d._darwin_lock:
        for svc in d._services.values():
            call = svc["calls"].get(depart_tpl)
            arr = svc.get("arrs", {}).get(dest_tpl)
            if not call or not arr or arr["sta"] <= call["std"]:
                continue
            fc = svc["fc"].get(depart_tpl, {})
            if fc.get("at"):
                continue  # already departed
            std = call["std"]
            etd = d._nearest(std, fc["et"]) if fc.get("et") else std
            if max(std, etd) < now - datetime.timedelta(minutes=1):
                continue
            if call["can"]:
                status = "Cancelled"
            elif fc.get("delayed"):
                status = "Delayed"
            elif etd > std:
                status = etd.strftime("%H:%M")
            else:
                status = "On time"
            arr_fc = svc["fc"].get(dest_tpl, {})
            arr_time = arr_fc.get("arr_at") or arr_fc.get("arr_et")
            eta = max(d._nearest(arr["sta"], arr_time) if arr_time else arr["sta"], etd)
            journeys.append(
                {
                    "time": call["ptd"],
                    "departs": etd,
                    "status": status,
                    "platform": fc.get("plat") or "—",
                    "destination": DESTINATION_NAMES.get(svc["dest"], svc["dest"] or "—"),
                    "arrives": eta,
                    "minutes": round((eta - etd).total_seconds() / 60),
                }
            )
    return sorted(journeys, key=lambda j: j["departs"])


def get_station_messages(*crs: str) -> list[dict]:
    """Current Darwin station messages for any of `crs`, most severe first."""
    with d._darwin_lock:
        messages = [m for m in d._station_messages.values() if set(m["crs"]) & set(crs)]
    return sorted(messages, key=lambda m: -m["sev"])


def _train_row(train: dict, now: datetime.datetime) -> html.Div:
    """Same colours as the main dashboard: red when cancelled or leaving within 10
    minutes, green from 10 to 20 minutes away."""
    status = train["status"]
    minutes = round((train["departs"] - now).total_seconds() / 60)
    if status == "Cancelled" or minutes <= 10:
        urgency = "red"
    elif minutes <= 20:
        urgency = "green"
    else:
        urgency = "none"
    if status == "On time":
        badge = dmc.Badge("On time", color="teal", variant="light", size="lg")
    elif status in ("Cancelled", "Delayed"):
        badge = dmc.Badge(status, color="red", variant="filled", size="lg")
    else:
        badge = dmc.Badge(f"Exp {status}", color="orange", variant="light", size="lg")
    when = "" if status == "Cancelled" else "now" if minutes <= 0 else f"in {minutes} min"
    details = [f"Platform {train['platform']}"] + ([when] if when else [])
    return html.Div(
        className=f"train-row urgency-{urgency}",
        children=[
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(train["time"], className="train-time"),
                            html.Span(f"→ {train['arrives']:%H:%M}", className="train-arrives"),
                        ]
                    ),
                    dmc.Text(f"{train['minutes']} min journey · to {train['destination']}", c="dimmed", size="sm"),
                ]
            ),
            html.Div(
                className="train-right",
                children=[badge, dmc.Text("  ·  ".join(details), size="sm", c="dimmed")],
            ),
        ],
    )


def _leave_by(journeys: list[dict], now: datetime.datetime):
    """When to set off to walk to the station for the next train worth catching."""
    for train in journeys:
        if train["status"] == "Cancelled":
            continue
        leave = train["departs"] - datetime.timedelta(minutes=WALK_MINUTES)
        minutes = (leave - now).total_seconds() / 60
        if minutes < 0:
            continue
        headline = "Leave now" if minutes < 1 else f"Leave in {int(minutes)} min"
        return html.Div(
            className="leave-by",
            children=[
                DashIconify(icon="tabler:walk", width=34),
                html.Div(
                    [
                        html.Div(headline, className="leave-by-headline"),
                        dmc.Text(
                            f"by {leave:%H:%M} for the {train['time']}, arriving {train['arrives']:%H:%M}",
                            c="dimmed",
                        ),
                    ]
                ),
            ],
        )
    return None


def _station_alerts(depart: str, dest: str, compact: bool = False):
    messages = get_station_messages(depart, dest)
    if compact:
        messages = [m for m in messages if m["sev"] >= 1]
    return [
        dmc.Alert(
            m["text"],
            title=", ".join(d.CRS_CODES.get(c, c) for c in m["crs"]),
            color="red" if m["sev"] >= 2 else "yellow" if m["sev"] == 1 else "blue",
            variant="light",
            radius="md",
            mb="sm",
            icon=DashIconify(icon="tabler:alert-triangle" if m["sev"] else "tabler:info-circle", width=22),
        )
        for m in messages
    ]


def _trains_block(depart: str, dest: str, limit: int, today: bool = False):
    missing = [k for k in d._DARWIN_REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        return _alert(f"{', '.join(missing)} not set.")
    status = dict(d._darwin_status)
    if not status["ready"]:
        if status["error"]:
            return _alert(f"Rail feed error: {status['error']}")
        return dmc.Group([dmc.Loader(size="sm"), dmc.Text("Loading live train data…", c="dimmed")])
    now = d._now_london()
    journeys = get_journeys(depart, dest, now)
    children = _station_alerts(depart, dest, compact=today)
    last = status["last_message"]
    as_of = last.strftime("%H:%M:%S") if last else "—"
    if not status["connected"]:
        children.append(_alert(f"Live feed reconnecting. Showing data as of {as_of}.", "yellow"))
    elif not last or datetime.datetime.now(d._LONDON) - last > datetime.timedelta(minutes=2):
        children.append(_alert(f"No live updates since {as_of}.", "yellow"))
    if today and depart == HOME_STATION:
        children.append(_leave_by(journeys, now))
    if not journeys:
        children.append(dmc.Text("No direct trains right now.", c="dimmed"))
    children += [_train_row(t, now) for t in journeys[:limit]]
    return html.Div(children)


def _journey_title(depart: str, dest: str) -> str:
    return f"{d.CRS_CODES.get(depart, depart)} → {d.CRS_CODES.get(dest, dest)}"


# ── Layout ────────────────────────────────────────────────────────────────────

SERVER_STARTED = str(time.time_ns())

app = dash.Dash(
    __name__,
    assets_folder="app_assets",
    external_stylesheets=[
        "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.css",
    ],
    external_scripts=[
        "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js",
    ],
    meta_tags=[
        {
            "name": "viewport",
            "content": "width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover",
        }
    ],
    title="Home",
)


def _scroll_rail(target_id: str, horizontal: bool = False) -> html.Div:
    """Thick arrow buttons and a draggable bar for scrolling `target_id` up/down (or
    left/right); app.js wires them up. Dropdowns get the same rail added by app.js."""
    back, fwd = ("tabler:chevron-left", "tabler:chevron-right") if horizontal else ("tabler:chevron-up", "tabler:chevron-down")
    return html.Div(
        className="scroll-rail rail-x" if horizontal else "scroll-rail",
        children=[
            html.Button(DashIconify(icon=back, width=36), className="rail-btn rail-back"),
            html.Div(html.Div(className="rail-thumb"), className="rail-track"),
            html.Button(DashIconify(icon=fwd, width=36), className="rail-btn rail-fwd"),
        ],
        **{"data-target": target_id},
    )


TIMER_PRESETS = [1, 3, 5, 10, 15, 20, 25, 30, 45, 60]


def _timer_modals() -> html.Div:
    """The cooking timer's chooser and its "time's up" screen."""
    big = {"size": "xl", "radius": "lg", "h": 72}
    return html.Div(
        [
            dmc.Modal(
                id="timer-modal",
                title=dmc.Text("Cooking timer", fw=700, size="xl"),
                centered=True,
                size="lg",
                radius="lg",
                children=[
                    html.Div(
                        className="timer-presets",
                        children=[
                            dmc.Button(f"{m} min", id={"type": "timer-preset", "id": m}, variant="light", **big)
                            for m in TIMER_PRESETS
                        ],
                    ),
                    html.Div(
                        id="timer-running-controls",
                        className="timer-presets",
                        children=[
                            dmc.Button("+1 min", id={"type": "timer-add", "id": 1}, variant="default", **big),
                            dmc.Button("+5 min", id={"type": "timer-add", "id": 5}, variant="default", **big),
                            dmc.Button("Stop timer", id="timer-stop", color="red", **big),
                        ],
                    ),
                ],
            ),
            dmc.Modal(
                id="timer-done",
                fullScreen=True,
                withCloseButton=False,
                closeOnClickOutside=False,
                closeOnEscape=False,
                className="timer-done",
                children=html.Div(
                    className="timer-done-body",
                    children=[
                        DashIconify(icon="tabler:alarm", width=140),
                        html.Div("Time's up", className="timer-done-title"),
                        dmc.Button("Stop", id="timer-dismiss", size="xl", radius="xl", color="red", w=280, h=96, fz=32),
                    ],
                ),
            ),
        ]
    )


def _panel(tab_id: str, *children) -> html.Section:
    return html.Section(list(children), id={"type": "panel", "id": tab_id}, className=f"panel panel-{tab_id}")


def serve_layout():
    today_name = datetime.datetime.now(d._LONDON).strftime("%A")
    return dmc.MantineProvider(
        forceColorScheme="dark",
        theme={"primaryColor": "blue", "defaultRadius": "md"},
        children=[
            dcc.Interval(id="int-clock", interval=1_000),
            dcc.Interval(id="int-trains", interval=5_000),
            dcc.Interval(id="int-weather", interval=1_800_000),
            dcc.Interval(id="int-slow", interval=300_000),
            dcc.Interval(id="int-minute", interval=60_000),
            dcc.Interval(id="int-server", interval=10_000),
            dcc.Store(id="tab", data="today"),
            dcc.Store(id="calendar-store"),
            dcc.Store(id="timer-end", storage_type="local"),
            _timer_modals(),
            dcc.Store(id="cooked-saved"),
            dcc.Store(id="shopping-version"),
            dcc.Store(id="food-sig"),
            dcc.Store(id="suggestions-sig"),
            dcc.Store(id="server-started-latest"),
            dcc.Store(id="server-started-seen"),
            html.Div(id="fc-dummy", hidden=True),
            html.Div(
                className="app",
                **{"data-night-dim": "on" if NIGHT_DIM else "off"},
                children=[
                    html.Header(
                        className="app-header",
                        children=[
                            html.Div(id="clock-time", className="clock-time"),
                            html.Div(id="clock-date", className="clock-date"),
                            html.Button(
                                [
                                    DashIconify(icon="tabler:moon", width=26, className="dim-icon-on"),
                                    DashIconify(icon="tabler:moon-off", width=26, className="dim-icon-off"),
                                    html.Span("Night Dim", className="dim-label"),
                                ],
                                id="dim-toggle",
                                className="dim-toggle",
                            ),
                        ],
                    ),
                    html.Div(
                        className="app-body",
                        children=[
                            html.Main(
                                id="app-main",
                                className="app-main",
                                children=[
                                    _panel(
                                        "today",
                                        html.Div(id="today-weather"),
                                        _card(None, html.Div(id="today-trains-title", className="card-title"), html.Div(id="today-trains")),
                                        _card("Today", html.Div(id="today-timeline")),
                                        _card("Tomorrow", html.Div(id="tomorrow-timeline")),
                                        _card("This week's menu", html.Div(id="today-menu")),
                                    ),
                                    _panel(
                                        "trains",
                                        _card(
                                            None,
                                            html.Div(
                                                className="journey-picker",
                                                children=[
                                                    dmc.Select(id="depart", data=STATION_OPTIONS, value="HDW", label="From", size="lg", allowDeselect=False, maxDropdownHeight=420),
                                                    dmc.ActionIcon(
                                                        DashIconify(icon="tabler:arrows-exchange", width=28),
                                                        id="swap",
                                                        size=56,
                                                        variant="light",
                                                        radius="xl",
                                                        className="swap-button",
                                                    ),
                                                    dmc.Select(id="dest", data=STATION_OPTIONS, value="MOG", label="To", size="lg", allowDeselect=False, maxDropdownHeight=420),
                                                ],
                                            ),
                                        ),
                                        _card(None, html.Div(id="trains-list")),
                                        _card("Tube & Elizabeth line", html.Div(id="tube-status")),
                                    ),
                                    _panel(
                                        "calendar",
                                        _card(None, html.Div(id="fc-container"), html.Div(id="calendar-error")),
                                    ),
                                    _panel(
                                        "food",
                                        _card(
                                            None,
                                            dmc.Group(
                                                [DashIconify(icon="tabler:shopping-cart", width=26), dmc.Text("Shopping list", fw=700, size="xl")],
                                                gap="sm",
                                                mb="md",
                                            ),
                                            html.Div(
                                                className="shop-add",
                                                children=[
                                                    dmc.TextInput(id="shop-input", placeholder="Add an item…", size="lg", radius="md"),
                                                    dmc.Button("Add", id="shop-add", size="lg", radius="md", leftSection=DashIconify(icon="tabler:plus", width=22)),
                                                ],
                                            ),
                                            html.Div(id="shopping-list"),
                                            dmc.Group(
                                                [
                                                    dmc.Text(id="shop-count", c="dimmed"),
                                                    dmc.Button(
                                                        "Clear ticked",
                                                        id="shop-clear",
                                                        variant="light",
                                                        color="gray",
                                                        size="md",
                                                        radius="md",
                                                        disabled=True,
                                                        leftSection=DashIconify(icon="tabler:trash", width=20),
                                                    ),
                                                ],
                                                justify="space-between",
                                                mt="sm",
                                            ),
                                            html.Div(id="shop-suggestions"),
                                        ),
                                        html.Div(id="menu-content"),
                                        _card(
                                            "Recipe finder",
                                            dmc.Select(
                                                id="ingredient",
                                                data=[],
                                                placeholder="Pick an ingredient",
                                                size="lg",
                                                clearable=True,
                                                maxDropdownHeight=420,
                                            ),
                                            html.Div(id="recipe-content", className="mt-md"),
                                        ),
                                    ),
                                    _panel(
                                        "schedule",
                                        dmc.SegmentedControl(
                                            id="schedule-day",
                                            data=[{"value": day, "label": day[:3]} for day in d.DAYS],
                                            value=today_name,
                                            fullWidth=True,
                                            size="lg",
                                            radius="lg",
                                            mb="md",
                                        ),
                                        html.Div(id="schedule-content"),
                                    ),
                                ],
                            ),
                            _scroll_rail("app-main"),
                        ],
                    ),
                    html.Nav(
                        className="app-nav",
                        children=[
                            html.Button(
                                [DashIconify(icon=icon, width=30), html.Span(label)],
                                id={"type": "nav", "id": tab_id},
                                n_clicks=0,
                                **{"data-tab": tab_id},
                            )
                            for tab_id, label, icon in TABS
                        ]
                        + [
                            html.Button(
                                [DashIconify(icon="tabler:alarm", width=30), html.Span("Timer", id="timer-text")],
                                id="timer-button",
                                className="timer-button",
                            )
                        ],
                    ),
                ],
            ),
        ],
    )


app.layout = serve_layout

# ── Callbacks: navigation and clock ───────────────────────────────────────────

clientside_callback(
    """function(n) {
        const now = new Date();
        const opts = { timeZone: "Europe/London" };
        return [
            now.toLocaleTimeString("en-GB", { ...opts, hour: "2-digit", minute: "2-digit", hour12: false }),
            now.toLocaleDateString("en-GB", { ...opts, weekday: "long", day: "numeric", month: "long" }),
        ];
    }""",
    Output("clock-time", "children"),
    Output("clock-date", "children"),
    Input("int-clock", "n_intervals"),
)

clientside_callback(
    """function(clicks) {
        const id = dash_clientside.callback_context.triggered_id;
        return id ? id.id : dash_clientside.no_update;
    }""",
    Output("tab", "data"),
    Input({"type": "nav", "id": ALL}, "n_clicks"),
    prevent_initial_call=True,
)

# Show the chosen panel and highlight its button. app.js reads window._tab for swiping.
clientside_callback(
    """function(tab) {
        window._tab = tab;
        const main = document.getElementById("app-main");
        if (main) main.scrollTop = 0;
        if (tab === "calendar" && window._fc) setTimeout(() => window._fc.updateSize(), 0);
        const ctx = dash_clientside.callback_context;
        return [
            ctx.outputs_list[0].map(o => `panel panel-${o.id.id}` + (o.id.id === tab ? " active" : "")),
            ctx.outputs_list[1].map(o => o.id.id === tab ? "active" : ""),
        ];
    }""",
    Output({"type": "panel", "id": ALL}, "className"),
    Output({"type": "nav", "id": ALL}, "className"),
    Input("tab", "data"),
)

clientside_callback(
    """function(n, depart, dest) { return [dest, depart]; }""",
    Output("depart", "value"),
    Output("dest", "value"),
    Input("swap", "n_clicks"),
    State("depart", "value"),
    State("dest", "value"),
    prevent_initial_call=True,
)


# ── Cooking timer (all in the browser; app.js does the beeping) ──

clientside_callback(
    """function(n, end) {
        return [true, end && end > Date.now() ? {} : { display: "none" }];
    }""",
    Output("timer-modal", "opened"),
    Output("timer-running-controls", "style"),
    Input("timer-button", "n_clicks"),
    State("timer-end", "data"),
    prevent_initial_call=True,
)

clientside_callback(
    """function(presets, adds, stop, end) {
        const id = dash_clientside.callback_context.triggered_id;
        if (!id) return dash_clientside.no_update;
        if (id === "timer-stop") return [null, false];
        if (id.type === "timer-preset") return [Date.now() + id.id * 60000, false];
        const base = end && end > Date.now() ? end : Date.now();
        return [base + id.id * 60000, false];
    }""",
    Output("timer-end", "data"),
    Output("timer-modal", "opened", allow_duplicate=True),
    Input({"type": "timer-preset", "id": ALL}, "n_clicks"),
    Input({"type": "timer-add", "id": ALL}, "n_clicks"),
    Input("timer-stop", "n_clicks"),
    State("timer-end", "data"),
    prevent_initial_call=True,
)

clientside_callback(
    """function(n, end) {
        const left = end ? Math.round((end - Date.now()) / 1000) : null;
        const done = left !== null && left <= 0;
        if (window.timerRing) window.timerRing(done);
        if (left === null || done) return ["Timer", "timer-button", done];
        const m = Math.floor(left / 60), sec = String(left % 60).padStart(2, "0");
        return [`${m}:${sec}`, "timer-button running", false];
    }""",
    Output("timer-text", "children"),
    Output("timer-button", "className"),
    Output("timer-done", "opened"),
    Input("int-clock", "n_intervals"),
    Input("timer-end", "data"),
)

clientside_callback(
    """function(n) { if (window.timerRing) window.timerRing(false); return null; }""",
    Output("timer-end", "data", allow_duplicate=True),
    Input("timer-dismiss", "n_clicks"),
    prevent_initial_call=True,
)


# Reload open pages when the app restarts, so they pick up the new version
@app.callback(Output("server-started-latest", "data"), Input("int-server", "n_intervals"))
def report_server_started(_):
    return SERVER_STARTED


clientside_callback(
    """function(latest, seen) {
        if (seen && latest && latest !== seen) window.location.reload();
        return latest || seen;
    }""",
    Output("server-started-seen", "data"),
    Input("server-started-latest", "data"),
    State("server-started-seen", "data"),
)

# ── Callbacks: data ───────────────────────────────────────────────────────────


@app.callback(
    Output("today-trains-title", "children"),
    Output("today-trains", "children"),
    Output("trains-list", "children"),
    Input("int-trains", "n_intervals"),
    Input("depart", "value"),
    Input("dest", "value"),
)
def update_trains(_, depart, dest):
    title = _journey_title(depart, dest)
    return title, _trains_block(depart, dest, 3, today=True), _trains_block(depart, dest, 10)


@app.callback(Output("tube-status", "children"), Input("int-slow", "n_intervals"))
def update_tube_status(_):
    try:
        lines = get_tube_status()
    except Exception as ex:
        return _alert(f"TfL status error: {ex}")
    disrupted = [l for l in lines if l["severity"] < 10]
    good = [l for l in lines if l["severity"] >= 10]
    rows = [
        html.Div(
            className="tube-row",
            children=[
                html.Span(className="tube-swatch", style={"background": TFL_LINE_COLOURS.get(l["id"], "#888")}),
                html.Div(
                    [
                        html.Div([html.Span(l["name"], className="tube-name"), html.Span(l["status"], className="tube-status-text")]),
                        dmc.Text(l["reason"], size="sm", c="dimmed") if l["reason"] else None,
                    ]
                ),
            ],
        )
        for l in disrupted
    ]
    if good:
        rows.append(
            html.Div(
                className="tube-good",
                children=[
                    html.Div(
                        [html.Span(className="tube-dot", style={"background": TFL_LINE_COLOURS.get(l["id"], "#888")}), l["name"]],
                        className="tube-good-line",
                    )
                    for l in good
                ],
            )
        )
        rows.insert(len(disrupted), dmc.Text("Good service on all other lines" if disrupted else "Good service on all lines", c="teal", fw=500, mt="xs" if disrupted else 0))
    return rows


@app.callback(Output("today-weather", "children"), Input("int-weather", "n_intervals"))
def update_weather(_):
    try:
        forecast = d.get_weather(*d.HADLEY_WOOD)
        series = forecast["features"][0]["properties"].get("timeSeries", [])
    except Exception as ex:
        return _card(None, _alert(f"Weather error: {ex}"))
    now = datetime.datetime.now(d._LONDON)
    hours = []
    for entry in series:
        local = dt_parser.isoparse(entry["time"]).astimezone(d._LONDON)
        if local + datetime.timedelta(hours=1) > now:
            hours.append((local, entry))
    hours = hours[:24]
    if not hours:
        return _card(None, _alert("No forecast data.", "yellow"))

    def is_day(t: datetime.datetime) -> bool:
        return 7 <= t.hour < 19

    current_time, current = hours[0]
    code = current.get("significantWeatherCode")
    temps = [e["screenTemperature"] for t, e in hours if t.date() == now.date() and e.get("screenTemperature") is not None]
    high_low = f"High {round(max(temps))}°  ·  Low {round(min(temps))}°" if temps else ""
    wind_mph = round((current.get("windSpeed10m") or 0) * 2.237)
    now_block = html.Div(
        className="weather-now",
        children=[
            _weather_icon(code, is_day(current_time), 96),
            html.Div(
                [
                    html.Div(f"{round(current['screenTemperature'])}°", className="weather-temp"),
                    dmc.Text(WEATHER_CODES.get(code, ("",))[0], size="lg"),
                ]
            ),
            html.Div(
                className="weather-details",
                children=[
                    dmc.Text(f"Feels like {round(current['feelsLikeTemperature'])}°", c="dimmed"),
                    dmc.Text(f"Rain {current.get('probOfPrecipitation', 0)}%", c="dimmed"),
                    dmc.Text(f"Wind {wind_mph} mph", c="dimmed"),
                    dmc.Text(high_low, c="dimmed"),
                ],
            ),
        ],
    )
    strip = html.Div(
        id="hour-strip",
        className="h-scroll hour-strip",
        children=[
            html.Div(
                className="hour-tile",
                children=[
                    dmc.Text("Now" if i == 0 else t.strftime("%H:%M"), size="sm", c="dimmed"),
                    _weather_icon(e.get("significantWeatherCode"), is_day(t), 44),
                    dmc.Text(f"{round(e['screenTemperature'])}°", fw=600, size="lg"),
                    dmc.Text(
                        f"{e.get('probOfPrecipitation', 0)}%",
                        size="sm",
                        c="blue.4" if e.get("probOfPrecipitation", 0) >= 30 else "dimmed",
                    ),
                ],
            )
            for i, (t, e) in enumerate(hours)
        ],
    )
    return _card(None, now_block, strip, _scroll_rail("hour-strip", horizontal=True))


@app.callback(
    Output("calendar-store", "data"),
    Output("calendar-error", "children"),
    Input("int-slow", "n_intervals"),
)
def update_calendar(_):
    try:
        events = d.get_upcoming_events()
    except Exception as ex:
        return [], _alert(f"Calendar error: {ex}")
    calendar = []
    for e in events:
        event = {"title": e["summary"], "start": e["start"], "end": e["end"]}
        if colour := bin_colour(e["summary"]):
            event["color"] = colour
        calendar.append(event)
    return calendar, None


def _allday_chip(title: str) -> html.Span:
    """An all-day event above the timeline; bin collections take the bin's colour."""
    colour = bin_colour(title)
    if not colour:
        return html.Span([html.B("All day", className="tl-allday-tag"), title], className="tl-allday")
    return html.Span(
        [DashIconify(icon="tabler:trash", width=22), html.B("All day", className="tl-allday-tag"), title],
        className="tl-allday tl-bin",
        style={"background": colour},
    )


def _timeline(events: list[dict], day: datetime.date, now: datetime.datetime):
    """A day as a horizontal timeline: hours across the top and one row per event, with
    a bar from its start to its end time, so events never overlap. On today, finished
    events fade and a line marks the current time."""
    is_today = day == now.date()
    day_start = datetime.datetime.combine(day, datetime.time.min)
    day_end = day_start + datetime.timedelta(days=1)
    # All-day events end at midnight on the day after, so Google also returns yesterday's
    all_day = [
        e["summary"]
        for e in events
        if "T" not in e["start"] and e["start"] <= day.isoformat() < e["end"]
    ]
    timed = sorted(
        (
            max(datetime.datetime.fromisoformat(e["start"]), day_start),
            min(datetime.datetime.fromisoformat(e["end"]), day_end),
            e["summary"],
        )
        for e in events
        if "T" in e["start"]
    )
    if not all_day and not timed:
        return dmc.Text("Nothing planned", c="dimmed")

    # Fit the hours shown around the events (and the current time, today), but leave
    # out the small hours unless something is on then
    first = min([max(now.hour, 6) if is_today else 23] + [start.hour for start, _, _ in timed])
    last = max([max(now.hour + 1, 7) if is_today else 0] + [24 if end == day_end else end.hour + (end.minute > 0) for _, end, _ in timed])
    last = min(24, max(last, first + 4))
    window_start = day_start + datetime.timedelta(hours=first)
    minutes = (last - first) * 60

    def pct(t: datetime.datetime) -> float:
        return min(100.0, max(0.0, (t - window_start).total_seconds() / 60 / minutes * 100))

    step = 1 if last - first <= 9 else 2
    hours = range(first, last + 1, step)
    rows = []
    for i, (start, end, title) in enumerate(timed):
        left, right = pct(start), pct(end)
        # Labels run right from the bar's start; if that would go off the edge, they sit
        # to the left of the bar instead (assumes a timeline about 620px wide)
        label_pct = (len(title) + 13) * 9.5 / 620 * 100
        flip = left + label_pct > 100 and left > label_pct
        label_style = {"right": f"{100 - left}%"} if flip else {"left": f"{left}%"}
        rows.append(
            html.Div(
                className="tl-row past" if is_today and end <= now else "tl-row",
                children=[
                    html.Div(
                        className="tl-bar",
                        style={"left": f"{left}%", "width": f"{max(right - left, 1.5)}%", "background": TIMELINE_COLOURS[i % len(TIMELINE_COLOURS)]},
                    ),
                    html.Div(
                        [html.Span(f"{start:%H:%M}–{end:%H:%M}", className="tl-time"), title],
                        className="tl-label tl-label-outside" if flip else "tl-label",
                        style=label_style,
                    ),
                ],
            )
        )
    return html.Div(
        className="tl",
        children=[
            html.Div([_allday_chip(title) for title in all_day], className="tl-alldays") if all_day else None,
            html.Div(
                className="tl-axis",
                children=[html.Span(f"{h:02d}:00", className="tl-hour", style={"left": f"{(h - first) / (last - first) * 100}%"}) for h in hours],
            ) if timed else None,
            html.Div(
                className="tl-rows",
                children=[
                    *[html.Div(className="tl-gridline", style={"left": f"{(h - first) / (last - first) * 100}%"}) for h in hours],
                    *rows,
                    html.Div(className="tl-now", style={"left": f"{pct(now)}%"})
                    if is_today and window_start <= now < day_start + datetime.timedelta(hours=last)
                    else None,
                ],
            ) if timed else None,
        ],
    )


@app.callback(
    Output("today-timeline", "children"),
    Output("tomorrow-timeline", "children"),
    Input("int-minute", "n_intervals"),
)
def update_timelines(_):
    now = d._now_london()
    timelines = []
    for day in (now.date(), now.date() + datetime.timedelta(days=1)):
        try:
            timelines.append(_timeline(get_day_events(day), day, now))
        except Exception as ex:
            timelines.append(_alert(f"Calendar error: {ex}"))
    return timelines


@app.callback(Output("schedule-content", "children"), Input("schedule-day", "value"), Input("int-slow", "n_intervals"))
def update_schedule(day, _):
    try:
        week_start, entries = get_schedule()
    except Exception as ex:
        return _alert(f"Schedule error: {ex}")
    start = _week_start_date(week_start)
    caption = dmc.Text(f"Week of {start:%-d %B %Y}" if start else "", c="dimmed", mb="sm")
    rows = [
        html.Div(
            className="schedule-row",
            children=[
                dmc.Text(entry, c="dimmed", size="sm"),
                dmc.Text(days.get(day, "—"), size="xl", fw=500),
            ],
        )
        for entry, days in entries.items()
    ]
    return [caption, _card(None, *rows)]


def _ingredients_section(items: list[dict] | None):
    """A collapsed "Ingredients" row that opens to show the list."""
    if not items:
        return None
    return dmc.Accordion(
        dmc.AccordionItem(
            [
                dmc.AccordionControl(f"Ingredients ({len(items)})"),
                dmc.AccordionPanel(
                    [
                        html.Div([html.Span(i["name"]), html.Span(i["amount"], className="ingredient-amount")], className="ingredient-row")
                        for i in items
                    ]
                ),
            ],
            value="ingredients",
        ),
        chevronSize=28,
        className="ingredients",
        mt="sm",
    )


def _signature(*data) -> str:
    return hashlib.md5(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


@app.callback(
    Output("menu-content", "children"),
    Output("today-menu", "children"),
    Output("food-sig", "data"),
    Input("int-slow", "n_intervals"),
    State("food-sig", "data"),
)
def update_menu(_, previous):
    try:
        menu = d.get_weekly_menu()
    except Exception as ex:
        return _card("This week's menu", _alert(f"Menu error: {ex}")), _alert(f"Menu error: {ex}"), None
    label_col = menu.columns[0]
    details = menu.set_index(label_col)
    cooked = get_cooked(list(details.columns))
    try:
        ingredients = get_recipe_ingredients()
    except Exception:
        ingredients = {}
    # Rebuilding closes any open ingredients list, so only do it when something changed
    signature = _signature(menu.to_json(), ingredients, sorted(cooked))
    if signature == previous:
        return dash.no_update, dash.no_update, dash.no_update
    cards, summary = [], []
    for recipe in details.columns:
        info = {k: v for k, v in details[recipe].items() if pd.notna(v) and str(v).strip()}
        side_salad = str(info.get("sideSalad", "")).upper() == "TRUE"
        book, page = info.get("recipeBook", ""), str(info.get("pageNumber", "")).strip()
        url = page if re.fullmatch(r"https?://\S+", page) else None
        where = book if url else f"{book}, p. {page}" if book and page else book or (f"p. {page}" if page else "")
        summary.append(
            html.Div(
                className="menu-row",
                children=[
                    dmc.Checkbox(
                        id={"type": "cooked", "id": recipe},
                        checked=recipe in cooked,
                        label=recipe,
                        description=None if url else where or None,
                        size="xl",
                        radius="md",
                        color="green",
                        className="menu-check",
                    ),
                    html.Span(
                        [
                            dmc.Anchor([where or "Recipe", " ", DashIconify(icon="tabler:external-link", width=16)], href=url, target="_blank") if url else None,
                            DashIconify(icon="tabler:salad", width=22) if side_salad else None,
                        ],
                        className="menu-row-meta",
                    ),
                ],
            )
        )
        source = []
        if url:
            source.append(dmc.Anchor([book or "Open recipe", " ", DashIconify(icon="tabler:external-link", width=16)], href=url, target="_blank"))
        elif where:
            source.append(dmc.Text(where, c="dimmed"))
        badges = []
        if total := info.get("totalTime"):
            badges.append(dmc.Badge([f"{total} min"], leftSection=DashIconify(icon="tabler:clock", width=14), variant="light", size="lg"))
        if side_salad:
            badges.append(dmc.Badge("Side salad", leftSection=DashIconify(icon="tabler:salad", width=14), color="green", variant="light", size="lg"))
        notes = [dmc.Text(info["notes"], size="sm", mt="xs")] if info.get("notes") else []
        cards.append(
            dmc.Card(
                [
                    dmc.Text(recipe, fw=600, size="lg"),
                    *source,
                    dmc.Group(badges, gap="xs", mt="sm") if badges else None,
                    *notes,
                    _ingredients_section(ingredients.get(recipe)),
                ],
                withBorder=True,
                radius="lg",
                padding="lg",
                mb="sm",
            )
        )
    return [dmc.Text("This week's menu", fw=600, c="dimmed", size="sm", tt="uppercase", mb="sm"), *cards], summary, signature


@app.callback(
    Output("cooked-saved", "data"),
    Input({"type": "cooked", "id": ALL}, "checked"),
    prevent_initial_call=True,
)
def update_cooked(checked):
    # Only save the box that was tapped. The list is rebuilt every few minutes, which
    # also fires this callback, and saving every box then could undo a fresh tick.
    tapped = dash.callback_context.triggered_id
    if not tapped:
        return dash.no_update
    ids = [i["id"]["id"] for i in dash.callback_context.inputs_list[0]]
    set_tick(ids, "cooked", tapped["id"], bool(checked[ids.index(tapped["id"])]))
    return time.time()


@app.callback(
    Output("shopping-version", "data"),
    Output("shop-input", "value"),
    Input("shop-add", "n_clicks"),
    Input("shop-input", "n_submit"),
    Input({"type": "shop-suggest", "id": ALL}, "n_clicks"),
    Input({"type": "shop-item", "id": ALL}, "checked"),
    Input({"type": "shop-remove", "id": ALL}, "n_clicks"),
    Input("shop-clear", "n_clicks"),
    State("shop-input", "value"),
    prevent_initial_call=True,
)
def change_shopping(_add, _submit, _suggest, checked, _remove, _clear, text):
    tapped = dash.callback_context.triggered_id
    # Redrawing the list also fires this (with no trigger, or buttons with no clicks
    # yet); only act on what was actually tapped
    is_tick = isinstance(tapped, dict) and tapped["type"] == "shop-item"
    if not tapped or not (is_tick or dash.callback_context.triggered[0]["value"]):
        return dash.no_update, dash.no_update
    if tapped in ("shop-add", "shop-input"):
        add_shopping_item(text or "")
        return time.time(), ""
    if tapped == "shop-clear":
        remove_shopping_items()
    elif tapped["type"] == "shop-suggest":
        add_shopping_item(tapped["id"])
    elif tapped["type"] == "shop-remove":
        remove_shopping_items({tapped["id"]})
    elif tapped["type"] == "shop-item":
        ids = [i["id"]["id"] for i in dash.callback_context.inputs_list[3]]
        tick_shopping_item(tapped["id"], bool(checked[ids.index(tapped["id"])]))
    return time.time(), dash.no_update


@app.callback(
    Output("shopping-list", "children"),
    Output("shop-count", "children"),
    Output("shop-clear", "disabled"),
    Input("shopping-version", "data"),
    Input("int-slow", "n_intervals"),
)
def show_shopping(_, __):
    items = get_shopping()
    if not items:
        return dmc.Text("Nothing on the list yet.", c="dimmed", my="md"), "", True
    to_buy = [i for i in items if not i["done"]]
    bought = [i for i in items if i["done"]]
    rows = [
        html.Div(
            className="shop-row",
            children=[
                dmc.Checkbox(
                    id={"type": "shop-item", "id": item["id"]},
                    checked=item["done"],
                    label=item["name"],
                    size="xl",
                    radius="md",
                    color="green",
                    className="shop-check",
                ),
                dmc.ActionIcon(
                    DashIconify(icon="tabler:x", width=24),
                    id={"type": "shop-remove", "id": item["id"]},
                    size=48,
                    variant="subtle",
                    color="gray",
                    radius="xl",
                ),
            ],
        )
        for item in to_buy + bought
    ]
    count = f"{len(to_buy)} to buy" + (f" · {len(bought)} ticked" if bought else "")
    return html.Div(rows, className="shop-list"), count, not bought


@app.callback(
    Output("shop-suggestions", "children"),
    Output("suggestions-sig", "data"),
    Input("int-slow", "n_intervals"),
    State("suggestions-sig", "data"),
)
def show_suggestions(_, previous):
    """This week's ingredients as buttons, for adding to the list without a keyboard.
    Only rebuilt when the ingredients change, so the section stays open while in use."""
    try:
        items = get_shopping_list(list(d.get_weekly_menu().columns[1:]))
    except Exception:
        return None, None
    names = sorted({i["name"] for i in items}, key=str.lower)
    signature = _signature(names)
    if signature == previous:
        return dash.no_update, dash.no_update
    return (
        dmc.Accordion(
            dmc.AccordionItem(
                [
                    dmc.AccordionControl("Add from this week's recipes"),
                    dmc.AccordionPanel(
                        html.Div(
                            [
                                dmc.Button(name, id={"type": "shop-suggest", "id": name}, variant="light", size="md", radius="xl")
                                for name in names
                            ],
                            className="shop-suggest-list",
                        )
                    ),
                ],
                value="suggestions",
            ),
            chevronSize=28,
            className="shop-suggestions",
            mt="md",
        ),
        signature,
    )


@app.callback(Output("ingredient", "data"), Input("int-slow", "n_intervals"))
def update_ingredient_options(_):
    try:
        on_menu = d.get_menu_recipe_names()
        recipes = get_recipe_ingredients()
    except Exception:
        return []
    return sorted({i["name"] for recipe, items in recipes.items() if recipe in on_menu for i in items})


@app.callback(
    Output("recipe-content", "children"),
    Input("ingredient", "value"),
    Input("cooked-saved", "data"),
)
def update_recipes(ingredient, _):
    if not ingredient:
        return None
    try:
        recipes = get_recipe_ingredients()
        cooked = get_cooked(list(d.get_weekly_menu().columns[1:]))
    except Exception as ex:
        return _alert(f"Recipe error: {ex}")
    matches = [
        (recipe, i["amount"])
        for recipe, items in recipes.items()
        for i in items
        if i["name"] == ingredient
    ]
    if not matches:
        return dmc.Text(f"No recipes use {ingredient}.", c="dimmed")
    return [
        html.Div(
            className="finder-row",
            children=[
                html.Div([dmc.Text(recipe, fw=500), dmc.Text(amount or "Amount not given", c="dimmed", size="sm")]),
                dmc.Badge("Cooked", color="green", variant="filled", size="lg", leftSection=DashIconify(icon="tabler:check", width=16))
                if recipe in cooked
                else dmc.Badge("Not cooked yet", color="gray", variant="light", size="lg"),
            ],
        )
        for recipe, amount in matches
    ]


clientside_callback(
    """function(events) {
        const el = document.getElementById("fc-container");
        if (!events || !el) return "";
        if (window._fc) {
            window._fc.removeAllEventSources();
            window._fc.addEventSource(events);
            return "";
        }
        window._fc = new FullCalendar.Calendar(el, {
            initialView: "dayGridMonth",
            timeZone: "local",
            height: "auto",
            firstDay: 1,
            dayMaxEvents: false,
            eventDisplay: "block",
            events: events,
            headerToolbar: { left: "prev,next", center: "title", right: "today" },
            buttonText: { today: "Today" },
            eventTimeFormat: { hour: "2-digit", minute: "2-digit", hour12: false },
            nowIndicator: true,
        });
        window._fc.render();
        return "";
    }""",
    Output("fc-dummy", "children"),
    Input("calendar-store", "data"),
)


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    if all(os.environ.get(k) for k in d._DARWIN_REQUIRED_ENV):
        threading.Thread(target=d._run_darwin_feed, name="darwin-feed", daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
