import datetime
import os
import threading
import urllib.parse

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import pytz
import requests
from cachetools import TTLCache
from dash import MATCH, Input, Output, State, clientside_callback, ctx, dcc, html
from dateutil import parser as dt_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from zeep import Client, xsd
from zeep.plugins import HistoryPlugin
from zeep.settings import Settings

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
HADLEY_WOOD = (51.6512, -0.1442)
RECIPE_SHEET_ID = "1qMt1jKFf3OVILmA-MsQ8Ga-8vsYLsCX0ky00zairf9M"
SCHEDULE_SHEET_ID = "1AFQrHf15-Pzyvbmn9jzPU1FvXnW4u9VllsIWAQ0Mq6U"
RECIPE_INGREDIENTS_GID = "802866866"
RAIL_WSDL = "http://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

CRS_CODES = {
    "HDW": "Hadley Wood",
    "MOG": "Moorgate",
    "OLD": "Old Street",
    "EXR": "Essex Road",
    "HHY": "Highbury & Islington",
    "DYP": "Drayton Park",
    "FPK": "Finsbury Park",
    "HGY": "Harringay",
    "HRN": "Hornsey",
    "AAP": "Alexandra Palace",
    "NSG": "New Southgate",
    "OKL": "Oakleigh Park",
    "NBA": "New Barnet",
    "PBR": "Potters Bar",
    "BPK": "Brookmans Park",
    "WMG": "Welham Green",
    "HAT": "Hatfield",
    "WGC": "Welwyn Garden City",
}

# ── Caching ────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_weather_cache: TTLCache = TTLCache(maxsize=4, ttl=1800)
_menu_cache: TTLCache = TTLCache(maxsize=1, ttl=300)
_departures_cache: TTLCache = TTLCache(maxsize=20, ttl=60)
_calendar_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

# ── Data fetching ─────────────────────────────────────────────────────────────


def _sheet_url(
    sheet_id: str, *, name: str | None = None, gid: str | None = None
) -> str:
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return f"{base}&sheet={urllib.parse.quote(name)}" if name else f"{base}&gid={gid}"


def _google_creds() -> Credentials:
    creds = (
        Credentials.from_authorized_user_file("token.json", CALENDAR_SCOPES)
        if os.path.exists("token.json")
        else None
    )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", CALENDAR_SCOPES
            ).run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return creds


def get_upcoming_events(max_results: int = 30) -> list[dict]:
    with _lock:
        if "events" in _calendar_cache:
            return _calendar_cache["events"]
    service = build("calendar", "v3", credentials=_google_creds())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    calendars = service.calendarList().list().execute().get("items", [])
    calendar_id = next(
        (c["id"] for c in calendars if c.get("summary") == "29ClaremontRoad"), None
    )
    if not calendar_id:
        raise ValueError("Calendar '29ClaremontRoad' not found.")
    items = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    result = [
        {
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "end": e["end"].get("dateTime", e["end"].get("date")),
            "summary": e.get("summary", "No Title"),
        }
        for e in items
    ]
    with _lock:
        _calendar_cache["events"] = result
    return result


