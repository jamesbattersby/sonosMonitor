"""Flask dashboard for Sonos play history.

Read-only front end over the shared SQLite database (see ../db.py). Designed to
run on 127.0.0.1:8001 behind an Apache reverse proxy (see deploy/apache-sonos.conf).

Renders server-side so it works without JavaScript; the same data is also exposed
as JSON at /api/top for client-side re-filtering.
"""
import os
import sys
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

# Import the shared db module from the project root (one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

app = Flask(__name__)

# Supported dimensions -> label shown in the UI.
DIMENSIONS = {
    "tracks": "Top Tracks",
    "artists": "Top Artists",
    "albums": "Top Albums",
    "stations": "Top Stations",
    "services": "Music Services",
    "line_in": "Line-In Play Starts",
}

# Period options -> human label. Value drives since_for().
PERIODS = {
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "year": "This year",
    "all": "All time",
}

DEFAULT_LIMIT = 10
LIMIT_CHOICES = [10, 25, 50, 100]

# Sort order for the play count. Value -> (SQL direction, human label).
ORDERS = {
    "desc": ("DESC", "Most played first"),
    "asc": ("ASC", "Fewest played first"),
}
DEFAULT_ORDER = "desc"


def _clamp_tz_offset(raw, default=0):
    """Clamp a browser UTC-offset (minutes) to the real range (-12h..+14h),
    so a malformed/adversarial 'tz' query param can't feed nonsense into the
    datetime arithmetic below."""
    try:
        return max(-720, min(840, int(raw)))
    except (TypeError, ValueError):
        return default


def _tz_delta_minutes(tz_param):
    """Minutes to add to a `played_at` value (stamped with the server's own
    wall clock -- see db.py) to read it in the browser's time zone.

    `tz_param` is the browser's own UTC offset in minutes, from the 'tz'
    query param (see the detection script in dashboard.html, which reads
    JS's `-Date().getTimezoneOffset()`); None or '' if the browser hasn't
    reported it yet (the very first request, before that script has
    redirected with 'tz' set; the noscript "Apply" button submits the
    filter form's hidden 'tz' field empty; or a non-JS/API caller that
    didn't pass one) -- in which case this returns 0 (no shift), leaving
    times in the server's own time zone, same as before this existed.

    The server's own offset is computed fresh per call (not cached) so it
    stays correct across a DST transition in this long-lived process.
    """
    if not tz_param:
        return 0
    browser_offset = _clamp_tz_offset(tz_param)
    server_offset = round(datetime.now().astimezone().utcoffset().total_seconds() / 60)
    return browser_offset - server_offset


