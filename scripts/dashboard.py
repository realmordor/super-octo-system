import os
import streamlit as st
import pandas as pd
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import datetime
from super_octo_system.train import get_departure_board, crs_codes

# If modifying these SCOPES, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def get_calendar_events():
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first time.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    service = build("calendar", "v3", credentials=creds)
    # Call the Calendar API
    now = datetime.datetime.utcnow().isoformat() + "Z"  # 'Z' indicates UTC time
    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])
    return events


def main():
    st.title("Super Octo System Dashboard")
    # Top row: Calendar (left), Weather (right)
    top_left, top_right = st.columns(2)
    with top_left:
        st.subheader("Google Calendar Events")
        st.write("Your events in a calendar view:")
        try:
            st.set_page_config(layout="wide")
            from super_octo_system.google_calendar import get_upcoming_events
            from streamlit_calendar import calendar

            events = get_upcoming_events(max_results=30)
            if not events:
                st.write("No upcoming events found.")
            else:
                calendar_events = []
                for event in events:
                    start = event["start"]
                    title = event["summary"]
                    calendar_events.append(
                        {"title": title, "start": start, "end": start}
                    )
                calendar(
                    events=calendar_events,
                    options={
                        "editable": False,
                        "selectable": False,
                        "headerToolbar": {
                            "left": "prev,next today",
                            "center": "title",
                            "right": "dayGridMonth,timeGridWeek,timeGridDay",
                        },
                        "initialView": "dayGridMonth",
                    },
                    custom_css=".fc-event-title { font-size: 1em; }",
                )
        except Exception as e:
            st.error(f"Google Calendar API error: {e}")

    with top_right:
        st.subheader("Met Office Forecast for Hadley Wood")
        try:
            from super_octo_system.met_office import get_met_office_forecast
            import pytz
            from dateutil import parser

            hw_lat = 51.6512
            hw_lon = -0.1442
            forecast = get_met_office_forecast(hw_lat, hw_lon)
            st.write("Feels Like & Precipitation Table:")
            time_series = []
            if "features" in forecast and forecast["features"]:
                props = forecast["features"][0]["properties"]
                london_tz = pytz.timezone("Europe/London")
                today = datetime.datetime.now(london_tz).date()
                for entry in props.get("timeSeries", []):
                    utc_time = parser.isoparse(entry.get("time"))
                    local_time = utc_time.astimezone(london_tz)
                    if local_time.date() != today:
                        continue
                    temp = entry.get("feelsLikeTemperature")
                    precip = entry.get("probOfPrecipitation")
                    temp_rounded = round(temp) if temp is not None else None
                    if precip is not None and precip >= 70:
                        precip_emoji = "🌧️"
                    elif precip is not None and precip >= 30:
                        precip_emoji = "🌦️"
                    else:
                        precip_emoji = "☀️"
                    if temp_rounded is not None and temp_rounded <= 5:
                        temp_emoji = "🥶"
                    elif temp_rounded is not None and temp_rounded >= 25:
                        temp_emoji = "🥵"
                    else:
                        temp_emoji = "😊"
                    time_series.append(
                        {
                            local_time.strftime("%H:%M"): {
                                "🌡️ Feels Like Temp (°C)": f"{temp_rounded} {temp_emoji}",
                                "🌧️ Probability of Precipitation (%)": f"{precip} {precip_emoji}",
                            }
                        }
                    )
                if time_series:
                    columns = {}
                    for entry in time_series:
                        for k, v in entry.items():
                            columns[k] = v
                    df_weather = pd.DataFrame(columns)
                    st.dataframe(df_weather, width="stretch")
                else:
                    st.write("No timeSeries data available.")
            else:
                st.write(forecast)
        except Exception as e:
            st.error(f"Met Office API error: {e}")

    # Bottom row: Train board (full width)
    st.markdown("---")
    train_container = st.container()
    with train_container:
        st.subheader("National Rail Departure Board Dashboard")
        token = os.environ.get("DARWIN_LITE_TOKEN")
        if not token:
            st.error("DARWIN_LITE_TOKEN not set in environment.")
            return
        col_train1, col_train2 = st.columns(2)
        with col_train1:
            crs_code = st.selectbox(
                "Select Departure Station",
                options=list(crs_codes.keys()),
                format_func=lambda x: crs_codes[x],
            )
        with col_train2:
            dest_crs_code = st.selectbox(
                "Select Destination Station",
                options=list(crs_codes.keys()),
                format_func=lambda x: crs_codes[x],
                index=1,
            )
        generated = datetime.datetime.min
        if st.button("Get Departure Board") or (
            datetime.datetime.now() - generated > datetime.timedelta(seconds=5)
        ):
            location_name, generated, services = get_departure_board(
                crs_code, token, 10, None
            )
            if not services:
                st.warning("No train services found or error occurred.")
                return
            data = []
            for t in services:
                # Filter by destination CRS code if selected
                dest_code = getattr(t.destination.location[0], "crs", "")
                if dest_crs_code and dest_code != dest_crs_code:
                    continue
                data.append(
                    {
                        "Time": getattr(t, "std", ""),
                        "Destination": getattr(
                            t.destination.location[0], "locationName", ""
                        ),
                        "Platform": getattr(t, "platform", ""),
                        "ETD": getattr(t, "etd", ""),
                    }
                )
            if not data:
                st.warning("No train services match the destination CRS code filter.")
                return
            df = pd.DataFrame(data)
            st.subheader(f"Trains from {location_name} (as of {generated})")
            st.dataframe(df)


if __name__ == "__main__":
    main()