def get_weather(lat: float, lon: float) -> dict:
    key = (lat, lon)
    with _lock:
        if key in _weather_cache:
            return _weather_cache[key]
    api_key = os.getenv("MET_OFFICE_API_KEY", "")
    if not api_key:
        raise ValueError("MET_OFFICE_API_KEY not set.")
    resp = requests.get(
        "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly",
        headers={"accept": "application/json", "apikey": api_key},
        params={
            "excludeParameterMetadata": "FALSE",
            "includeLocationName": "TRUE",
            "latitude": lat,
            "longitude": lon,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    with _lock:
        _weather_cache[key] = result
    return result


def get_departures(crs: str, token: str, num_rows: int = 10):
    key = (crs, token)
    with _lock:
        if key in _departures_cache:
            return _departures_cache[key]
    client = Client(
        wsdl=RAIL_WSDL, settings=Settings(strict=False), plugins=[HistoryPlugin()]
    )
    header = xsd.Element(
        "{http://thalesgroup.com/RTTI/2013-11-28/Token/types}AccessToken",
        xsd.ComplexType(
            [
                xsd.Element(
                    "{http://thalesgroup.com/RTTI/2013-11-28/Token/types}TokenValue",
                    xsd.String(),
                )
            ]
        ),
    )
    res = client.service.GetDepartureBoard(
        numRows=num_rows, crs=crs, _soapheaders=[header(TokenValue=token)]
    )
    result = (res.locationName, res.generatedAt, res.trainServices.service)
    with _lock:
        _departures_cache[key] = result
    return result


def _load_menu_sheet() -> pd.DataFrame:
    with _lock:
        if "menu" in _menu_cache:
            return _menu_cache["menu"]
    df = pd.read_csv(_sheet_url(RECIPE_SHEET_ID, name="thisWeekMenuMaker"))
    with _lock:
        _menu_cache["menu"] = df
    return df


def get_weekly_menu() -> pd.DataFrame:
    df = _load_menu_sheet()
    return df.iloc[3:].dropna(axis=1, thresh=1).reset_index(drop=True)


def get_menu_recipe_names() -> set[str]:
    df = _load_menu_sheet()
    final = df[df.iloc[:, 0] == "final"]
    if final.empty:
        return set()
    return {str(v).strip() for v in final.iloc[0, 1:] if pd.notna(v) and str(v).strip()}


def get_schedule_summary() -> pd.DataFrame:
    df = pd.read_csv(_sheet_url(SCHEDULE_SHEET_ID, name="Form Responses 1"))
    if df.empty:
        return pd.DataFrame()
    latest = df.iloc[-1]
    collated: dict = {}
    for col in df.columns:
        for day in DAYS:
            if day.lower() in col.lower():
                entry = col.split(day)[0].strip().replace("_", " ")
                collated.setdefault(entry, {"Entry": entry})[day] = latest[col]
                break
        else:
            collated[col] = latest[col]
    entries = {k: v for k, v in collated.items() if isinstance(v, dict)}
    summary = pd.DataFrame(entries.values()) if entries else pd.DataFrame([collated])
    if "Timestamp" in summary.columns:
        summary = summary.drop(columns=["Timestamp"])
    if "Entry" in summary.columns:
        summary["Entry"] = summary["Entry"].str.replace(r"[\[\]\(\)]", "", regex=True)
    return summary.iloc[:-1].reset_index(drop=True) if len(summary) > 1 else summary


def get_ingredients() -> list[str]:
    menu_recipes = get_menu_recipe_names()
    df = pd.read_csv(
        _sheet_url(RECIPE_SHEET_ID, gid=RECIPE_INGREDIENTS_GID), skiprows=3
    )
    current, ingredients = None, set()
    for _, row in df.iterrows():
        first = row.iloc[0]
        if pd.notna(first) and str(first).strip():
            current = str(first).strip()
        if not current or current not in menu_recipes:
            continue
        display = row.iloc[2]
        if pd.notna(display) and str(display).strip():
            ingredients.add(str(display).strip())
    return sorted(ingredients)


def get_recipes_for_ingredient(ingredient: str) -> dict[str, str]:
    df = pd.read_csv(
        _sheet_url(RECIPE_SHEET_ID, gid=RECIPE_INGREDIENTS_GID), skiprows=3
    )
    needle = ingredient.lower()
    current, matches = None, {}
    for _, row in df.iterrows():
        first = row.iloc[0]
        if pd.notna(first) and str(first).strip():
            current = str(first).strip()
        if not current or current in matches:
            continue
        if any(needle in str(v).lower() for v in row if pd.notna(v)):
            amount = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else ""
            unit = str(row.iloc[4]).strip() if pd.notna(row.iloc[4]) else ""
            matches[current] = f"{amount} {unit}".strip() or "Amount not specified"
    return matches


# ── Helpers ───────────────────────────────────────────────────────────────────

CRS_OPTIONS = [{"label": v, "value": k} for k, v in CRS_CODES.items()]

CARD_STYLE = {"marginBottom": "1.25rem", "borderRadius": "12px"}
HEADER_STYLE = {
    "borderRadius": "12px 12px 0 0",
    "fontSize": "1.1rem",
    "fontWeight": "600",
}
ROW_STYLE = {"fontSize": "1.05rem", "padding": "0.75rem 1rem"}


def _table(df: pd.DataFrame) -> dbc.Table:
    return dbc.Table.from_dataframe(
        df,
        striped=True,
        hover=True,
        responsive=True,
        style={"fontSize": "1.05rem", "marginBottom": 0},
    )


def _error(msg: str) -> dbc.Alert:
    return dbc.Alert(msg, color="danger", className="mb-0")


def _warn(msg: str) -> dbc.Alert:
    return dbc.Alert(msg, color="warning", className="mb-0")


# ── App ───────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.DARKLY,
        "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.css",
    ],
    external_scripts=[
        "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js",
    ],
    meta_tags=[
        {
            "name": "viewport",
            "content": "width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no",
        }
    ],
    title="Super Octo System",
)

