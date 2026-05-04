"""
Run this script whenever the Google Calendar token expires:

    uv run python scripts/refresh_google_token.py

It will open a browser window to re-authorise, then save a fresh token.json.
"""

import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


def refresh_token():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        print("Token is still valid — no action needed.")
        return

    if creds and creds.expired and creds.refresh_token:
        print("Attempting silent refresh...")
        try:
            creds.refresh(Request())
            print("Token refreshed silently.")
        except Exception as e:
            print(f"Silent refresh failed ({e}), re-authorising via browser...")
            creds = None

    if not creds:
        if not os.path.exists(CREDENTIALS_FILE):
            raise FileNotFoundError(
                f"{CREDENTIALS_FILE} not found. Download it from Google Cloud Console "
                "(APIs & Services → Credentials → OAuth 2.0 Client) and place it in "
                "the project root."
            )
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
        print("Authorisation complete.")

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"Token saved to {TOKEN_FILE}.")


if __name__ == "__main__":
    refresh_token()
