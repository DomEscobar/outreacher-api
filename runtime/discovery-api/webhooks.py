"""Outbound webhooks: agents get pushed state changes instead of polling.

Delivery is signed (`X-Hermes-Signature: sha256=<hex>` over the raw body), retried with exponential
backoff, never follows redirects and never includes personal content — only ids, types, statuses and
timestamps. The secret is stored privately (0600 DB) and returned once at creation.
"""
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from events import events_since
from models import Strict, public_url

EventType = Literal['outreach', 'discovery', 'campaign', 'outreach.paused', 'outreach.resumed']
ALL_EVENTS = ('outreach', 'discovery', 'campaign', 'outreach.paused', 'outreach.resumed')
INTERVAL = float(os.environ.get('WEBHOOK_INTERVAL_SECONDS', '10'))


class WebhookRequest(Strict):
    url: str = Field(max_length=2048)
    secret: str | None = Field(default=None, min_length=16, max_length=128)
    events: list[EventType] = Field(default_factory=lambda: list(ALL_EVENTS), min_length=1, max_length=5)

    @field_validator('url')
    @classmethod
    def https_endpoint(cls, value):
        return public_url(value)


class WebhookView(Strict):
    id: str
    url: str
    events: list[EventType]
    active: bool
    created_at: float
    updated_at: float
    failures: int = 0
    last_error: str | None = None


class WebhookCreated(WebhookView):
    secret: str  # returned once; store it on the receiving side


def _public_address(url):
    """Resolve the endpoint and refuse private/loopback targets (checked again at delivery)."""
    host = urlsplit(url).hostname
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('unresolvable_webhook_host') from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global: raise ValueError('private_webhook_address')
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def send_webhook(url, body, secret, timeout=10):
    _public_address(url)  # blocks SSRF at send time as well
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'User-Agent': 'hermes-outreach-webhook/1',
        'X-Hermes-Signature': 'sha256=' + signature})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        if not 200 <= response.status < 300: raise OSError('webhook_http_%d' % response.status)


def matches(types, event):
    kind = event['type']
    return kind in types or (kind.startswith('outreach.') and 'outreach' in types)


class WebhookStore:
    def __init__(self, directory):
        self.path = os.path.join(str(directory), 'webhooks.sqlite3')
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS webhooks(id TEXT PRIMARY KEY, url TEXT NOT NULL, secret TEXT NOT NULL, '
                       'events TEXT NOT NULL, active INTEGER NOT NULL, cursor REAL NOT NULL, failures INTEGER NOT NULL, '
                       'next_at REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, last_error TEXT)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA secure_delete=ON')
            yield db
        finally:
            db.close()

    def create(self, request):
        now = time.time()
        record = {'id': secrets.token_urlsafe(18), 'url': request.url, 'secret': request.secret or secrets.token_urlsafe(32),
                  'events': list(request.events), 'active': 1, 'cursor': 0.0, 'failures': 0, 'next_at': now,
                  'created_at': now, 'updated_at': now, 'last_error': None}
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO webhooks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (record['id'], record['url'], record['secret'], json.dumps(record['events']), 1, 0.0, 0, now,
                        now, now, None))
        return self.get(record['id'])

    def _row(self, row):
        record = dict(row)
        record['events'] = json.loads(record['events'])
        record['active'] = bool(record['active'])
        record['has_secret'] = bool(record['secret'])
        record['created_at'], record['updated_at'] = record.pop('created'), record.pop('updated')
        return record

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM webhooks WHERE id=?', (ident,)).fetchone()
        return self._row(row) if row else None

    def list(self, active_only=False):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM webhooks' + (' WHERE active=1' if active_only else '') +
                              ' ORDER BY created').fetchall()
        return [self._row(r) for r in rows]

    def save(self, record):
        record['updated_at'] = time.time()
        with self.lock, self.connect() as db:
            db.execute('UPDATE webhooks SET cursor=?, failures=?, next_at=?, updated=?, last_error=? WHERE id=?',
                       (record['cursor'], record['failures'], record['next_at'], record['updated_at'],
                        record.get('last_error'), record['id']))

    def delete(self, ident):
        with self.lock, self.connect() as db:
            return db.execute('DELETE FROM webhooks WHERE id=?', (ident,)).rowcount > 0


def view(record):
    return {'id': record['id'], 'url': record['url'], 'events': record['events'], 'active': record['active'],
            'created_at': record['created_at'], 'updated_at': record['updated_at'], 'failures': record['failures'],
            'last_error': record.get('last_error')}


class WebhookDispatcher:
    def __init__(self, store, outreach_service, discovery_store, campaign_store, start=True, sender=None):
        self.store, self.outreach, self.discovery_store, self.campaign_store = store, outreach_service, discovery_store, campaign_store
        self.sender = sender or send_webhook
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.loop, name='webhook-worker', daemon=True)
        if start and os.environ.get('OUTREACH_BACKGROUND', '1') != '0': self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread.is_alive(): self.thread.join(timeout=8)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.dispatch_once()
            except Exception:
                pass
            self.wake.wait(timeout=INTERVAL)
            self.wake.clear()

    def dispatch_once(self, now=None):
        now = now or time.time()
        hooks = self.store.list(active_only=True)
        delivered = 0
        for hook in hooks:
            if hook['next_at'] > now: continue
            # Per-hook cursor: events a filter does not match still advance it, so one hook cannot pin
            # the shared cursor and starve the others.
            events, _ = events_since(self.outreach, self.discovery_store, self.campaign_store,
                                     since=hook['cursor'], limit=100)
            due = [e for e in events if matches(hook['events'], e)][:100]
            if not due:
                if events:
                    hook['cursor'] = events[-1]['updated_at']
                    self.store.save(hook)
                continue
            body = json.dumps({'events': due}, separators=(',', ':')).encode()
            try:
                self.sender(hook['url'], body, hook['secret'])
            except Exception as exc:
                hook['failures'] += 1
                hook['next_at'] = now + min(3600.0, 30.0 * (2 ** min(hook['failures'], 7)))
                hook['last_error'] = type(exc).__name__
                self.store.save(hook)
                continue
            hook['cursor'], hook['failures'], hook['next_at'], hook['last_error'] = due[-1]['updated_at'], 0, now + INTERVAL, None
            self.store.save(hook)
            delivered += len(due)
        return delivered
