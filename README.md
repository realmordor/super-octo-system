# super-octo-system

A personal home dashboard built with Streamlit. Displays Google Calendar events, Met Office weather, National Rail departures, a weekly menu, household schedule, and a recipe finder — all in one page, auto-refreshing every 60 seconds.

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
DARWIN_LITE_TOKEN=   # National Rail Darwin Lite API token
MET_OFFICE_API_KEY=  # Met Office DataHub API key
```

**3. Set up Google Calendar credentials**

- Download `credentials.json` from Google Cloud Console (OAuth 2.0 client for a desktop app)
- Place it in the project root
- On first run the browser will open for OAuth authorisation — `token.json` is then saved automatically

## Running the dashboard

```bash
uv run streamlit run scripts/dashboard.py
```

To serve on your local network (e.g. for a wall-mounted display):
```bash
uv run streamlit run scripts/dashboard.py --server.address 0.0.0.0 --server.headless true
```

Then open `http://<your-server-ip>:8501` in any browser on the network.

## Running as a background service

Create `/etc/systemd/system/dashboard.service`:
```ini
[Unit]
Description=Super Octo System Dashboard
After=network.target

[Service]
User=squid
WorkingDirectory=/home/squid/code/super-octo-system
ExecStart=/home/squid/.local/bin/uv run streamlit run scripts/dashboard.py --server.address 0.0.0.0 --server.headless true
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
uv run pytest tests/test_dashboard.py -v
```

**Integration tests** (hit real APIs — requires `.env` to be configured):
```bash
uv run pytest tests/test_integration.py -v
```

**All tests:**
```bash
uv run pytest tests/ -v
```

**Unit tests only, skipping integration:**
```bash
uv run pytest tests/ -m "not integration" -v
```
