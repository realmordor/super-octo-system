# super-octo-system

A personal home dashboard built with Dash. Displays Google Calendar events, Met Office weather, live National Rail departures, a weekly menu, household schedule, and a recipe finder — all in one page, refreshing automatically.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)

## Setup

**1. Install dependencies**
```bash
uv sync
```

**2. Configure environment variables**
```bash
cp .env-template .env
```
Fill in `.env`:
```
MET_OFFICE_API_KEY=  # Met Office DataHub API key

# Darwin Push Port credentials (see below)
DARWIN_SFTP_PASS=
DARWIN_STOMP_USER=
DARWIN_STOMP_PASS=
```

**Getting Darwin Push Port credentials**

1. Log in (or register) at https://opendata.nationalrail.co.uk and open **My Feeds**. Subscribe to the **Darwin** feed if you haven't already.
2. From the FTP details, copy the password into `DARWIN_SFTP_PASS`.
3. From **Darwin Topic Information**, copy **Username** and **Password** into `DARWIN_STOMP_USER` and `DARWIN_STOMP_PASS`. These are different from the FTP login.

The train board uses the real-time Push Port stream (STOMP topic `darwin.pushport-v16`). The stream only sends changes, so on startup the dashboard loads the current state from the SFTP snapshot and the pushport logs written since, then keeps it up to date from the stream. The first board appears roughly 5–40 seconds after startup.

**3. Set up Google Calendar credentials**

- Download `credentials.json` from Google Cloud Console (OAuth 2.0 client for a desktop app)
- Place it in the project root
- On first run the browser will open for OAuth authorisation — `token.json` is then saved automatically

## Running the dashboard

```bash
uv run python scripts/dash_dashboard.py
```

Or start it in the background (logs go to `nohup_dash.out`):
```bash
./scripts/start_dash_dashboard.sh
```

The dashboard listens on all interfaces, so open `http://<your-server-ip>:8050` in any browser on the network (e.g. for a wall-mounted display).

## Running as a background service

Create `/etc/systemd/system/dashboard.service`:
```ini
[Unit]
Description=Super Octo System Dashboard
After=network.target

[Service]
User=squid
WorkingDirectory=/home/squid/code/super-octo-system
ExecStart=/home/squid/.local/bin/uv run python scripts/dash_dashboard.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then enable and start it:
```bash
sudo systemctl enable dashboard
sudo systemctl start dashboard
```

## Running the tests

**Unit tests** (no network required):
```bash
uv run pytest tests/test_dash_dashboard.py -v
```

**Integration tests** (hit real APIs — requires `.env` to be configured):
```bash
uv run pytest tests/test_integration_dash.py -v
```

**All tests:**
```bash
uv run pytest tests/ -v
```

**Unit tests only, skipping integration:**
```bash
uv run pytest tests/ -m "not integration" -v
```
