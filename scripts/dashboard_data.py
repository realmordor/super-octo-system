"""Data for the dashboard (scripts/dash_app.py): Google Calendar, Met Office weather,
live trains from the Darwin Push Port, and the weekly menu and schedule sheets."""

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

import pandas as pd
import paramiko
import pytz
import requests
import stomp
from cachetools import TTLCache
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
_DARWIN_PP_NS7 = "http://www.thalesgroup.com/rtti/PushPort/StationMessages/v1"
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
#         "arrs": {tiploc: {"pta", "sta"}},
#         "fc": {tiploc: {"et", "at", "delayed", "plat", "arr_et", "arr_at"}}}
_services: dict[str, dict] = {}
# message id -> {"crs": [crs, ...], "sev": int, "text": str}, for tracked stations only
_station_messages: dict[str, dict] = {}
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
    ref, dest, calls, arrs = None, None, {}, {}
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
        if tpl in _TRACKED_TIPLOCS and tag in ("IP", "DT") and loc.get("pta"):
            arrs[tpl] = {"pta": loc.get("pta"), "sta": _nearest(ref, loc.get("pta"))}
    if not calls:
        services.pop(rid, None)
        return
    services.setdefault(rid, {"fc": {}}).update(dest=dest, calls=calls, arrs=arrs)


def _apply_ts(msg: ET.Element, services: dict) -> None:
    for loc in msg.findall(f"{{{_DARWIN_PP_NS5}}}Location"):
        if loc.get("tpl") not in _TRACKED_TIPLOCS or not (loc.get("wtd") or loc.get("wta")):
            continue
        svc = services.setdefault(msg.get("rid"), {"dest": None, "calls": {}, "fc": {}})
        fc = svc["fc"].setdefault(loc.get("tpl"), {})
        if loc.get("wtd"):
            dep = loc.find(f"{{{_DARWIN_PP_NS5}}}dep")
            if dep is not None:
                fc["et"] = dep.get("et")
                fc["at"] = None if dep.get("atRemoved") == "true" else dep.get("at")
                fc["delayed"] = "true" in (dep.get("delayed"), dep.get("etUnknown"))
            plat = loc.find(f"{{{_DARWIN_PP_NS5}}}plat")
            if plat is not None:
                suppressed = "true" in (plat.get("platsup"), plat.get("cisPlatsup"))
                fc["plat"] = None if suppressed else plat.text
        arr = loc.find(f"{{{_DARWIN_PP_NS5}}}arr")
        if arr is not None:
            fc["arr_et"] = arr.get("et")
            fc["arr_at"] = None if arr.get("atRemoved") == "true" else arr.get("at")


def _apply_station_message(msg: ET.Element, messages: dict) -> None:
    """Keep station messages (disruption notices etc.) for tracked stations. A message
    sent again without stations or text has been withdrawn."""
    stations = sorted(
        s.get("crs") for s in msg.findall(f"{{{_DARWIN_PP_NS7}}}Station") if s.get("crs") in CRS_CODES
    )
    body = msg.find(f"{{{_DARWIN_PP_NS7}}}Msg")
    text = " ".join("".join(body.itertext()).split()) if body is not None else ""
    if stations and text and msg.get("suppress") != "true":
        messages[msg.get("id")] = {"crs": stations, "sev": int(msg.get("sev") or 0), "text": text}
    else:
        messages.pop(msg.get("id"), None)


def _apply_pushport(xml: bytes, services: dict, messages: dict | None = None) -> None:
    """Fold one Push Port document into `services` (and station messages into
    `messages`, by default the module's _station_messages)."""
    messages = _station_messages if messages is None else messages
    if not _TRACKED_TPL_RE.search(xml) and b"<deactivated" not in xml and b"<OW " not in xml:
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
            elif tag == "OW":
                _apply_station_message(msg, messages)


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
    _station_messages.clear()  # the snapshot has every current message
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
