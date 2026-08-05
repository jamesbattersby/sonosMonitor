# Sonos Monitor

Tracks what's playing on your **Sonos** speakers and presents the listening
history as a web dashboard — top tracks, artists, albums and radio stations,
filterable by time period and room — plus a live system-status panel (per-room
online/offline, volume, grouping, battery).

It has two long-running parts that share one SQLite database, plus an optional
Apache front end:

```
sonosMonitor.py ──write──▶  sonos.db  ◀──read──  web/app.py (Flask) ◀──proxy── Apache
   (logger)              (SQLite, WAL)              (dashboard)              (/sonos/)
```

## What each part does

| File | Role |
|------|------|
| `sonosMonitor.py` | **Logger.** Subscribes to Sonos UPnP events and records every play to the database in near-real-time, with a 60-second poll as a safety net. Writer. |
| `web/app.py` | **Dashboard.** A small Flask app that reads the database and serves the rankings. Reader. |
| `web/templates/dashboard.html` | The dashboard page (server-rendered; CSS/JS inlined). |
| `db.py` | Shared SQLite schema, connection handling and the `insert_play()` helper. Imported by both the logger and the web app. |
| `sonos.db` | The database (created automatically on first run). |
| `deploy/` | systemd unit files and the Apache config for running it as a service. |
| `requirements.txt` | Python dependencies. |

### How the logger works

- It **subscribes** to each Sonos group coordinator's playback events (UPnP GENA)
  and to zone-grouping changes, so plays are captured the instant they happen
  rather than by constant polling. A full poll runs every 60s as a fallback to
  catch anything missed and to recover after a speaker drops off the network.
- **Radio** stations are logged once per tune-in (not once per song).
- **Grouped rooms:** when several rooms play the same thing as a group, a row is
  recorded for *each* room (so every room appears in the dashboard and per-room
  stats are correct), but the rooms share one event id so the play still **counts
  once** in the "all rooms" totals.

## Requirements

- **Python 3.12** (any recent 3.x should work).
- Python packages (see `requirements.txt`):
  - [`soco`](https://github.com/SoCo/SoCo) — Sonos control/eventing library
  - [`flask`](https://flask.palletsprojects.com/) — web framework
- The machine running the logger must be on the **same local network** as the
  Sonos speakers, and reachable by them on **TCP port 1400** (the UPnP event
  listener) — check your firewall if events never arrive.
- **Optional:** Apache with `mod_proxy` + `mod_proxy_http`, to expose the
  dashboard at a nice URL. Not needed for local use.

## Install

```bash
pip3 install -r requirements.txt
```

The SQLite database needs no setup — `db.py` creates it (and migrates the schema)
automatically on first run.

## Run (quick start / local)

Open two terminals from the project directory:

```bash
# 1) the logger — discovers speakers and records plays (Ctrl+C to stop)
python3 sonosMonitor.py

# 2) the dashboard — serves on http://127.0.0.1:8001/
python3 web/app.py
```

Then browse to **http://127.0.0.1:8001/**. Use the dropdowns to change the time
period, room, how many entries to show (Top 10/25/50/100), and the sort order.

Inspect the database directly at any time:

```bash
python3 -c "import db; c=db.connect(); [print(dict(r)) for r in c.execute('SELECT * FROM plays ORDER BY id DESC LIMIT 10')]"
```

## Run as a service (systemd + Apache)

To keep the logger and dashboard running across reboots and serve the dashboard
through Apache at `/sonos/`.

> The unit files in `deploy/` are templates: `<USER>`, `<PROJECT_DIR>` and
> `<PYTHON_BIN>` placeholders stand in for the user to run as, the project
> directory, and the full path to the Python interpreter where you installed
> the requirements (e.g. the output of `which python3`). **Fill those in in
> both files before installing**, e.g.:
>
> ```bash
> sed -i \
>   -e 's|<USER>|myuser|g' \
>   -e 's|<PROJECT_DIR>|/home/myuser/sonosMonitor|g' \
>   -e 's|<PYTHON_BIN>|/usr/bin/python3|g' \
>   deploy/sonos-logger.service deploy/sonos-web.service
> ```

**1. systemd services** (logger + dashboard):

```bash
sudo cp deploy/sonos-logger.service deploy/sonos-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sonos-logger sonos-web
systemctl status sonos-logger sonos-web        # both should be "active (running)"
```

**2. Apache reverse proxy** (serves the dashboard at `/sonos/`):

```bash
sudo a2enmod proxy proxy_http
sudo cp deploy/apache-sonos.conf /etc/apache2/conf-available/sonos.conf
sudo a2enconf sonos
sudo apache2ctl configtest
sudo systemctl reload apache2
```

The dashboard is then available at **http://<host>/sonos/**.

Useful checks:

```bash
journalctl -u sonos-logger -f        # follow logger output (plays, events)
journalctl -u sonos-web -f           # follow dashboard requests
```

## API

The dashboard also exposes JSON for scripting:

```
GET /api/top?dim=tracks|artists|albums|stations&period=today|7d|30d|year|all&room=<name|all>&limit=<1-500>&order=asc|desc
```

## Notes

- **S1 and S2:** uses only standard SoCo calls, so it works with both Sonos S1
  and S2 systems.
- There is no build step and no automated test suite.
