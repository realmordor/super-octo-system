import datetime
import os
import urllib.parse

import pandas as pd
import pytz
import requests
import streamlit as st
from dateutil import parser as dt_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from streamlit_autorefresh import st_autorefresh
from streamlit_calendar import calendar as st_calendar
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
INGREDIENTS_DROPDOWN_GID = "960508758"
RAIL_WSDL = "http://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"

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

# ── Data fetching ─────────────────────────────────────────────────────────────


def _sheet_url(
    sheet_id: str, *, name: str | None = None, gid: str | None = None
) -> str:
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if name:
        return f"{base}&sheet={urllib.parse.quote(name)}"
    return f"{base}&gid={gid}"


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
    return [
        {
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "summary": e.get("summary", "No Title"),
        }
        for e in items
    ]


def get_weather(lat: float, lon: float) -> dict:
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
    return resp.json()


def get_departures(
    crs: str, token: str, num_rows: int = 10
) -> tuple[str, datetime.datetime, list]:
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
    return res.locationName, res.generatedAt, res.trainServices.service


def get_weekly_menu() -> pd.DataFrame:
    df = pd.read_csv(_sheet_url(RECIPE_SHEET_ID, name="thisWeekMenuMaker"))
    return df.iloc[3:].dropna(axis=1, thresh=1).reset_index(drop=True)


def get_schedule_summary() -> pd.DataFrame:
    df = pd.read_csv(_sheet_url(SCHEDULE_SHEET_ID, name="Form Responses 1"))
    if df.empty:
        return pd.DataFrame()
    latest = df.iloc[-1]
    days = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    collated: dict = {}
    for col in df.columns:
        for day in days:
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


def get_menu_recipe_names() -> set[str]:
    df = pd.read_csv(_sheet_url(RECIPE_SHEET_ID, name="thisWeekMenuMaker"))
    final = df[df.iloc[:, 0] == "final"]
    if final.empty:
        return set()
    return {str(v).strip() for v in final.iloc[0, 1:] if pd.notna(v) and str(v).strip()}


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
            amount = next(
                (
                    str(row[c]).strip()
                    for c in df.columns
                    if "amount" in str(c).lower() and pd.notna(row[c])
                ),
                "",
            )
            unit = next(
                (
                    str(row[c]).strip()
                    for c in df.columns
                    if "unit" in str(c).lower() and pd.notna(row[c])
                ),
                "",
            )
            matches[current] = f"{amount} {unit}".strip() or "Amount not specified"
    return matches


# ── UI sections ───────────────────────────────────────────────────────────────


def render_calendar():
    st.subheader("Google Calendar")
    try:
        events = get_upcoming_events()
        if not events:
            st.write("No upcoming events.")
            return
        cal_events = []
        for e in events:
            try:
                start = dt_parser.isoparse(e["start"]).strftime("%Y-%m-%d %H:%M")
            except Exception:
                start = e["start"]
            cal_events.append({"title": e["summary"], "start": start, "end": start})
        st_calendar(
            events=cal_events,
            options={
                "editable": False,
                "selectable": False,
                "headerToolbar": {
                    "left": "prev,next today",
                    "center": "title",
                    "right": "dayGridMonth,timeGridWeek,timeGridDay",
                },
                "initialView": "listWeek",
                "eventTimeFormat": {
                    "hour": "2-digit",
                    "minute": "2-digit",
                    "hour12": False,
                },
            },
            custom_css=".fc-event-title { font-size: 1em; }",
        )
    except Exception as e:
        st.error(f"Calendar error: {e}")


