import contextlib
import datetime
import gzip
import io
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
import zlib
from xml.etree import ElementTree as ET

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import paramiko
import pytz
import requests
import stomp
from cachetools import TTLCache
from dash import MATCH, Input, Output, State, clientside_callback, dcc, html
from dateutil import parser as dt_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
HADLEY_WOOD = (51.6512, -0.1442)
RECIPE_SHEET_ID = "1qMt1jKFf3OVILmA-MsQ8Ga-8vsYLsCX0ky00zairf9M"
SCHEDULE_SHEET_ID = "1AFQrHf15-Pzyvbmn9jzPU1FvXnW4u9VllsIWAQ0Mq6U"
RECIPE_INGREDIENTS_GID = "802866866"
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

TIPLOC_FROM_CRS = {
    "HDW": "HADLYWD", "MOG": "MRGT",    "OLD": "OLDST",   "EXR": "ESSEXRD",
    "HHY": "HGHI",    "DYP": "DRYP",    "FPK": "FNPK",    "HGY": "HRGY",
    "HRN": "HRNSY",   "AAP": "ALEXNDP", "NSG": "NEWSGAT", "OKL": "OKLGHPK",
    "NBA": "NBARNET",  "PBR": "POTRSBR", "BPK": "BRKMNPK", "WMG": "WELHAMG",
    "HAT": "HATFILD",  "WGC": "WLWYNGC",
}
TIPLOC_TO_NAME = {t: CRS_CODES[c] for c, t in TIPLOC_FROM_CRS.items()}

_DARWIN_HOST = "darwin-dist-44ae45.nationalrail.co.uk"
_DARWIN_SFTP_PORT = 2222
_DARWIN_SFTP_USER = "sftpuser"
_DARWIN_STOMP_PORT = 61613
_DARWIN_PP_NS5 = "http://www.thalesgroup.com/rtti/PushPort/Forecasts/v3"
_DARWIN_TOPIC = "/topic/darwin.pushport-v16"
_DARWIN_SNAPSHOT = "snapshot/snapshot.gz"
_DARWIN_REQUIRED_ENV = ("DARWIN_SFTP_PASS", "DARWIN_STOMP_USER", "DARWIN_STOMP_PASS")
_TRACKED_TIPLOCS = frozenset(TIPLOC_FROM_CRS.values())
_TRACKED_TPL_RE = re.compile(
    rb'tpl="(?:' + b"|".join(t.encode() for t in _TRACKED_TIPLOCS) + rb')"'
)

# ── Caching ────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_weather_cache: TTLCache = TTLCache(maxsize=4, ttl=1800)
_menu_cache: TTLCache = TTLCache(maxsize=1, ttl=300)
_calendar_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

# ── Darwin live state ─────────────────────────────────────────────────────────

log = logging.getLogger("darwin")

# rid -> {"dest": tiploc, "calls": {tiploc: {"ptd", "std", "can"}},
#         "fc": {tiploc: {"et", "at", "delayed", "plat"}}}
_services: dict[str, dict] = {}
_darwin_lock = threading.Lock()
_darwin_status: dict = {
    "ready": False,
    "connected": False,
    "last_message": None,
    "error": None,
}

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


_LONDON = pytz.timezone("Europe/London")


def _to_london(dt_str: str) -> str:
    if not dt_str or "T" not in dt_str:
        return dt_str  # all-day date string, pass through unchanged
    dt = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt.astimezone(_LONDON).strftime("%Y-%m-%dT%H:%M:%S")


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
            "start": _to_london(e["start"].get("dateTime", e["start"].get("date"))),
            "end": _to_london(e["end"].get("dateTime", e["end"].get("date"))),
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


# ── Darwin live departures ────────────────────────────────────────────────────
#
# The Push Port stream only carries changes, so on every (re)connect the state is
# rebuilt from the SFTP snapshot plus the pushport logs written since, then kept
# current from the stream.


def _now_london() -> datetime.datetime:
    return datetime.datetime.now(_LONDON).replace(tzinfo=None)


def _nearest(ref: datetime.datetime, hhmm: str) -> datetime.datetime:
    """The datetime at clock time `hhmm` closest to `ref`, so times crossing midnight land on the right day."""
    t = ref.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0)
    if t - ref > datetime.timedelta(hours=12):
        return t - datetime.timedelta(days=1)
    if ref - t > datetime.timedelta(hours=12):
        return t + datetime.timedelta(days=1)
    return t