app.layout = dbc.Container(
    fluid=True,
    style={"paddingBottom": "2rem"},
    children=[
        # Intervals + calendar store
        dcc.Interval(id="int-clock", interval=1_000),
        dcc.Interval(id="int-trains", interval=60_000),
        dcc.Interval(id="int-weather", interval=1_800_000),
        dcc.Interval(id="int-slow", interval=300_000),
        dcc.Store(id="calendar-store"),
        html.Div(id="fc-dummy", style={"display": "none"}),
        # ── Header ────────────────────────────────────────────────────────────────
        dbc.Row(
            className="my-3 align-items-end",
            children=[
                dbc.Col(
                    html.Div(
                        id="clock",
                        style={
                            "fontSize": "2rem",
                            "fontWeight": "700",
                            "lineHeight": "1.1",
                        },
                    )
                ),
                dbc.Col(
                    html.Small(
                        id="last-updated", className="text-muted text-end d-block"
                    )
                ),
            ],
        ),
        # ── Calendar ──────────────────────────────────────────────────────────────
        dbc.Card(
            style=CARD_STYLE,
            children=[
                dbc.CardHeader(
                    dbc.Button(
                        "Google Calendar",
                        id={"type": "toggle", "id": "calendar"},
                        color="link",
                        className="w-100 text-start text-white fw-semibold p-0",
                        style={"fontSize": "1.1rem", "textDecoration": "none"},
                    ),
                    style=HEADER_STYLE,
                ),
                dbc.Collapse(
                    dbc.CardBody(
                        html.Div(id="fc-container"),
                        style={"padding": "0.5rem 1rem 1rem"},
                    ),
                    id={"type": "collapse", "id": "calendar"},
                    is_open=True,
                ),
            ],
        ),
        # ── Trains ────────────────────────────────────────────────────────────────
        dbc.Card(
            style=CARD_STYLE,
            children=[
                dbc.CardHeader(
                    dbc.Button(
                        "National Rail Departures",
                        id={"type": "toggle", "id": "trains"},
                        color="link",
                        className="w-100 text-start text-white fw-semibold p-0",
                        style={"fontSize": "1.1rem", "textDecoration": "none"},
                    ),
                    style=HEADER_STYLE,
                ),
                dbc.Collapse(
                    dbc.CardBody(
                        [
                            dbc.Row(
                                className="mb-3 g-2",
                                children=[
                                    dbc.Col(
                                        dcc.Dropdown(
                                            id="depart",
                                            options=CRS_OPTIONS,
                                            value="HDW",
                                            clearable=False,
                                            style={"fontSize": "1.05rem"},
                                        ),
                                        width=5,
                                    ),
                                    dbc.Col(
                                        dcc.Dropdown(
                                            id="dest",
                                            options=CRS_OPTIONS,
                                            value="MOG",
                                            clearable=False,
                                            style={"fontSize": "1.05rem"},
                                        ),
                                        width=5,
                                    ),
                                    dbc.Col(
                                        dbc.Button(
                                            "↻ Refresh",
                                            id="btn-refresh-trains",
                                            color="secondary",
                                            size="sm",
                                            className="w-100",
                                        ),
                                        width=2,
                                    ),
                                ],
                            ),
                            html.Div(id="trains-content"),
                        ]
                    ),
                    id={"type": "collapse", "id": "trains"},
                    is_open=True,
                ),
            ],
        ),
        # ── Weather ───────────────────────────────────────────────────────────────
        dbc.Card(
            style=CARD_STYLE,
            children=[
                dbc.CardHeader(
                    dbc.Button(
                        "Met Office Forecast – Hadley Wood",
                        id={"type": "toggle", "id": "weather"},
                        color="link",
                        className="w-100 text-start text-white fw-semibold p-0",
                        style={"fontSize": "1.1rem", "textDecoration": "none"},
                    ),
                    style=HEADER_STYLE,
                ),
                dbc.Collapse(
                    dbc.CardBody(id="weather-content"),
                    id={"type": "collapse", "id": "weather"},
                    is_open=True,
                ),
            ],
        ),
        # ── Menu ──────────────────────────────────────────────────────────────────
        dbc.Card(
            style=CARD_STYLE,
            children=[
                dbc.CardHeader(
                    dbc.Button(
                        "This Week's Menu",
                        id={"type": "toggle", "id": "menu"},
                        color="link",
                        className="w-100 text-start text-white fw-semibold p-0",
                        style={"fontSize": "1.1rem", "textDecoration": "none"},
                    ),
                    style=HEADER_STYLE,
                ),
                dbc.Collapse(
                    dbc.CardBody(id="menu-content"),
                    id={"type": "collapse", "id": "menu"},
                    is_open=True,
                ),
            ],
        ),
        # ── Recipe Finder ─────────────────────────────────────────────────────────
        dbc.Card(
            style=CARD_STYLE,
            children=[
                dbc.CardHeader(
                    dbc.Button(
                        "Recipe Finder",
                        id={"type": "toggle", "id": "recipes"},
                        color="link",
                        className="w-100 text-start text-white fw-semibold p-0",
                        style={"fontSize": "1.1rem", "textDecoration": "none"},
                    ),
                    style=HEADER_STYLE,
                ),
                dbc.Collapse(
                    dbc.CardBody(
                        [
                            dcc.Dropdown(
                                id="ingredient",
                                options=[],
                                placeholder="Select an ingredient…",
                                clearable=True,
                                style={"fontSize": "1.05rem"},
                            ),
                            html.Div(id="recipe-content", className="mt-3"),
                        ]
                    ),
                    id={"type": "collapse", "id": "recipes"},
                    is_open=True,
                ),
            ],
        ),
        # ── Schedule ──────────────────────────────────────────────────────────────
        dbc.Card(
            style=CARD_STYLE,
            children=[
                dbc.CardHeader(
                    dbc.Button(
                        "Household Schedule",
                        id={"type": "toggle", "id": "schedule"},
                        color="link",
                        className="w-100 text-start text-white fw-semibold p-0",
                        style={"fontSize": "1.1rem", "textDecoration": "none"},
                    ),
                    style=HEADER_STYLE,
                ),
                dbc.Collapse(
                    dbc.CardBody(id="schedule-content"),
                    id={"type": "collapse", "id": "schedule"},
                    is_open=True,
                ),
            ],
        ),
    ],
)

