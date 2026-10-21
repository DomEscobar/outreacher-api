"""Change feed for agents: events derived from the existing stores, never personal content.

A cursor is the `updated_at` timestamp of the last event a client has seen. Events are returned with
`updated_at > since`, so the boundary item is not repeated. Only ids, types, statuses and timestamps are
exposed (plus `campaign_id`, `connect`/`connection` state and an error code), never names, URLs or text.
"""
import time

CONTROL_EVENTS = ('outreach.paused', 'outreach.resumed')


def outreach_events(store, since):
    for r in store.list(limit=10000):
        ts = r.get('updated_at') or 0
        if ts <= since: continue
        connect = r.get('connect') or {}
        connection = r.get('connection') or {}
        yield {'type': 'outreach', 'ref_id': r['id'], 'status': r['status'], 'updated_at': ts,
               'campaign_id': r.get('campaign_id'), 'connect': connect.get('state'),
               'connection': connection.get('state'), 'error': r.get('error')}


def discovery_events(store, since):
    for j in store.list(limit=10000):
        ts = j.get('updated') or 0
        if ts <= since: continue
        yield {'type': 'discovery', 'ref_id': j['id'], 'status': j['status'], 'updated_at': ts,
               'error': j.get('error')}


def campaign_events(store, since):
    for c in store.list(limit=10000):
        ts = c.get('updated_at') or 0
        if ts <= since: continue
        yield {'type': 'campaign', 'ref_id': c['id'], 'status': c['status'], 'updated_at': ts,
               'error': c.get('error')}


def control_events(service, since):
    store = service.store
    paused_at = store.meta('paused_at') or 0
    if paused_at > since and service.paused_until > time.time():
        yield {'type': 'outreach.paused', 'ref_id': 'outreach', 'status': 'paused', 'updated_at': paused_at,
               'until': service.paused_until, 'error': None}
    resumed_at = store.meta('resumed_at') or 0
    if resumed_at > since:
        yield {'type': 'outreach.resumed', 'ref_id': 'outreach', 'status': 'active', 'updated_at': resumed_at,
               'error': None}


def events_since(service, discovery_store, campaign_store, since=0.0, limit=500):
    """All events newer than `since`, oldest first, and the cursor to use next."""
    items = []
    items.extend(control_events(service, since))
    items.extend(outreach_events(service.store, since))
    items.extend(discovery_events(discovery_store, since))
    if campaign_store: items.extend(campaign_events(campaign_store, since))
    items.sort(key=lambda e: (e['updated_at'], e['type'], e['ref_id']))
    page = items[:limit]
    cursor = page[-1]['updated_at'] if page else since
    return page, cursor