def _apply_schedule(msg: ET.Element, services: dict) -> None:
    rid = msg.get("rid")
    if msg.get("deleted") == "true":
        services.pop(rid, None)
        return
    ref, dest, calls = None, None, {}
    for loc in msg:
        working = loc.get("wtd") or loc.get("wta") or loc.get("wtp")
        if not working:
            continue
        ref = (
            _nearest(ref, working)
            if ref
            else datetime.datetime.fromisoformat(f"{msg.get('ssd')}T{working[:5]}")
        )
        tag, tpl = loc.tag.split("}")[-1], loc.get("tpl")
        if tag == "DT":
            dest = tpl
        if tpl in _TRACKED_TIPLOCS and tag in ("OR", "IP") and loc.get("ptd"):
            calls[tpl] = {
                "ptd": loc.get("ptd"),
                "std": _nearest(ref, loc.get("ptd")),
                "can": loc.get("can") == "true",
            }
    if not calls:
        services.pop(rid, None)
        return
    services.setdefault(rid, {"fc": {}}).update(dest=dest, calls=calls)


def _apply_ts(msg: ET.Element, services: dict) -> None:
    for loc in msg.findall(f"{{{_DARWIN_PP_NS5}}}Location"):
        if loc.get("tpl") not in _TRACKED_TIPLOCS or not loc.get("wtd"):
            continue
        svc = services.setdefault(msg.get("rid"), {"dest": None, "calls": {}, "fc": {}})
        fc = svc["fc"].setdefault(loc.get("tpl"), {})
        dep = loc.find(f"{{{_DARWIN_PP_NS5}}}dep")
        if dep is not None:
            fc["et"] = dep.get("et")
            fc["at"] = None if dep.get("atRemoved") == "true" else dep.get("at")
            fc["delayed"] = "true" in (dep.get("delayed"), dep.get("etUnknown"))
        plat = loc.find(f"{{{_DARWIN_PP_NS5}}}plat")
        if plat is not None:
            suppressed = "true" in (plat.get("platsup"), plat.get("cisPlatsup"))
            fc["plat"] = None if suppressed else plat.text


def _apply_pushport(xml: bytes, services: dict) -> None:
    """Fold one Push Port document into `services`."""
    if not _TRACKED_TPL_RE.search(xml) and b"<deactivated" not in xml:
        return
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return
    for response in root:  # sR in the snapshot, uR in logs and the stream
        for msg in response:
            tag = msg.tag.split("}")[-1]
            if tag == "schedule":
                _apply_schedule(msg, services)
            elif tag == "TS":
                _apply_ts(msg, services)
            elif tag == "deactivated":
                services.pop(msg.get("rid"), None)


def _prune_services(services: dict, now: datetime.datetime) -> None:
    cutoff = now - datetime.timedelta(hours=3)
    stale = [
        rid
        for rid, svc in services.items()
        if not svc["calls"] or max(c["std"] for c in svc["calls"].values()) < cutoff
    ]
    for rid in stale:
        del services[rid]


@contextlib.contextmanager
def _darwin_sftp():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        _DARWIN_HOST,
        port=_DARWIN_SFTP_PORT,
        username=_DARWIN_SFTP_USER,
        password=os.environ["DARWIN_SFTP_PASS"],
        timeout=30,
    )
    try:
        yield ssh.open_sftp()
    finally:
        ssh.close()


def _read_gz_lines(sftp: paramiko.SFTPClient, path: str) -> list[bytes]:
    buf = io.BytesIO()
    sftp.getfo(path, buf)
    return gzip.decompress(buf.getvalue()).splitlines()


def _load_darwin_state() -> dict:
    """Build state from the snapshot and the pushport logs written after it."""
    services: dict = {}
    with _darwin_sftp() as sftp:
        snapshot_mtime = sftp.stat(_DARWIN_SNAPSHOT).st_mtime
        logs = sorted(
            f"pushport/{a.filename}"
            for a in sftp.listdir_attr("pushport")
            if a.filename.endswith(".gz") and a.st_mtime >= snapshot_mtime
        )
        for path in [_DARWIN_SNAPSHOT, *logs]:
            for line in _read_gz_lines(sftp, path):
                _apply_pushport(line, services)
    return services


class _DarwinListener(stomp.ConnectionListener):
    def __init__(self, inbox: queue.Queue):
        self.inbox = inbox
        self.error: str | None = None

    def on_message(self, frame):
        self.inbox.put(frame.body)

    def on_error(self, frame):
        self.error = frame.headers.get("message") or "Darwin stream error"
        log.error("Darwin stream error: %s", self.error)


def _connect_stomp(listener: _DarwinListener) -> stomp.Connection12:
    conn = stomp.Connection12(
        [(_DARWIN_HOST, _DARWIN_STOMP_PORT)],
        heartbeats=(15_000, 15_000),
        auto_decode=False,
    )
    conn.set_listener("darwin", listener)
    try:
        conn.connect(
            username=os.environ["DARWIN_STOMP_USER"],
            passcode=os.environ["DARWIN_STOMP_PASS"],
            wait=True,
        )
    except stomp.exception.ConnectFailedException:
        raise ConnectionError(listener.error or "Could not connect to Darwin stream") from None
    conn.subscribe(_DARWIN_TOPIC, id="1", ack="auto")
    return conn