def since_for(period, delta_minutes=0):
    """Return a lower-bound 'YYYY-MM-DD HH:MM:SS' string, or None for all time.

    `delta_minutes` (see _tz_delta_minutes()) shifts the 'today'/'year'
    calendar boundaries to the browser's local calendar day/year rather than
    the server's; '7d'/'30d' anchor on today's own calendar boundary too --
    the end of today, minus 7 or 30 days -- rather than a rolling
    exactly-N*24h window back from the current moment, so the shift added
    going in and subtracted coming out still cancels out the same way.
    """
    now = datetime.now() + timedelta(minutes=delta_minutes)
    end_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = end_of_today - timedelta(days=7)
    elif period == "30d":
        start = end_of_today - timedelta(days=30)
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # "all" or anything unrecognised
        return None
    return (start - timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _previous_bounds(period, delta_minutes=0):
    """Return (prev_start, prev_end) `played_at` bounds -- server-time strings,
    window is [prev_start, prev_end) -- for the position-change indicator, or
    None for 'all' (no fixed start, so no meaningful "previous window").

    Defined generically as "the same-length window immediately before the
    current period's start" (prev_end == since_for()'s own start) rather than
    a calendar-specific "yesterday"/"last year", so today/7d/30d/year all
    share one implementation without special-casing.
    """
    since = since_for(period, delta_minutes)
    if since is None:
        return None
    start = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
    length = datetime.now() - start
    return (start - length).strftime("%Y-%m-%d %H:%M:%S"), since


# Each dimension defines the grouping columns and the label columns to return.
# All use COUNT(*) AS plays and share the same WHERE-clause builder below.
_DIM_SQL = {
    "tracks": {
        # Grouped by artist+title alone so play counts aren't fragmented by
        # album (the same track can appear on a single, an album, and a
        # compilation); when those plays disagree on album, label it
        # 'Various' rather than picking one arbitrarily.
        "select": "artist, title, CASE WHEN COUNT(DISTINCT album) > 1 THEN 'Various' ELSE MAX(album) END AS album",
        "where": "kind = 'track' AND title IS NOT NULL AND title <> ''",
        "group": "artist, title",
        # Ties on play count sort by artist, then album, then title -- more
        # useful for browsing than the bare artist/title GROUP BY columns.
        "tie_break": "artist, album, title",
    },
    "artists": {
        "select": "artist",
        "where": "kind = 'track' AND artist IS NOT NULL AND artist <> ''",
        "group": "artist",
    },
    "albums": {
        # Grouped by album alone (not artist+album): a compilation has the same
        # album title across many different track artists, so if more than one
        # distinct artist shows up under an album, label it 'Various' rather
        # than fragmenting it into one low-count row per artist.
        "select": "CASE WHEN COUNT(DISTINCT artist) > 1 THEN 'Various' ELSE artist END AS artist, album",
        "where": "kind = 'track' AND album IS NOT NULL AND album <> ''",
        "group": "album",
    },
    "stations": {
        "select": "station",
        "where": "kind = 'radio' AND station IS NOT NULL AND station <> ''",
        "group": "station",
    },
    "services": {
        "select": "service",
        "where": "kind = 'track' AND service IS NOT NULL AND service <> ''",
        "group": "service",
    },
    "line_in": {
        # 'source' is which room's line-in jack fed the play, which usually
        # matches 'room' (the room it was heard in) but can differ when a
        # room plays another room's shared line-in.
        "select": "room, source",
        "where": "kind = 'line_in'",
        "group": "room, source",
    },
}


def top(dim, period, room, limit, order=DEFAULT_ORDER, delta_minutes=0):
    """Return ranked rows for a dimension as a list of dicts (label cols + 'plays').

    Play count is COUNT(DISTINCT event_id) so a track played across a group of
    rooms at once counts once, not once per room.
    """
    spec = _DIM_SQL[dim]
    direction = ORDERS.get(order, ORDERS[DEFAULT_ORDER])[0]  # whitelisted -> safe to inline

    where = [spec["where"]]
    params = []

    since = since_for(period, delta_minutes)
    if since is not None:
        where.append("played_at >= ?")
        params.append(since)

    if room and room != "all":
        where.append("room = ?")
        params.append(room)

    tie_break = spec.get("tie_break", spec["group"])
    sql = (
        f"SELECT {spec['select']}, COUNT(DISTINCT event_id) AS plays "
        f"FROM plays WHERE {' AND '.join(where)} "
        f"GROUP BY {spec['group']} "
        f"ORDER BY plays {direction}, {tie_break} ASC "
        f"LIMIT ?"
    )
    params.append(limit)

    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def previous_ranks(dim, room, period, order=DEFAULT_ORDER, delta_minutes=0):
    """Return {key_tuple: rank} for every row in dim's *previous* period (see
    `_previous_bounds()`), unlimited (not just the visible top-n) so a row
    that fell out of the current period's top-n still resolves to its real
    previous rank rather than looking new. Keyed on the same columns as
    `top()`'s GROUP BY (`spec['group']`), which is exactly the identity a row
    should be matched on across periods. Ranked with the same play-count
    direction as the currently selected `order`, since that's what the '#'
    column's own position means. Returns None for 'all' (see
    `_previous_bounds()`).
    """
    bounds = _previous_bounds(period, delta_minutes)
    if bounds is None:
        return None
    prev_start, prev_end = bounds

    spec = _DIM_SQL[dim]
    direction = ORDERS.get(order, ORDERS[DEFAULT_ORDER])[0]
    key_cols = [c.strip() for c in spec["group"].split(",")]
    tie_break = spec.get("tie_break", spec["group"])

    where = [spec["where"], "played_at >= ?", "played_at < ?"]
    params = [prev_start, prev_end]
    if room and room != "all":
        where.append("room = ?")
        params.append(room)

    # SELECT uses spec['select'] (not just spec['group']) so a tie_break
    # column outside the GROUP BY key (e.g. tracks' 'album') is available to
    # ORDER BY -- same query shape as top(), just bounded instead of limited,
    # so ranks here land in the same order the table itself displays.
    sql = (
        f"SELECT {spec['select']}, COUNT(DISTINCT event_id) AS plays "
        f"FROM plays WHERE {' AND '.join(where)} "
        f"GROUP BY {spec['group']} "
        f"ORDER BY plays {direction}, {tie_break} ASC"
    )

    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    ranks = _dense_ranks(rows)
    return {tuple(r[c] for c in key_cols): ranks[i] for i, r in enumerate(rows)}


def _dense_ranks(rows):
    """Standard competition ranking (1-2-2-4) from a plays-ordered row/dict
    list: rows tied on 'plays' share the same rank, and the rank right after
    a tied group reflects how many rows precede it (not a dense 1-2-3 rank
    that would pretend the tie never happened).
    """
    ranks = []
    for i, row in enumerate(rows):
        ranks.append(ranks[-1] if i and row["plays"] == rows[i - 1]["plays"] else i + 1)
    return ranks


def _annotate_rank(rows):
    """Tag each row (in place) with `_rank` (its competition rank -- see
    `_dense_ranks()`) and `_rank_tied` (True when it shares that rank with
    the row above, i.e. the '#' column should show '=' instead of the
    number). Must run before `_annotate_position_change()`, which reuses
    `_rank` as the row's current-period rank.
    """
    ranks = _dense_ranks(rows)
    for i, row in enumerate(rows):
        row["_rank"] = ranks[i]
        row["_rank_tied"] = i > 0 and ranks[i] == ranks[i - 1]


def _annotate_position_change(rows, dim, room, period, order, delta_minutes=0):
    """Tag each row (in place) with `_pos_symbol` ('up'/'down'/'same'/'new'/
    None) and `_pos_delta` (positions moved, or None when there's nothing to
    show) by comparing its `_rank` (set by `_annotate_rank()`, which must run
    first) against `previous_ranks()`. `_pos_symbol` is None when there's no
    previous period to compare against ('all') -- the template renders
    nothing for that row.
    """
    spec = _DIM_SQL[dim]
    key_cols = [c.strip() for c in spec["group"].split(",")]
    prev_ranks = previous_ranks(dim, room, period, order, delta_minutes)

    for row in rows:
        if prev_ranks is None:
            row["_pos_symbol"] = None
            row["_pos_delta"] = None
            continue

        current_rank = row["_rank"]
        prev_rank = prev_ranks.get(tuple(row[c] for c in key_cols))
        if prev_rank is None:
            row["_pos_symbol"] = "new"
            row["_pos_delta"] = None
        else:
            delta = prev_rank - current_rank  # positive => now higher up (lower rank number)
            row["_pos_symbol"] = "up" if delta > 0 else "down" if delta < 0 else "same"
            row["_pos_delta"] = abs(delta) or None


# Time-bucket format (SQLite strftime) and axis-label extractor per period, for
# the "plays over time" chart. Bucket width is chosen per period: 1 hour for
# 'today'/'7d', 1 day for '30d'/'year', 1 month for 'all'.
_CHART_BUCKETS = {
    "today": ("%Y-%m-%d %H:00", lambda b: b[11:16]),
    "7d": ("%Y-%m-%d %H:00", lambda b: f"{b[8:10]}/{b[11:13]}h"),
    "30d": ("%Y-%m-%d", lambda b: b[5:]),
    "year": ("%Y-%m-%d", lambda b: b[5:]),
    "all": ("%Y-%m", lambda b: b),
}
CHART_MAX_LABELS = 10  # thin out x-axis labels so they don't overlap

# Stacked-by-artist chart: only the top N artists (by plays in the charted
# window) get their own series/color; everyone else folds into one "Other"
# segment so an unbounded artist count never blows past the palette. Slots
# 1-8 are the dataviz skill's validated categorical palette; 9-15 are a
# best-effort extension (see --series-9..15 in dashboard.html) chosen so
# every *adjacent* pair in this fixed order clears the CVD-safety target —
# the property a stacked bar actually needs — but, unlike the first 8,
# not checked against every non-adjacent pair, so two distant ranks could
# still land on similar colors if they end up visually next to each other
# in a bucket where the ranks between them have no plays.
MAX_ARTIST_SERIES = 15
OTHER_LABEL = "Other"


def plays_over_time_by_artist(period, room, delta_minutes=0):
    """Track play-starts per time bucket per artist, as [{'bucket', 'artist',
    'plays'}], oldest first.

    A "play start" is a distinct event_id, matching the count used everywhere
    else (a grouped play counts once, not once per room).

    `delta_minutes` (see _tz_delta_minutes()) shifts `played_at` into the
    browser's time zone *before* bucketing, via a bound SQLite datetime
    modifier, so bucket boundaries land on the browser's clock, not the
    server's -- not just their displayed labels.
    """
    fmt, _ = _CHART_BUCKETS.get(period, _CHART_BUCKETS["30d"])

    where = ["kind = 'track'", "artist IS NOT NULL", "artist <> ''"]
    params = []

    since = since_for(period, delta_minutes)
    if since is not None:
        where.append("played_at >= ?")
        params.append(since)

    if room and room != "all":
        where.append("room = ?")
        params.append(room)

    # fmt comes from the fixed _CHART_BUCKETS map above, not user input; the
    # modifier is a bound param even though it's server-computed, since
    # SQLite datetime modifiers accept bound values like any other expression.
    modifier = f"{delta_minutes:+d} minutes"
    sql = (
        f"SELECT strftime('{fmt}', played_at, ?) AS bucket, artist, COUNT(DISTINCT event_id) AS plays "
        f"FROM plays WHERE {' AND '.join(where)} "
        "GROUP BY bucket, artist ORDER BY bucket ASC"
    )

    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, [modifier] + params)]


