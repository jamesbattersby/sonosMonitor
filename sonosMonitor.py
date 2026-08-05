import queue
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import soco
from soco.events import event_listener

import db

# Configuration
POLL_FALLBACK = 60      # Safety-net full poll when no events arrive (seconds)
SUB_TIMEOUT = 300       # UPnP subscription lifetime; auto-renewed before expiry
DISCOVER_TIMEOUT = 5    # soco.discover() wait (seconds)

# Sonos service id (the numeric 'sid=' query param on on-demand track URIs) ->
# service name, e.g. '9' -> 'Spotify'. Populated once from a coordinator's
# service catalog and reused for the life of the process.
_SERVICE_NAMES = None

_SID_RE = re.compile(r"[?&]sid=(\d+)")
_SPOTIFY_CONNECT_RE = re.compile(r"^x-sonos-vli:.*,spotify:")
_LINE_IN_RE = re.compile(r"^x-rincon-stream:(.+)")

# Line-in source uid -> its custom name (e.g. 'Turntable'), once resolved.
# Cached per uid for the process lifetime, like _SERVICE_NAMES -- a room's
# line-in name rarely changes and it's otherwise a per-event network call.
_LINE_IN_NAMES = {}


def _line_in_name(zone, uid, uri):
    """Best-effort custom line-in name for a line-in URI (e.g. 'Turntable'),
    or None.

    The name isn't in any AVTransport call (GetPositionInfo/GetMediaInfo both
    return blank metadata for a live line-in signal) -- it lives in the
    *source* device's own ContentDirectory, under 'AI:' ("Audio In"), the
    same listing the Sonos app's line-in source picker reads. Must be queried
    on that specific device (`zone`), not the coordinator playing it, since a
    room can play another room's shared line-in.

    `zone` is looked up by `uid` from a discovery (see zone_by_uid in
    main()); if that uid hasn't been seen yet, returns None without caching
    so a later call (once the device is known) can still resolve it. Once a
    real query is attempted, the result -- including None for a device with
    no line-in configured -- is cached for good.
    """
    if uid in _LINE_IN_NAMES:
        return _LINE_IN_NAMES[uid]
    if zone is None:
        return None
    name = None
    try:
        result = zone.contentDirectory.Browse([
            ("ObjectID", "AI:"),
            ("BrowseFlag", "BrowseDirectChildren"),
            ("Filter", "*"),
            ("StartingIndex", 0),
            ("RequestedCount", 100),
            ("SortCriteria", ""),
        ])["Result"]
        root = ET.fromstring(result)
        for item in root.findall("{*}item"):
            if item.findtext("{*}res") == uri:
                name = item.findtext("{http://purl.org/dc/elements/1.1/}title")
                break
    except Exception:
        pass
    _LINE_IN_NAMES[uid] = name
    return name


def _load_service_names(coord):
    """Fetch the household's music service catalog from a coordinator.

    Maps both the catalog's raw Id and its derived ServiceType (Id*256+7,
    the value SoCo notes is used elsewhere in Sonos, e.g. for tokens) to the
    service name, since it isn't documented which one shows up as a track
    URI's 'sid='.
    """
    names = {}
    try:
        descriptor_xml = coord.musicServices.ListAvailableServices()["AvailableServiceDescriptorList"]
        root = ET.fromstring(descriptor_xml)
        for service in root.findall("Service"):
            name = service.get("Name")
            raw_id = service.get("Id")
            names[raw_id] = name
            names[str(int(raw_id) * 256 + 7)] = name
    except Exception:
        pass
    return names


def service_from_uri(coord, uri):
    """Best-effort on-demand music service name for a track URI, or None.

    Tracks played from a service via the Sonos app carry a 'sid=' query
    param on their URI; Spotify Connect sessions use a distinct URI scheme
    with no sid. Local library, radio, line-in, TV and AirPlay all return
    None (radio/library are handled separately; the rest have no service to
    report).
    """
    global _SERVICE_NAMES
    if _SPOTIFY_CONNECT_RE.match(uri):
        return "Spotify"
    match = _SID_RE.search(uri)
    if not match:
        return None
    if _SERVICE_NAMES is None:
        _SERVICE_NAMES = _load_service_names(coord)
    return _SERVICE_NAMES.get(match.group(1))