def _follow_darwin_feed() -> None:
    global _services
    inbox: queue.Queue[bytes] = queue.Queue()
    # Subscribe before loading so messages sent during the SFTP download are queued
    conn = _connect_stomp(_DarwinListener(inbox))
    _darwin_status.update(connected=True, error=None)
    try:
        log.info("Connected to Darwin stream; loading state from SFTP")
        services = _load_darwin_state()
        with _darwin_lock:
            _services = services
        _darwin_status["ready"] = True
        log.info("Darwin state loaded (%d services)", len(services))
        next_prune = time.time()
        while conn.is_connected():
            try:
                xml = zlib.decompress(inbox.get(timeout=5), zlib.MAX_WBITS | 32)
            except queue.Empty:
                pass
            else:
                with _darwin_lock:
                    _apply_pushport(xml, services)
                _darwin_status["last_message"] = datetime.datetime.now(_LONDON)
            if time.time() >= next_prune:
                next_prune = time.time() + 600
                with _darwin_lock:
                    _prune_services(services, _now_london())
    finally:
        _darwin_status["connected"] = False
        if conn.is_connected():
            conn.disconnect()


def _run_darwin_feed() -> None:
    while True:
        try:
            _follow_darwin_feed()
            log.warning("Darwin stream disconnected")
        except Exception as ex:
            log.exception("Darwin feed failed")
            _darwin_status["error"] = str(ex) or type(ex).__name__
        time.sleep(30)


def _departures_from(
    services: dict, depart_tpl: str, dest_tpl: str | None, now: datetime.datetime
) -> list[dict]:
    rows = []
    for svc in services.values():
        call = svc["calls"].get(depart_tpl)
        if not call or (dest_tpl and svc["dest"] != dest_tpl):
            continue
        fc = svc["fc"].get(depart_tpl, {})
        if fc.get("at"):
            continue  # already departed
        std = call["std"]
        etd = _nearest(std, fc["et"]) if fc.get("et") else std
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
        rows.append(
            (
                std,
                {
                    "Time": call["ptd"],
                    "Destination": TIPLOC_TO_NAME.get(svc["dest"], svc["dest"] or "—"),
                    "Platform": fc.get("plat") or "—",
                    "ETD": status,
                },
            )
        )
    rows.sort(key=lambda r: r[0])
    return [row for _, row in rows]


def get_departures(depart_crs: str, dest_crs: str | None = None) -> tuple[str, list[dict]]:
    with _darwin_lock:
        departures = _departures_from(
            _services,
            TIPLOC_FROM_CRS.get(depart_crs, depart_crs),
            TIPLOC_FROM_CRS.get(dest_crs) if dest_crs else None,
            _now_london(),
        )
    return CRS_CODES.get(depart_crs, depart_crs), departures


def _load_menu_sheet() -> pd.DataFrame:
    with _lock:
        if "menu" in _menu_cache:
            return _menu_cache["menu"]
    df = pd.read_csv(_sheet_url(RECIPE_SHEET_ID, name="thisWeekMenuMaker"))
    with _lock:
        _menu_cache["menu"] = df
    return df


_MENU_ROW_ORDER = ["final", "recipeBook", "pageNumber", "sideSalad", "notes"]
_MENU_ROWS_REMOVE = {"prepTime", "cookTime", "sourceShortName", "serves"}


def get_weekly_menu() -> pd.DataFrame:
    df = _load_menu_sheet()
    menu = df.iloc[3:].dropna(axis=1, thresh=1).reset_index(drop=True)
    final_rows = df[df.iloc[:, 0] == "final"]
    if not final_rows.empty:
        final = final_rows.iloc[0]
        menu.columns = [
            str(final[col]).strip() if col in final.index and pd.notna(final[col]) and str(final[col]).strip() else col
            for col in menu.columns
        ]
    label_col = menu.columns[0]
    menu = menu[~menu[label_col].isin(_MENU_ROWS_REMOVE)]
    order = {label: i for i, label in enumerate(_MENU_ROW_ORDER)}
    menu = menu.sort_values(
        by=label_col,
        key=lambda s: s.map(lambda x: order.get(x, len(_MENU_ROW_ORDER))),
        kind="stable",
    ).reset_index(drop=True)
    return menu


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


TABLE_STYLE = {
    "striped": True,
    "hover": True,
    "responsive": True,
    "style": {"fontSize": "1.05rem", "marginBottom": 0},
}
TRAIN_COLUMNS = ["Time", "Destination", "Platform", "ETD"]


def _table(df: pd.DataFrame, index: bool = False) -> dbc.Table:
    return dbc.Table.from_dataframe(df, index=index, index_label="", **TABLE_STYLE)