def _bucket_range(period, first_bucket, delta_minutes=0):
    """Every bucket label spanning the period, oldest to now — including ones
    with no plays. Without this, a quiet stretch at the start or end of the
    window (e.g. no plays yet today, or nothing in the last few hours of a 7d
    view) just isn't in `plays_over_time_by_artist()`'s result, so the chart
    would be silently truncated instead of showing an empty bar.

    'today'/'7d'/'30d'/'year' anchor on the period's own start (matching
    since_for(), including its end-of-today-minus-N-days anchor for 7d/30d);
    'all' has no fixed start, so it anchors on `first_bucket` (the earliest
    bucket actually seen in the data).

    `delta_minutes` (see _tz_delta_minutes()) shifts `now` into the browser's
    time zone, unlike since_for() it's *not* shifted back afterwards: these
    bucket strings are matched directly against plays_over_time_by_artist()'s
    already browser-shifted bucket keys, so both need to live in that same
    (browser-local) frame.
    """
    fmt, _ = _CHART_BUCKETS.get(period, _CHART_BUCKETS["30d"])
    now = datetime.now() + timedelta(minutes=delta_minutes)
    end_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    # 'today' always fills all 24 hourly buckets, even ones still in the
    # future relative to `now` -- so the chart's x-axis has a stable, full-day
    # shape all day rather than growing bar-by-bar as the day goes on. Every
    # other period still stops at `now` (there's no data past it anyway).
    if period == "today":
        start, step = now.replace(hour=0, minute=0, second=0, microsecond=0), timedelta(hours=1)
        limit = end_of_today - step
    elif period == "7d":
        start = end_of_today - timedelta(days=7)
        step = timedelta(hours=1)
        limit = now
    elif period == "30d":
        start = end_of_today - timedelta(days=30)
        step = timedelta(days=1)
        limit = now
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
        limit = now
    else:  # "all" — monthly step, handled separately below since it isn't fixed-length
        start, step, limit = datetime.strptime(first_bucket, fmt), None, None

    buckets = []
    cur = start
    if step is not None:
        while cur <= limit:
            buckets.append(cur.strftime(fmt))
            cur += step
    else:
        end = now.replace(day=1)
        cur = cur.replace(day=1)
        while cur <= end:
            buckets.append(cur.strftime(fmt))
            cur = cur.replace(year=cur.year + (cur.month // 12), month=(cur.month % 12) + 1)
    return buckets


def build_chart(rows, period, delta_minutes=0):
    """Turn plays_over_time_by_artist() rows into stacked-bar data for the
    client-side D3 chart: each bucket becomes a bar, each bar stacked by
    artist. The top MAX_ARTIST_SERIES artists (by total plays across the
    charted window) each get a fixed palette slot so a given artist keeps
    the same color in every bucket; everyone else is summed into one
    'Other' segment. Buckets with no plays (at the start, end, or in the
    middle of the window) still get a bar — see _bucket_range(). Scaling and
    stacking geometry is D3's job (see dashboard.html); this only hands over
    raw counts.

    Returns {'buckets': [...], 'series': [...]} (for the legend), or None if
    there are no rows.
    """
    if not rows:
        return None

    _, label_for = _CHART_BUCKETS.get(period, _CHART_BUCKETS["30d"])

    totals = {}
    by_bucket = {}
    for r in rows:
        totals[r["artist"]] = totals.get(r["artist"], 0) + r["plays"]
        by_bucket.setdefault(r["bucket"], {})[r["artist"]] = r["plays"]

    # Rank once over the whole window so an artist's color/rank never shifts
    # from one bucket to the next.
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    top_artists = [artist for artist, _ in ranked[:MAX_ARTIST_SERIES]]
    top_set = set(top_artists)
    series = [
        {"name": artist, "color_var": f"--series-{i + 1}"}
        for i, artist in enumerate(top_artists)
    ]
    if len(ranked) > MAX_ARTIST_SERIES:
        series.append({"name": OTHER_LABEL, "color_var": "--series-other"})

    buckets = _bucket_range(period, first_bucket=rows[0]["bucket"], delta_minutes=delta_minutes)
    bucket_segs = {}
    bucket_totals = {}
    for b in buckets:
        segs = {}
        for artist, plays in by_bucket.get(b, {}).items():
            key = artist if artist in top_set else OTHER_LABEL
            segs[key] = segs.get(key, 0) + plays
        bucket_segs[b] = segs
        bucket_totals[b] = sum(segs.values())

    n = len(buckets)
    label_stride = max(1, -(-n // CHART_MAX_LABELS))  # ceil(n / CHART_MAX_LABELS)

    out_buckets = []
    for i, b in enumerate(buckets):
        segs = bucket_segs[b]
        bar_segments = [
            {"artist": s["name"], "plays": segs[s["name"]], "color_var": s["color_var"]}
            for s in series
            if segs.get(s["name"])
        ]
        out_buckets.append({
            "bucket": b,
            "total": bucket_totals[b],
            "segments": bar_segments,
            "label": label_for(b) if (i % label_stride == 0 or i == n - 1) else None,
        })
    return {"buckets": out_buckets, "series": series}


def artist_totals(period, room, delta_minutes=0):
    """All artists with play counts for the period/room, ranked plays DESC —
    the whole-period breakdown behind the artist-share bar. Deliberately
    ignores the 'order'/'limit' controls that shape the Top Artists table:
    this needs every artist (to compute accurate percentages) always ranked
    the same way (so the top-N/color assignment below is stable).
    """
    where = ["kind = 'track'", "artist IS NOT NULL", "artist <> ''"]
    params = []

    since = since_for(period, delta_minutes)
    if since is not None:
        where.append("played_at >= ?")
        params.append(since)

    if room and room != "all":
        where.append("room = ?")
        params.append(room)

    sql = (
        f"SELECT artist, COUNT(DISTINCT event_id) AS plays "
        f"FROM plays WHERE {' AND '.join(where)} "
        "GROUP BY artist ORDER BY plays DESC, artist ASC"
    )

    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def build_artist_share(rows):
    """Turn artist_totals() rows into a single 100%-stacked bar's segments:
    the top MAX_ARTIST_SERIES artists each get a fixed palette slot (the same
    slots/colors the artist-over-time chart uses), the rest fold into one
    'Other' segment so the bar never runs out of distinguishable colors.

    Returns {'segments': [...], 'total': n} or None if there are no rows.
    """
    if not rows:
        return None

    total = sum(r["plays"] for r in rows)
    top_rows = rows[:MAX_ARTIST_SERIES]
    other_total = sum(r["plays"] for r in rows[MAX_ARTIST_SERIES:])

    segments = [
        {
            "artist": r["artist"],
            "plays": r["plays"],
            "pct": round(r["plays"] / total * 100, 1),
            "color_var": f"--series-{i + 1}",
        }
        for i, r in enumerate(top_rows)
    ]
    if other_total:
        segments.append({
            "artist": OTHER_LABEL,
            "plays": other_total,
            "pct": round(other_total / total * 100, 1),
            "color_var": "--series-other",
        })
    return {"segments": segments, "total": total}


def rooms():
    """Distinct room names present in the data, alphabetical."""
    with db.connect() as conn:
        return [r["room"] for r in conn.execute("SELECT DISTINCT room FROM plays ORDER BY room")]


# Transport state -> (display label, status-color class). 'OFFLINE' is a real
# problem (unreachable), so it borrows the status palette's severity colors;
# 'PAUSED_PLAYBACK'/'STOPPED' are just idle, not a problem, so they stay
# neutral ink rather than a status color. Anything not listed here (a state
# string this hasn't seen before) falls back to 'serious' in system_status().
_STATE_DISPLAY = {
    "PLAYING": ("Playing", "good"),
    "PAUSED_PLAYBACK": ("Paused", "muted"),
    "STOPPED": ("Stopped", "muted"),
    "OFFLINE": ("Offline", "critical"),
}


def system_status(delta_minutes=0):
    """Latest known live status per room (see db.sync_status), alphabetical.

    Not period/room-filtered like the rest of the dashboard -- it's a live
    snapshot, not history, so it always shows every known room.

    `delta_minutes` (see _tz_delta_minutes()) shifts `updated_at` -- stamped
    with the server's own wall clock, like `played_at` -- into the browser's
    time zone for display.
    """
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM status ORDER BY room")]

    for r in rows:
        r["state_label"], r["status_class"] = _STATE_DISPLAY.get(r["state"], ("Unknown", "serious"))
        r["mute"] = bool(r["mute"])
        updated = datetime.strptime(r["updated_at"], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=delta_minutes)
        r["updated_at"] = updated.strftime("%Y-%m-%d %H:%M")  # trim seconds
        # For the polling row-highlight (see patchTable() in dashboard.html):
        # _key identifies the row across polls, _sig is what "changed" means.
        # updated_at is deliberately excluded from _sig -- sync_status()
        # bumps it for every room on every sweep regardless of whether
        # anything actually changed, so including it would flash every row
        # on every poll instead of just the ones with real changes.
        r["_key"] = r["room"]
        r["_sig"] = "|".join(str(r[c]) for c in ("state", "volume", "mute", "group_label", "battery_level"))
    return rows


def _clamp_limit(raw, default=DEFAULT_LIMIT):
    try:
        return min(max(int(raw), 1), 500)
    except (TypeError, ValueError):
        return default


def _parse_filters():
    """Normalize the period/room/n/order/tz query params shared by '/' and
    '/api/refresh' so both routes apply the exact same fallbacks.

    'tz' (the browser's UTC offset in minutes, set by dashboard.html's
    detection script -- see _tz_delta_minutes()) is returned raw, not
    clamped/defaulted, so the template can tell "browser hasn't reported yet"
    (None) apart from an explicit value when echoing it into the filter
    form's hidden field.
    """
    period = request.args.get("period", "7d")
    if period not in PERIODS:
        period = "7d"
    room = request.args.get("room", "all")
    limit = _clamp_limit(request.args.get("n"), DEFAULT_LIMIT)
    order = request.args.get("order", DEFAULT_ORDER)
    if order not in ORDERS:
        order = DEFAULT_ORDER
    tz = request.args.get("tz")
    return period, room, limit, order, tz


def _dashboard_data(period, room, limit, order, delta_minutes=0):
    """Everything the dashboard shows for a given filter set: ranked tables,
    the two charts, and the live status snapshot. Shared by '/' (full page)
    and '/api/refresh' (polling) so they can never disagree."""
    tables = {
        dim: {"label": label, "rows": top(dim, period, room, limit, order, delta_minutes)}
        for dim, label in DIMENSIONS.items()
    }
    for dim, table in tables.items():
        _annotate_rank(table["rows"])
        _annotate_position_change(table["rows"], dim, room, period, order, delta_minutes)

    # For the polling row-highlight (see patchTable() in dashboard.html): a
    # row's _key is everything but its play count, rank, and position-change
    # (all volatile on their own, independent of the row's identity), so
    # re-ranking alone (no real change) never counts as a change; _sig folds
    # those volatile bits back in, so any of them changing (a new play, a
    # rank/tie shuffle, or a shift vs the previous period) flashes the row.
    _VOLATILE_COLS = ("plays", "_rank", "_rank_tied", "_pos_symbol", "_pos_delta")
    for table in tables.values():
        for row in table["rows"]:
            row["_key"] = "|".join(str(v) for k, v in row.items() if k not in _VOLATILE_COLS)
            row["_sig"] = "|".join(str(row[c]) for c in _VOLATILE_COLS)
    chart = build_chart(plays_over_time_by_artist(period, room, delta_minutes), period, delta_minutes)
    artist_share = build_artist_share(artist_totals(period, room, delta_minutes))
    status = system_status(delta_minutes)
    return tables, chart, artist_share, status


@app.route("/")
def dashboard():
    period, room, limit, order, tz = _parse_filters()
    tables, chart, artist_share, status = _dashboard_data(
        period, room, limit, order, _tz_delta_minutes(tz)
    )

    return render_template(
        "dashboard.html",
        tables=tables,
        chart=chart,
        artist_share=artist_share,
        status=status,
        periods=PERIODS,
        rooms=rooms(),
        orders=ORDERS,
        limit_choices=LIMIT_CHOICES,
        selected_period=period,
        selected_room=room,
        selected_limit=limit,
        selected_order=order,
        selected_tz=tz,
    )


@app.route("/api/refresh")
def api_refresh():
    """Polled by the dashboard's client-side JS every few seconds so the
    page can pick up new plays/status without a full reload. Renders the
    same partials the initial page uses (see _status_table.html /
    _ranked_table.html) so the markup never drifts between the two, and
    returns the two charts' raw data in the same shape already embedded in
    the page (see build_chart/build_artist_share) so the client can diff
    against what it last rendered before touching the DOM.
    """
    period, room, limit, order, tz = _parse_filters()
    tables, chart, artist_share, status = _dashboard_data(
        period, room, limit, order, _tz_delta_minutes(tz)
    )

    return jsonify(
        {
            "status_html": render_template("_status_table.html", status=status),
            "tables_html": {
                dim: render_template("_ranked_table.html", table=table)
                for dim, table in tables.items()
            },
            "chart": chart,
            "artist_share": artist_share,
        }
    )


@app.route("/api/top")
def api_top():
    dim = request.args.get("dim", "tracks")
    if dim not in DIMENSIONS:
        return jsonify({"error": f"unknown dim '{dim}'"}), 400

    period = request.args.get("period", "7d")
    room = request.args.get("room", "all")
    limit = _clamp_limit(request.args.get("limit"), DEFAULT_LIMIT)
    order = request.args.get("order", DEFAULT_ORDER)
    if order not in ORDERS:
        order = DEFAULT_ORDER
    # 'tz' (browser UTC offset, minutes) is optional here -- a non-browser
    # API caller that omits it just gets 'today'/'year' boundaries in the
    # server's own time zone, same as before this existed.
    delta_minutes = _tz_delta_minutes(request.args.get("tz"))

    return jsonify(
        {
            "dim": dim,
            "period": period,
            "room": room,
            "limit": limit,
            "order": order,
            "rows": top(dim, period, room, limit, order, delta_minutes),
        }
    )


if __name__ == "__main__":
    db.init_db()
    app.run(host="127.0.0.1", port=8001, debug=True)