# ── Callbacks ─────────────────────────────────────────────────────────────────

# Clock — runs entirely in the browser, no server round-trip
clientside_callback(
    """function(n) {
        return new Date().toLocaleString("en-GB", {
            timeZone: "Europe/London",
            weekday: "long", year: "numeric", month: "long", day: "numeric",
            hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
        });
    }""",
    Output("clock", "children"),
    Input("int-clock", "n_intervals"),
)

clientside_callback(
    """function(n) {
        return "Last updated: " + new Date().toLocaleString("en-GB", {
            timeZone: "Europe/London",
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
        });
    }""",
    Output("last-updated", "children"),
    Input("int-slow", "n_intervals"),
)


@app.callback(
    Output({"type": "collapse", "id": MATCH}, "is_open"),
    Input({"type": "toggle", "id": MATCH}, "n_clicks"),
    State({"type": "collapse", "id": MATCH}, "is_open"),
    prevent_initial_call=True,
)
def toggle_section(_, is_open):
    return not is_open


@app.callback(Output("calendar-store", "data"), Input("int-slow", "n_intervals"))
def update_calendar(_):
    try:
        events = get_upcoming_events()
        return [
            {"title": e["summary"], "start": e["start"], "end": e["end"]}
            for e in events
        ]
    except Exception:
        return []


clientside_callback(
    """function(events) {
        if (!events) return "";

        const el = document.getElementById("fc-container");
        if (!el) return "";

        if (window._fc) {
            window._fc.removeAllEventSources();
            window._fc.addEventSource(events);
            return "";
        }

        window._fc = new FullCalendar.Calendar(el, {
            initialView: "dayGridMonth",
            timeZone: "Europe/London",
            height: "auto",
            events: events,
            headerToolbar: {
                left: "prev,next today",
                center: "title",
                right: "dayGridMonth,timeGridWeek,listWeek",
            },
            buttonText: { month: "Month", week: "Week", list: "List" },
            eventTimeFormat: { hour: "2-digit", minute: "2-digit", hour12: false },
            nowIndicator: true,
        });
        window._fc.render();
        return "";
    }""",
    Output("fc-dummy", "children"),
    Input("calendar-store", "data"),
)


