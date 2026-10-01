"""Phone-style version of the dashboard for a portrait touch screen.

Runs alongside scripts/dash_dashboard.py (port 8051 instead of 8050) and reuses its
data fetching. Each section is a full-screen tab picked from a bottom bar, or by
swiping left and right; the app returns to the Today tab after a few idle minutes.
"""

import datetime
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import dash
import dash_mantine_components as dmc
import pandas as pd
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

_ingredients_cache: TTLCache = TTLCache(maxsize=1, ttl=300)
_day_events_cache: TTLCache = TTLCache(maxsize=4, ttl=300)

TIMELINE_COLOURS = ["#1c7ed6", "#0c8599", "#9c36b5", "#e8590c", "#4263eb", "#c2255c", "#2f9e44"]

FRACTIONS = {0.25: "¼", 0.33: "⅓", 0.5: "½", 0.67: "⅔", 0.75: "¾"}

COOKED_FILE = Path(__file__).resolve().parent.parent / ".cooked.json"
_cooked_lock = threading.Lock()

# ── Data ──────────────────────────────────────────────────────────────────────


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


def get_recipe_ingredients() -> dict[str, list[tuple[str, str]]]:
    """{recipe: [(ingredient, amount)]} from the ingredients sheet. The recipe name is
    only filled in on each recipe's first row."""
    if "ingredients" in _ingredients_cache:
        return _ingredients_cache["ingredients"]
    df = pd.read_csv(d._sheet_url(d.RECIPE_SHEET_ID, gid=d.RECIPE_INGREDIENTS_GID))
    recipes: dict[str, list[tuple[str, str]]] = {}
    current = None
    for row in df.itertuples(index=False):
        name, _, display, amount, unit = row[:5]
        if pd.notna(name) and str(name).strip():
            current = str(name).strip()
        if current and pd.notna(display) and str(display).strip():
            recipes.setdefault(current, []).append((str(display).strip(), _format_amount(amount, unit)))
    _ingredients_cache["ingredients"] = recipes
    return recipes


