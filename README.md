# super-octo-system

A personal home dashboard for a portrait touch screen, built with Dash and [Dash Mantine Components](https://www.dash-mantine-components.com/). Shows Google Calendar events, Met Office weather, live National Rail departures, Tube status, a weekly menu, a shopping list, the household schedule and a recipe finder, refreshing automatically.

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
uv run python scripts/dash_app.py
```

Or start it in the background (logs go to `nohup_app.out`):
```bash
./scripts/start_dash_app.sh
```

The dashboard listens on all interfaces, so open `http://<your-server-ip>:8051` in any browser on the network. Open pages reload themselves when it restarts.

Each section is a full-screen tab (Today, Trains, Calendar, Food, Schedule) picked from the bottom bar; swipe left or right to change tab, and after 3 minutes without a touch it goes back to Today. Styles and touch handling live in `scripts/app_assets/`. To scale everything up or down for a different screen, change the `font-size` at the top of `app.css`. The data fetching (calendar, weather, trains, menu sheets) is in `scripts/dashboard_data.py`.

- **Today**: weather, horizontal timelines for today and tomorrow, and this week's menu with tick boxes for cooked recipes; it's drawn a little smaller than the other tabs so it fits on one screen (the `.panel-today` block in `app.css`)
- **Trains**: any train that stops at the destination, with arrival time and journey length; Darwin station messages for the chosen stations; Tube and Elizabeth line status from TfL; on Today, a "leave in N min" countdown for trains from Hadley Wood (set the walk in `WALK_TO_STATION_MINUTES`, default 10)
- **Bin collections** ("Black bin collection" and so on) show in the bin's colour on Today and in the Calendar tab
- **Food**: a shared shopping list you add to (type an item, or tap one of this week's ingredients) and tick off, recipe cards with their ingredients, and the recipe finder
- **Cooking timer** from the Timer button in the bottom bar, which beeps until stopped
- **Night**: the screen dims from 22:30 to 06:30 after two minutes untouched, and a tap wakes it; the moon button in the header turns this on or off for that screen (`NIGHT_DIM=off` in `.env` makes off the default)

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
ExecStart=/home/squid/.local/bin/uv run python scripts/dash_app.py
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
uv run pytest tests/test_dash_app.py tests/test_dashboard_data.py -v
```

**Integration tests** (hit real APIs — requires `.env` to be configured):
```bash
uv run pytest tests/test_integration_data.py -v
```

**All tests:**
```bash
uv run pytest tests/ -v
```

**Unit tests only, skipping integration:**
```bash
uv run pytest tests/ -m "not integration" -v
```
