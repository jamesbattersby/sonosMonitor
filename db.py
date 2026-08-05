"""Shared SQLite store for Sonos play history.

Used by both the logger (sonosMonitor.py, writer) and the web app
(web/app.py, reader). The database is a single file in this directory,
opened in WAL mode so reads and writes can happen concurrently.
"""
import os
import sqlite3
from datetime import datetime

# The .db file lives alongside this module, so the logger and web app
# resolve the same path regardless of their working directory.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sonos.db")


def connect():
    """Return a connection with row access by name and WAL enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets the web app read while the logger writes without locking.
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """Create the plays/status tables and indexes if they don't already exist."""
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plays (
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              played_at TEXT    NOT NULL,   -- 'YYYY-MM-DD HH:MM:SS' local time
              room      TEXT    NOT NULL,
              kind      TEXT    NOT NULL,   -- 'track' | 'radio' | 'line_in'
              title     TEXT,               -- song title (track rows)
              artist    TEXT,               -- track rows
              album     TEXT,               -- track rows
              station   TEXT,               -- station/channel name (radio rows)
              event_id  TEXT,               -- shared across a group's rows for one play
              service   TEXT,               -- on-demand service name (track rows), e.g. 'Spotify'
              source    TEXT                -- line-in source room (line_in rows); may differ from `room`
            );
            CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays(played_at);
            CREATE INDEX IF NOT EXISTS idx_plays_room      ON plays(room);
            CREATE INDEX IF NOT EXISTS idx_plays_kind      ON plays(kind);

            CREATE TABLE IF NOT EXISTS status (
              room          TEXT PRIMARY KEY,
              state         TEXT    NOT NULL,   -- transport state, or 'OFFLINE'/'UNKNOWN'
              volume        INTEGER,             -- 0-100, NULL if unreadable
              mute          INTEGER,             -- 0/1, NULL if unreadable
              group_label   TEXT,                -- e.g. 'Kitchen + 2'
              battery_level INTEGER,              -- 0-100, NULL if not a battery-powered speaker
              updated_at    TEXT    NOT NULL     -- 'YYYY-MM-DD HH:MM:SS' local time of last snapshot
            );
            """
        )
        # Migrate DBs created before event_id/service existed. Must run before
        # creating indexes on those columns, since a pre-existing table won't
        # have them yet (CREATE TABLE IF NOT EXISTS above is a no-op for it).
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(plays)")]
        if "event_id" not in cols:
            conn.execute("ALTER TABLE plays ADD COLUMN event_id TEXT")
            conn.execute(
                # Give each legacy row its own id so it still counts as one
                # distinct play.
                "UPDATE plays SET event_id = 'legacy-' || id WHERE event_id IS NULL"
            )
        if "service" not in cols:
            conn.execute("ALTER TABLE plays ADD COLUMN service TEXT")
        if "source" not in cols:
            conn.execute("ALTER TABLE plays ADD COLUMN source TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_event_id ON plays(event_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_service  ON plays(service)")


def insert_play(room, kind, *, title=None, artist=None, album=None, station=None,
                 event_id=None, service=None, source=None):
    """Insert a single play row, stamping the current local time.

    `event_id` ties together the rows written for every room in a group during
    one play, so aggregations can COUNT(DISTINCT event_id) to count it once.
    `service` names the on-demand music service a track came from (e.g.
    'Spotify'); NULL for local library tracks and for radio rows. `source`
    names the room whose line-in jack is playing (line_in rows only); may
    differ from `room` when a room plays another room's shared line-in.
    """
    played_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO plays (played_at, room, kind, title, artist, album, station, event_id, service, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (played_at, room, kind, title, artist, album, station, event_id, service, source),
        )


def sync_status(rows):
    """Replace the live-status snapshot with `rows` (one dict per currently
    discovered room: room, state, volume, mute, group_label, battery_level),
    and mark any room previously known to `status` but absent from `rows` as
    'OFFLINE' (keeping its last-known volume/group/battery rather than
    clearing them, so the panel can still show what it looked like when last
    seen).

    Called once per full sweep, so status is only ever as fresh as the
    logger's discovery cadence — see POLL_FALLBACK in sonosMonitor.py.
    """
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO status (room, state, volume, mute, group_label, battery_level, updated_at)
                VALUES (:room, :state, :volume, :mute, :group_label, :battery_level, :updated_at)
                ON CONFLICT(room) DO UPDATE SET
                    state         = excluded.state,
                    volume        = excluded.volume,
                    mute          = excluded.mute,
                    group_label   = excluded.group_label,
                    battery_level = excluded.battery_level,
                    updated_at    = excluded.updated_at
                """,
                {**r, "updated_at": updated_at},
            )

        online_rooms = [r["room"] for r in rows]
        placeholders = ",".join("?" * len(online_rooms))
        conn.execute(
            f"UPDATE status SET state = 'OFFLINE', updated_at = ? "
            f"WHERE room NOT IN ({placeholders})",
            [updated_at, *online_rooms],
        )