def _read_cooked(menu: list[str]) -> set[str]:
    try:
        saved = json.loads(COOKED_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return set()
    if sorted(saved.get("menu", [])) != sorted(menu):
        COOKED_FILE.unlink(missing_ok=True)
        return set()
    return set(saved.get("cooked", [])) & set(menu)


def get_cooked(menu: list[str]) -> set[str]:
    """Recipes ticked off as cooked. Ticks belong to one weekly menu: as soon as the
    menu changes at all the saved ticks are deleted, so they never come back even if
    the same recipes return later."""
    with _cooked_lock:
        return _read_cooked(menu)


def set_cooked(menu: list[str], recipe: str, done: bool) -> None:
    """Tick or untick one recipe, keeping whatever else is already ticked."""
    with _cooked_lock:
        cooked = _read_cooked(menu)
        cooked = cooked | {recipe} if done else cooked - {recipe}
        data = json.dumps({"menu": sorted(menu), "cooked": sorted(cooked & set(menu))}, indent=2)
        tmp = COOKED_FILE.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(COOKED_FILE)


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


def _train_row(train: dict, now: datetime.datetime) -> html.Div:
    """Same colours as the main dashboard: red when cancelled or leaving within 10
    minutes, green from 10 to 20 minutes away."""
    etd = train["ETD"]
    leaves = d._nearest(now, etd if re.fullmatch(r"\d\d:\d\d", etd) else train["Time"])
    minutes = round((leaves - now).total_seconds() / 60)
    if etd == "Cancelled" or minutes <= 10:
        urgency = "red"
    elif minutes <= 20:
        urgency = "green"
    else:
        urgency = "none"
    if etd == "On time":
        badge = dmc.Badge("On time", color="teal", variant="light", size="lg")
    elif etd in ("Cancelled", "Delayed"):
        badge = dmc.Badge(etd, color="red", variant="filled", size="lg")
    else:
        badge = dmc.Badge(f"Exp {etd}", color="orange", variant="light", size="lg")
    when = "" if etd == "Cancelled" else "now" if minutes <= 0 else f"in {minutes} min"
    return html.Div(
        className=f"train-row urgency-{urgency}",
        children=[
            html.Div(
                [
                    html.Div(train["Time"], className="train-time"),
                    dmc.Text(train["Destination"], c="dimmed"),
                ]
            ),
            html.Div(
                className="train-right",
                children=[
                    badge,
                    dmc.Text(f"Platform {train['Platform']}  ·  {when}" if when else f"Platform {train['Platform']}", size="sm", c="dimmed"),
                ],
            ),
        ],
    )


def _trains_block(depart: str, dest: str, limit: int):
    missing = [k for k in d._DARWIN_REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        return _alert(f"{', '.join(missing)} not set.")
    status = dict(d._darwin_status)
    if not status["ready"]:
        if status["error"]:
            return _alert(f"Rail feed error: {status['error']}")
        return dmc.Group([dmc.Loader(size="sm"), dmc.Text("Loading live train data…", c="dimmed")])
    _, departures = d.get_departures(depart, dest)
    children = []
    last = status["last_message"]
    as_of = last.strftime("%H:%M:%S") if last else "—"
    if not status["connected"]:
        children.append(_alert(f"Live feed reconnecting. Showing data as of {as_of}.", "yellow"))
    elif not last or datetime.datetime.now(d._LONDON) - last > datetime.timedelta(minutes=2):
        children.append(_alert(f"No live updates since {as_of}.", "yellow"))
    if not departures:
        children.append(dmc.Text("No direct trains right now.", c="dimmed"))
    now = d._now_london()
    children += [_train_row(t, now) for t in departures[:limit]]
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


def _panel(tab_id: str, *children) -> html.Section:
    return html.Section(list(children), id={"type": "panel", "id": tab_id}, className="panel")


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
            dcc.Store(id="cooked-saved"),
            dcc.Store(id="server-started-latest"),
            dcc.Store(id="server-started-seen"),
            html.Div(id="fc-dummy", hidden=True),
            html.Div(
                className="app",
                children=[
                    html.Header(
                        className="app-header",
                        children=[
                            html.Div(id="clock-time", className="clock-time"),
                            html.Div(id="clock-date", className="clock-date"),
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
                                    ),
                                    _panel(
                                        "calendar",
                                        _card(None, html.Div(id="fc-container"), html.Div(id="calendar-error")),
                                    ),
                                    _panel(
                                        "food",
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
            ctx.outputs_list[0].map(o => o.id.id === tab ? "panel active" : "panel"),
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
    return title, _trains_block(depart, dest, 3), _trains_block(depart, dest, 10)


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
    return [{"title": e["summary"], "start": e["start"], "end": e["end"]} for e in events], None


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
            html.Div([html.Span([html.B("All day", className="tl-allday-tag"), title], className="tl-allday") for title in all_day], className="tl-alldays") if all_day else None,
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


def _ingredients_section(items: list[tuple[str, str]] | None):
    """A collapsed "Ingredients" row that opens to show the list."""
    if not items:
        return None
    return dmc.Accordion(
        dmc.AccordionItem(
            [
                dmc.AccordionControl(f"Ingredients ({len(items)})"),
                dmc.AccordionPanel(
                    [
                        html.Div([html.Span(name), html.Span(amount, className="ingredient-amount")], className="ingredient-row")
                        for name, amount in items
                    ]
                ),
            ],
            value="ingredients",
        ),
        chevronSize=28,
        className="ingredients",
        mt="sm",
    )


@app.callback(
    Output("menu-content", "children"),
    Output("today-menu", "children"),
    Input("int-slow", "n_intervals"),
)
def update_menu(_):
    try:
        menu = d.get_weekly_menu()
    except Exception as ex:
        return _card("This week's menu", _alert(f"Menu error: {ex}")), _alert(f"Menu error: {ex}")
    label_col = menu.columns[0]
    details = menu.set_index(label_col)
    cooked = get_cooked(list(details.columns))
    try:
        ingredients = get_recipe_ingredients()
    except Exception:
        ingredients = {}
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
    return [dmc.Text("This week's menu", fw=600, c="dimmed", size="sm", tt="uppercase", mb="sm"), *cards], summary


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
    set_cooked(ids, tapped["id"], bool(checked[ids.index(tapped["id"])]))
    return time.time()


@app.callback(Output("ingredient", "data"), Input("int-slow", "n_intervals"))
def update_ingredient_options(_):
    try:
        on_menu = d.get_menu_recipe_names()
        recipes = get_recipe_ingredients()
    except Exception:
        return []
    return sorted({name for recipe, items in recipes.items() if recipe in on_menu for name, _ in items})


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
        (recipe, amount)
        for recipe, items in recipes.items()
        for name, amount in items
        if name == ingredient
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