@app.callback(
    Output("trains-content", "children"),
    Input("int-trains", "n_intervals"),
    Input("btn-refresh-trains", "n_clicks"),
    Input("depart", "value"),
    Input("dest", "value"),
)
def update_trains(_, __, depart, dest):
    token = os.environ.get("DARWIN_LITE_TOKEN")
    if not token:
        return _error("DARWIN_LITE_TOKEN not set.")
    if ctx.triggered_id == "btn-refresh-trains":
        with _lock:
            _departures_cache.pop((depart, token), None)
    try:
        location, generated, services = get_departures(depart, token)
    except Exception as ex:
        return _error(f"Rail API error: {ex}")
    if not services:
        return _warn("No services found.")
    data = [
        {
            "Time": t.std,
            "Destination": t.destination.location[0].locationName,
            "Platform": t.platform or "—",
            "ETD": t.etd,
        }
        for t in services
        if getattr(t.destination.location[0], "crs", "") == dest
    ]
    if not data:
        return _warn("No trains to the selected destination.")
    london_tz = pytz.timezone("Europe/London")
    generated_local = generated.astimezone(london_tz).strftime("%Y-%m-%d %H:%M:%S")
    return html.Div(
        [
            html.Small(
                f"From {location} as of {generated_local}",
                className="text-muted d-block mb-2",
            ),
            _table(pd.DataFrame(data)),
        ]
    )


@app.callback(
    Output("weather-content", "children"), Input("int-weather", "n_intervals")
)
def update_weather(_):
    try:
        forecast = get_weather(*HADLEY_WOOD)
        if not (forecast.get("features") and forecast["features"]):
            return _warn("No forecast data.")
        london_tz = pytz.timezone("Europe/London")
        today = datetime.datetime.now(london_tz).date()
        tomorrow = today + datetime.timedelta(days=1)
        rows: dict = {}
        for entry in forecast["features"][0]["properties"].get("timeSeries", []):
            local = dt_parser.isoparse(entry["time"]).astimezone(london_tz)
            if local.date() not in (today, tomorrow):
                continue
            temp = entry.get("feelsLikeTemperature")
            precip = entry.get("probOfPrecipitation")
            t = round(temp) if temp is not None else None
            t_icon = (
                "❄"
                if (t is not None and t <= 5)
                else "♨"
                if (t is not None and t >= 25)
                else "◑"
            )
            r_icon = (
                "☂"
                if (precip is not None and precip >= 70)
                else "☁"
                if (precip is not None and precip >= 30)
                else "☀"
            )
            label = (
                local.strftime("%H:%M")
                if local.date() == today
                else local.strftime("T+1 %H:%M")
            )
            rows[label] = {
                "Feels Like (°C)": f"{t_icon} {t}",
                "Precip (%)": f"{r_icon} {precip}",
            }
        if not rows:
            return _warn("No forecast data available.")
        return _table(pd.DataFrame(rows))
    except Exception as ex:
        return _error(f"Weather error: {ex}")


@app.callback(Output("menu-content", "children"), Input("int-slow", "n_intervals"))
def update_menu(_):
    try:
        return _table(get_weekly_menu())
    except Exception as ex:
        return _error(f"Menu error: {ex}")


@app.callback(Output("ingredient", "options"), Input("int-slow", "n_intervals"))
def update_ingredient_options(_):
    try:
        return [{"label": i, "value": i} for i in get_ingredients()]
    except Exception:
        return []


@app.callback(Output("recipe-content", "children"), Input("ingredient", "value"))
def update_recipes(ingredient):
    if not ingredient:
        return html.Div()
    try:
        matches = get_recipes_for_ingredient(ingredient)
        if not matches:
            return dbc.Alert(
                f"No recipes found for '{ingredient}'.", color="info", className="mb-0"
            )
        return dbc.ListGroup(
            [
                dbc.ListGroupItem(
                    [
                        html.Strong(recipe),
                        html.Span(f" — {amount}", className="text-muted"),
                    ],
                    style=ROW_STYLE,
                )
                for recipe, amount in matches.items()
            ],
            flush=True,
        )
    except Exception as ex:
        return _error(f"Recipe error: {ex}")


@app.callback(Output("schedule-content", "children"), Input("int-slow", "n_intervals"))
def update_schedule(_):
    try:
        df = get_schedule_summary()
        caption = None
        if "Monday" in df.columns and not df.empty:
            caption = html.Small(
                f"Week starting: {df.iloc[-1]['Monday']}",
                className="text-muted d-block mb-2",
            )
            df = df.iloc[:-1].reset_index(drop=True)
        return html.Div([caption, _table(df)] if caption else [_table(df)])
    except Exception as ex:
        return _error(f"Schedule error: {ex}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False)