def _trains_table(departures: list[dict], now: datetime.datetime) -> dbc.Table:
    """The next train is orange if it leaves within 10 minutes; delays and cancellations are red."""
    rows = []
    for i, train in enumerate(departures):
        etd = train["ETD"]
        leaves = _nearest(now, etd if re.fullmatch(r"\d\d:\d\d", etd) else train["Time"])
        leaving_soon = i == 0 and leaves - now <= datetime.timedelta(minutes=10)
        cells = []
        for col in TRAIN_COLUMNS:
            if col == "ETD" and etd != "On time":
                color = "var(--bs-danger)"
            elif leaving_soon:
                color = "var(--bs-warning)"
            else:
                color = None
            cells.append(html.Td(train[col], style={"color": color}) if color else html.Td(train[col]))
        rows.append(html.Tr(cells))
    return dbc.Table(
        [html.Thead(html.Tr([html.Th(c) for c in TRAIN_COLUMNS])), html.Tbody(rows)],
        **TABLE_STYLE,
    )


def _error(msg: str) -> dbc.Alert:
    return dbc.Alert(msg, color="danger", className="mb-0")


def _warn(msg: str) -> dbc.Alert:
    return dbc.Alert(msg, color="warning", className="mb-0")


# ── App ───────────────────────────────────────────────────────────────────────

SERVER_STARTED = str(time.time_ns())

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
        dcc.Interval(id="int-trains", interval=15_000),
        dcc.Interval(id="int-weather", interval=1_800_000),
        dcc.Interval(id="int-slow", interval=300_000),
        dcc.Store(id="calendar-store"),
        html.Div(id="fc-dummy", style={"display": "none"}),
        dcc.Interval(id="int-server", interval=10_000),
        dcc.Store(id="server-started-latest"),
        dcc.Store(id="server-started-seen"),
        # ── Header ────────────────────────────────────────────────────────────────
        html.Div(
            style={
                "position": "sticky",
                "top": "0",
                "zIndex": "1000",
                "backgroundColor": "var(--bs-body-bg)",
                "paddingTop": "0.75rem",
                "paddingBottom": "0.75rem",
            },
            children=dbc.Row(
                className="align-items-end",
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


# Reload open pages when the dashboard restarts, so they pick up the new version
@app.callback(Output("server-started-latest", "data"), Input("int-server", "n_intervals"))
def report_server_started(_):
    return SERVER_STARTED


clientside_callback(
    """function(latest, seen) {
        if (seen && latest && latest !== seen) {
            window.location.reload();
        }
        return latest || seen;
    }""",
    Output("server-started-seen", "data"),
    Input("server-started-latest", "data"),
    State("server-started-seen", "data"),
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
            timeZone: "local",
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
    Output("int-trains", "interval"),
    Input("int-trains", "n_intervals"),
    State("int-trains", "interval"),
)
def update_trains_interval(_, current):
    """Check every second while live train data loads at startup, then every 15 seconds."""
    loading = (
        not _darwin_status["ready"]
        and not _darwin_status["error"]
        and all(os.environ.get(k) for k in _DARWIN_REQUIRED_ENV)
    )
    interval = 1_000 if loading else 15_000
    return dash.no_update if interval == current else interval


@app.callback(
    Output("trains-content", "children"),
    Input("int-trains", "n_intervals"),
    Input("btn-refresh-trains", "n_clicks"),
    Input("depart", "value"),
    Input("dest", "value"),
)
def update_trains(_, __, depart, dest):
    missing = [k for k in _DARWIN_REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        return _error(f"{', '.join(missing)} not set.")
    status = dict(_darwin_status)
    if not status["ready"]:
        if status["error"]:
            return _error(f"Rail feed error: {status['error']}")
        return dbc.Alert("Loading live train data…", color="info", className="mb-0")
    location, departures = get_departures(depart, dest)
    last = status["last_message"]
    as_of = last.strftime("%H:%M:%S") if last else "—"
    children = []
    if not status["connected"]:
        children.append(
            _warn(f"Live feed disconnected, reconnecting. Showing data as of {as_of}.")
        )
    elif not last or datetime.datetime.now(_LONDON) - last > datetime.timedelta(minutes=2):
        children.append(_warn(f"No live updates since {as_of}."))
    if not departures:
        children.append(_warn("No trains to the selected destination."))
        return html.Div(children)
    children += [
        html.Small(
            f"From {location} · live, last update {as_of}",
            className="text-muted d-block mb-2",
        ),
        _trains_table(departures[:5], _now_london()),
    ]
    return html.Div(children)


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
        return _table(pd.DataFrame(rows), index=True)
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
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    if all(os.environ.get(k) for k in _DARWIN_REQUIRED_ENV):
        threading.Thread(target=_run_darwin_feed, name="darwin-feed", daemon=True).start()
    app.run(host="0.0.0.0", port=8050, debug=False)