def log_line(room, detail):
    """Prints a formatted entry to the console (captured by the systemd journal)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{room}] {detail}")

def coordinator_playback(coord, zone_by_uid):
    """Snapshot what a group coordinator is playing.

    Grouped satellite speakers report blank metadata (they're slaved to the
    coordinator via an x-rincon: URI), so playback is always read from the
    coordinator. Callers then attribute it to each room in the group.

    `zone_by_uid` (uid -> SoCo, refreshed each full_sweep() from discovery)
    resolves the room/device embedded in a line-in URI's uid -- which isn't
    necessarily `coord` itself, since a room can play another room's shared
    line-in.

    Returns a dict always carrying 'state'. When something loggable is playing
    it also carries 'kind', 'identifier', 'detail' and 'fields' (kwargs for
    db.insert_play); those are absent for stopped/paused or blank streams
    with no distinguishable source (e.g. TV or AirPlay inputs).
    """
    info = coord.get_current_transport_info()
    state = info.get('current_transport_state', 'STOPPED')
    if state != 'PLAYING':
        return {'state': state}

    if coord.is_playing_radio:
        # Radio: identified by station so it dedups to one row per tune-in.
        station = coord.get_current_media_info().get('channel', '').strip()
        if not station:
            return {'state': state}
        return {
            'state': state,
            'kind': 'radio',
            'identifier': f"radio::{station}",
            'detail': f"📻 {station}",
            'fields': {'station': station},
        }

    track = coord.get_current_track_info()
    title = track.get('title', '').strip()
    artist = track.get('artist', '').strip()
    album = track.get('album', '').strip()
    uri = track.get('uri', '')
    if not title:
        line_in_match = _LINE_IN_RE.match(uri)
        if line_in_match:
            # Falls back to the source room's own name (e.g. a room playing
            # another room's shared line-in with no custom name set) when
            # _line_in_name() can't resolve a custom name, and further to the
            # raw uid if that room hasn't shown up in a full_sweep()
            # discovery yet.
            source_uid = line_in_match.group(1)
            source_zone = zone_by_uid.get(source_uid)
            source = (
                _line_in_name(source_zone, source_uid, uri)
                or (source_zone.player_name if source_zone else source_uid)
            )
            return {
                'state': state,
                'kind': 'line_in',
                'identifier': f"line_in::{source}",
                'detail': f"🔌 Line-In ({source})",
                'fields': {'source': source},
            }
        return {'state': state}  # blank metadata stream (TV input, AirPlay, transient)
    service = service_from_uri(coord, uri)
    detail = f"{artist} - {title} (Album: {album})"
    if service:
        detail += f" [{service}]"
    return {
        'state': state,
        'kind': 'track',
        'identifier': f"track::{artist} - {title}",
        'detail': detail,
        'fields': {'title': title, 'artist': artist, 'album': album, 'service': service},
    }

def log_coordinator(coord, last_played, coord_events, zone_by_uid):
    """Read a coordinator's playback and log it for every room in its group.

    Shared by the event handler and the poll fallback. Applies the same per-room
    dedup and a single shared event_id, so a grouped play still counts once.
    """
    try:
        snap = coordinator_playback(coord, zone_by_uid)
        members = list(coord.group.members) if coord.group else [coord]
    except Exception:
        return  # speaker briefly unreachable; the next event/poll will retry

    if snap.get('kind'):
        # Mint a new event id when the coordinator's play changes; otherwise
        # reuse it so every member row shares one id.
        prev = coord_events.get(coord.uid)
        if not prev or prev['identifier'] != snap['identifier']:
            coord_events[coord.uid] = {
                'identifier': snap['identifier'],
                'event_id': f"{coord.uid}:{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            }
        event_id = coord_events[coord.uid]['event_id']
        for member in members:
            room = member.player_name
            if last_played.get(room) != snap['identifier']:
                db.insert_play(room, snap['kind'], event_id=event_id, **snap['fields'])
                log_line(room, snap['detail'])
                last_played[room] = snap['identifier']

    elif snap['state'] in ('STOPPED', 'PAUSED_PLAYBACK'):
        # Music stopped: clear each room so a replay logs immediately.
        for member in members:
            last_played.pop(member.player_name, None)
        coord_events.pop(coord.uid, None)

def _refresh_zone_group_state(zones):
    """Force SoCo's shared zone-group-topology cache to reflect the live network.

    `.group`/`.all_zones` (used by coordinators() and everywhere below) go
    through soco's ZoneGroupState.poll(), which -- once we hold a
    zoneGroupTopology subscription -- assumes incoming events keep the cache
    warm and skips re-fetching, forever. That assumption only holds for
    soco.events_asyncio, whose notify() handler explicitly feeds an event's
    ZoneGroupState payload into process_payload(); the threaded soco.events
    module we use has no such wiring (Service._update_cache_on_event() is a
    no-op for ZoneGroupTopology), so the cache silently freezes at whatever
    grouping was live when the subscription was created and never reflects
    later regroupings. Fetching GetZoneGroupState() and feeding it to
    process_payload() directly -- bypassing poll()'s subscription
    short-circuit -- keeps it current. The cache is a single shared instance
    per household (see SoCo.zone_group_state), so any one zone can refresh it
    for all of them.
    """
    zone = next(iter(zones), None)
    if zone is None:
        return
    try:
        zgs = zone.zoneGroupTopology.GetZoneGroupState()['ZoneGroupState']
        zone.zone_group_state.process_payload(payload=zgs, source='poll', source_ip=zone.ip_address)
    except Exception:
        pass

def coordinators(zones):
    """Map uid -> coordinator SoCo for the groups present in `zones`."""
    coords = {}
    for zone in zones:
        coord = zone.group.coordinator if zone.group else zone
        if coord is not None:
            coords[coord.uid] = coord
    return coords

def snapshot_status(zones):
    """Best-effort live status for every currently discovered room, for the
    status panel. Each field is independently best-effort: a speaker that
    fails one call (e.g. no battery) still reports the rest.
    """
    # Transport state is a group-level property: a non-coordinator member's
    # own get_current_transport_info() is unreliable (same reason
    # coordinator_playback() only ever reads the coordinator for metadata --
    # see CLAUDE.md). Fetch it once per coordinator and share it across every
    # room in that group, rather than asking each zone individually.
    coord_states = {}
    for coord in coordinators(zones).values():
        try:
            coord_states[coord.uid] = coord.get_current_transport_info().get('current_transport_state', 'UNKNOWN')
        except Exception:
            coord_states[coord.uid] = 'UNKNOWN'

    rows = []
    for zone in zones:
        coord = zone.group.coordinator if zone.group else zone
        state = coord_states.get(coord.uid, 'UNKNOWN')
        try:
            volume = zone.volume
        except Exception:
            volume = None
        try:
            mute = int(zone.mute)
        except Exception:
            mute = None
        try:
            group_label = zone.group.short_label if zone.group else zone.player_name
        except Exception:
            group_label = zone.player_name
        try:
            # Only Sonos Move/Roam report this; everything else raises.
            battery_level = zone.get_battery_info(timeout=3).get('Level')
        except Exception:
            battery_level = None
        rows.append({
            'room': zone.player_name,
            'state': state,
            'volume': volume,
            'mute': mute,
            'group_label': group_label,
            'battery_level': battery_level,
        })
    return rows

def resync_subscriptions(zones, av_subs, topo, event_q):
    """Reconcile UPnP subscriptions with the speakers currently on the network.

    Subscribes new coordinators' avTransport, drops departed ones, and keeps one
    zoneGroupTopology subscription alive. Callbacks run in the event listener
    thread and only enqueue a marker; all DB work stays on the main thread.
    `topo` is a mutable holder: {'sub': Subscription|None, 'uid': str|None}.
    """
    if not zones:
        return  # transient empty discovery -- keep existing subscriptions

    coords = coordinators(zones)

    def av_callback(event):
        try:
            event_q.put(('av', event.service.soco))
        except Exception:
            event_q.put(('resync', None))

    def renew_failed(_exc):
        event_q.put(('resync', None))

    # Subscribe coordinators we're not yet watching.
    for uid, coord in coords.items():
        if uid not in av_subs:
            try:
                sub = coord.avTransport.subscribe(requested_timeout=SUB_TIMEOUT, auto_renew=True)
                sub.callback = av_callback
                sub.auto_renew_fail = renew_failed
                av_subs[uid] = sub
            except Exception:
                pass

    # Drop coordinators that have gone away.
    for uid in list(av_subs):
        if uid not in coords:
            try:
                av_subs[uid].unsubscribe()
            except Exception:
                pass
            del av_subs[uid]

    # Keep exactly one topology subscription; move it if its zone left.
    if topo['uid'] not in coords:
        if topo['sub'] is not None:
            try:
                topo['sub'].unsubscribe()
            except Exception:
                pass
            topo['sub'], topo['uid'] = None, None
        for uid, coord in coords.items():
            try:
                tsub = coord.zoneGroupTopology.subscribe(requested_timeout=SUB_TIMEOUT, auto_renew=True)
                tsub.callback = lambda event: event_q.put(('topology', None))
                topo['sub'], topo['uid'] = tsub, uid
                break
            except Exception:
                continue

def main():
    print("🚀 Initialising Sonos S1 History Logger (event-driven)...")
    print(f"🗄️  Logging plays to:   {db.DB_PATH}")
    print(f"📡 Listening for Sonos events on port {soco.config.EVENT_LISTENER_PORT}; "
          f"polling every {POLL_FALLBACK}s as a fallback.")
    print("Press Ctrl+C to stop the program.\n")

    db.init_db()

    # Dedup identifier per room ("track::<artist> - <title>" / "radio::<station>" /
    # "line_in::<source room>"), and per-coordinator current play so all rooms in
    # a group share one event_id (a grouped play then counts once under
    # COUNT(DISTINCT event_id)).
    last_played = {}
    coord_events = {}

    # uid -> SoCo for every speaker seen in a discovery, refreshed each
    # full_sweep() below. Never cleared on a blip (same reasoning as db.sync_status
    # keeping a room's last-known info) -- lets a line-in URI's uid resolve to its
    # room/device even between sweeps.
    zone_by_uid = {}

    # UPnP subscriptions and the cross-thread event queue. Callbacks (listener
    # thread) only enqueue markers; this thread does all discovery and DB writes.
    av_subs = {}
    topo = {'sub': None, 'uid': None}
    event_q = queue.Queue()

    def full_sweep():
        """Rediscover, reconcile subscriptions, log every group's state, and
        refresh the live-status snapshot (skipped on an empty discovery, same
        as resync_subscriptions -- a transient blip shouldn't mark every room
        offline).
        """
        zones = soco.discover(timeout=DISCOVER_TIMEOUT) or set()
        zone_by_uid.update({zone.uid: zone for zone in zones})
        _refresh_zone_group_state(zones)
        resync_subscriptions(zones, av_subs, topo, event_q)
        for coord in coordinators(zones).values():
            log_coordinator(coord, last_played, coord_events, zone_by_uid)
        if zones:
            db.sync_status(snapshot_status(zones))
        return zones

    try:
        # Initial sweep: subscribe and capture whatever is already playing.
        zones = full_sweep()
        if not zones:
            print("⚠️  No Sonos devices found yet; will keep retrying every "
                  f"{POLL_FALLBACK}s.")
        else:
            print(f"👂 Subscribed to {len(av_subs)} group coordinator(s); waiting for events.")

        while True:
            try:
                # Block on the event queue; a timeout is the poll-fallback trigger.
                kind, payload = event_q.get(timeout=POLL_FALLBACK)
            except queue.Empty:
                kind, payload = 'poll', None

            if kind == 'av':
                # One coordinator changed -- re-read just that group. Normalise in
                # case regrouping made the emitting device a member.
                coord = payload.group.coordinator if payload.group else payload
                log_coordinator(coord, last_played, coord_events, zone_by_uid)
            else:
                # 'topology' (grouping changed), 'resync' (a renewal failed), or
                # 'poll' (fallback tick): rediscover, fix subscriptions, sweep all.
                full_sweep()

    except KeyboardInterrupt:
        print("\n👋 Stopping Sonos logger. Goodbye!")
    finally:
        # Best-effort cleanup so speakers stop sending to a dead listener.
        for sub in list(av_subs.values()):
            try:
                sub.unsubscribe()
            except Exception:
                pass
        if topo['sub'] is not None:
            try:
                topo['sub'].unsubscribe()
            except Exception:
                pass
        try:
            event_listener.stop()
        except Exception:
            pass

if __name__ == "__main__":
    main()

