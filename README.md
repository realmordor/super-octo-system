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

### Phone-style version (portrait touch screens)

`scripts/dash_app.py` shows the same data as full-screen tabs (Today, Trains, Calendar, Food, Schedule) with a bottom navigation bar, built with [Dash Mantine Components](https://www.dash-mantine-components.com/). Swipe left or right to change tab; after 3 minutes without a touch it goes back to Today. It runs alongside the original on port 8051:
```bash
./scripts/start_dash_app.sh    # logs go to nohup_app.out
```
Its styles and touch handling live in `scripts/app_assets/`. To scale everything up or down for a different screen, change the `font-size` at the top of `app.css`.

What it adds over the original:
- **Trains**: any train that stops at the destination (not just ones that end there), with arrival time and journey length; Darwin station messages for the chosen stations; Tube and Elizabeth line status from TfL; on Today, a "leave in N min" countdown for trains from Hadley Wood (set the walk in `WALK_TO_STATION_MINUTES`, default 10)
- **Today**: weather, horizontal timelines for today and tomorrow, and this week's menu with tick boxes for cooked recipes
- **Food**: a shared shopping list you add to (type an item, or tap one of this week's ingredients) and tick off, recipe cards with their ingredients, and the recipe finder
- **Cooking timer** from the Timer button in the bottom bar, which beeps until stopped
- **Night**: the screen dims from 22:30 to 06:30; a tap wakes it for two minutes

Cooked ticks are saved in `.cooked.json` and cleared as soon as the weekly menu changes. The shopping list is saved in `.shopping.json` and kept until items are removed.

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
