import datetime
import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def get_upcoming_events(max_results=10):
    """
    Authenticate and fetch upcoming Google Calendar events.
    Returns a list of dicts with 'start' and 'summary'.
    """
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    service = build("calendar", "v3", credentials=creds)
    now = datetime.datetime.utcnow().isoformat() + "Z"
    # Find the calendarId for '29ClaremontRoad'
    calendar_list = service.calendarList().list().execute()
    calendar_id = None
    for cal in calendar_list.get("items", []):
        if cal.get("summary") == "29ClaremontRoad":
            calendar_id = cal.get("id")
            break
    if not calendar_id:
        raise ValueError("Calendar '29ClaremontRoad' not found in your account.")
    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])
    event_list = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        event_list.append({"start": start, "summary": event.get("summary", "No Title")})
    return event_list