def render_weather():
    st.subheader("Met Office Forecast – Hadley Wood")
    try:
        forecast = get_weather(*HADLEY_WOOD)
        if not (forecast.get("features") and forecast["features"]):
            st.write(forecast)
            return
        london_tz = pytz.timezone("Europe/London")
        now = datetime.datetime.now(london_tz)
        today = now.date()
        tomorrow = today + datetime.timedelta(days=1)
        rows: dict = {}
        for entry in forecast["features"][0]["properties"].get("timeSeries", []):
            local = dt_parser.isoparse(entry["time"]).astimezone(london_tz)
            if local.date() not in (today, tomorrow):
                continue
            temp = entry.get("feelsLikeTemperature")
            precip = entry.get("probOfPrecipitation")
            t = round(temp) if temp is not None else None
            t_emoji = (
                "🥶"
                if (t is not None and t <= 5)
                else "🥵"
                if (t is not None and t >= 25)
                else "😊"
            )
            r_emoji = (
                "🌧️"
                if (precip is not None and precip >= 70)
                else "🌦️"
                if (precip is not None and precip >= 30)
                else "☀️"
            )
            label = (
                local.strftime("%H:%M")
                if local.date() == today
                else local.strftime("T+1 %H:%M")
            )
            rows[label] = {
                "🌡️ Feels Like (°C)": f"{t} {t_emoji}",
                "🌧️ Precip (%)": f"{precip} {r_emoji}",
            }
        if rows:
            st.dataframe(
                pd.DataFrame(rows).T,
                width="stretch",
                column_config={
                    "🌡️ Feels Like (°C)": st.column_config.TextColumn(width="medium"),
                    "🌧️ Precip (%)": st.column_config.TextColumn(width="medium"),
                },
            )
        else:
            st.write("No forecast data available.")
    except Exception as e:
        st.error(f"Weather error: {e}")


def render_trains():
    st.subheader("National Rail Departure Board")
    token = os.environ.get("DARWIN_LITE_TOKEN")
    if not token:
        st.error("DARWIN_LITE_TOKEN not set.")
        return
    col1, col2 = st.columns(2)
    with col1:
        depart = st.selectbox("Departure", list(CRS_CODES), format_func=CRS_CODES.get)
    with col2:
        dest = st.selectbox(
            "Destination", list(CRS_CODES), format_func=CRS_CODES.get, index=1
        )

    cache_key = f"trains_{depart}_{dest}"
    if cache_key not in st.session_state or st.button("Refresh"):
        try:
            location, generated, services = get_departures(depart, token)
            st.session_state[cache_key] = (location, generated, services)
        except Exception as e:
            st.error(f"Rail API error: {e}")
            return

    location, generated, services = st.session_state[cache_key]
    if not services:
        st.warning("No services found.")
        return
    data = [
        {
            "Time": t.std,
            "Destination": t.destination.location[0].locationName,
            "Platform": t.platform or "",
            "ETD": t.etd,
        }
        for t in services
        if getattr(t.destination.location[0], "crs", "") == dest
    ]
    if not data:
        st.warning("No trains to the selected destination.")
    else:
        st.caption(f"From {location} as of {generated}")
        st.dataframe(pd.DataFrame(data))


def render_menu():
    st.subheader("This Week's Menu")
    try:
        st.dataframe(get_weekly_menu())
    except Exception as e:
        st.error(f"Menu error: {e}")


def render_schedule():
    st.subheader("Household Schedule")
    try:
        df = get_schedule_summary()
        if "Monday" in df.columns and not df.empty:
            st.caption(f"Week starting: {df.iloc[-1]['Monday']}")
            df = df.iloc[:-1].reset_index(drop=True)
        st.dataframe(df)
    except Exception as e:
        st.error(f"Schedule error: {e}")


def render_recipe_finder():
    st.subheader("Recipe Finder")
    try:
        ingredients = get_ingredients()
    except Exception as e:
        st.error(f"Could not load ingredients: {e}")
        return
    if not ingredients:
        st.warning("No ingredients found.")
        return
    selected = st.selectbox("Find recipes using:", ["— select —"] + ingredients)
    if selected == "— select —":
        return
    try:
        matches = get_recipes_for_ingredient(selected)
        if matches:
            st.success(f"{len(matches)} recipe(s) containing '{selected}':")
            for recipe, amount in matches.items():
                st.write(f"• **{recipe}**: {amount}")
        else:
            st.info(f"No recipes found for '{selected}'.")
    except Exception as e:
        st.error(f"Recipe search error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    st.set_page_config(layout="wide", page_title="Super Octo System")
    st_autorefresh(interval=60_000, key="data_refresh")
    st.title("Super Octo System Dashboard")

    col_left, col_right = st.columns(2)
    with col_left:
        render_calendar()
    with col_right:
        render_weather()

    st.markdown("---")
    col_left, col_right = st.columns(2)
    with col_left:
        render_trains()
    with col_right:
        render_menu()

    st.markdown("---")
    render_schedule()

    st.markdown("---")
    render_recipe_finder()


if __name__ == "__main__":
    main()
