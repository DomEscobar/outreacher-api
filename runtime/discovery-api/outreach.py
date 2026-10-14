"""Outreach workflow: connect first, research and write only after the connection is accepted.

  POST /api/v1/outreach            create a contact; returns a short connection note to send MANUALLY
  (background, daily)              read-only check in the logged-in browser whether the connection
                                   was accepted (background tab, no clicks, daily cap, jitter)
  (background, after acceptance)   research the company for the requested topic (website pages,
                                   verbatim quotes) and draft the message following the template
  GET  /api/v1/outreach/{id}       status, findings with sources, message draft
  GET  /api/v1/outreach?status=…   list contacts
  POST /api/v1/outreach/{id}/accepted   start research now (manual override)
  DELETE /api/v1/outreach/{id}

Nothing is ever sent, typed or clicked on LinkedIn by this module. Texts are drafts for a human.
"""
from contextlib import contextmanager
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit

from pydantic import Field, field_validator, model_validator

from models import Strict, Text, profile_key, profile_url, public_url
import search_backend as sb

LongText = Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
Topic = Annotated[str, Field(strict=True, min_length=2, max_length=300, pattern=r'^[^\x00-\x1f\x7f]+$')]
Status = Literal['awaiting_connection', 'connected', 'researching', 'ready', 'sent', 'failed', 'expired', 'do_not_contact']
ConnectionState = Literal['unknown', 'pending', 'not_connected', 'connected', 'login_required']

CHECK_INTERVAL_HOURS = float(os.environ.get('OUTREACH_CHECK_HOURS', '24'))
DAILY_CHECK_CAP = int(os.environ.get('OUTREACH_DAILY_CHECKS', '40'))
EXPIRE_DAYS = float(os.environ.get('OUTREACH_EXPIRE_DAYS', '30'))
LOGIN_PAUSE_HOURS = 6
COMMON_PATHS = ('leistungen', 'produkte', 'ueber-uns', 'unternehmen', 'team', 'karriere')  # tried when the extract has no links
# Sub-pages worth reading for any kind of company (offer, about, people, hiring, news).
PAGE_HINTS = ('leistung', 'produkt', 'loesung', 'lösung', 'service', 'angebot', 'kompetenz', 'schwerpunkt', 'branche',
              'referenz', 'kunden', 'case', 'ueber', 'über', 'about', 'unternehmen', 'firma', 'kanzlei', 'praxis', 'team',
              'karriere', 'jobs', 'stellen', 'news', 'aktuell', 'presse', 'blog')


# ---------------------------------------------------------------------------- API models

class OutreachBudget(Strict):
    max_cost_usd: float = Field(default=0.15, gt=0, le=2)


class OutreachRequest(Strict):
    linkedin_url: str = Field(max_length=2048)
    company_website: str | None = Field(default=None, max_length=2048)
    company_name: Text | None = None
    topic: Topic
    template: LongText | None = None  # guidance for the message after acceptance (tone, structure, offer, CTA)
    note_template: LongText | None = None  # optional guidance for the connection note
    connection_note: bool = True
    note_max_chars: int = Field(default=200, ge=50, le=300)
    message_max_chars: int = Field(default=1200, ge=200, le=4000)
    language: Literal['de', 'en'] = 'de'
    allow_same_company: bool = False  # create even if another person at this company is already in outreach
    budget: OutreachBudget = Field(default_factory=OutreachBudget)

    @field_validator('linkedin_url')
    @classmethod
    def canonical_profile(cls, value):
        canonical = sb.linkedin_profile(value)
        if not canonical: raise ValueError('LinkedIn profile URL (/in/…) required')
        return profile_url(canonical)

    @field_validator('company_website')
    @classmethod
    def website(cls, value):
        return public_url(value) if value else value

    @model_validator(mode='after')
    def company(self):
        if not (self.company_website or self.company_name):
            raise ValueError('company_website or company_name required')
        return self


class Finding(Strict):
    need: Text            # why the company could need the topic
    where: Text           # where exactly (business area, process, team, location)
    quote: Annotated[str, Field(min_length=8, max_length=600)]  # verbatim text from the source page
    url: str = Field(max_length=2048)


class Connection(Strict):
    state: ConnectionState = 'unknown'
    checks: int = 0
    last_checked_at: float | None = None
    next_check_at: float | None = None


class OutreachRecord(Strict):
    id: str
    status: Status
    created_at: float
    updated_at: float
    linkedin_url: str
    name: str | None = None
    headline: str | None = None
    company_website: str | None = None
    company_name: str | None = None
    topic: str
    connection_note: str | None = None
    connection: Connection = Field(default_factory=Connection)
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    sources: list[str] = Field(default_factory=list, max_length=10)
    message: str | None = None
    message_is_draft: Literal[True] = True
    sent_by_system: Literal[False] = False
    cost_usd: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------- persistence

class OutreachStore:
    """Private SQLite (0600) next to the discovery DB; request (incl. template) and record as JSON."""
    def __init__(self, directory, retention_days=180):
        self.path = os.path.join(str(directory), 'outreach.sqlite3')
        self.retention = retention_days * 86400
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS outreach(id TEXT PRIMARY KEY, profile TEXT UNIQUE NOT NULL, '
                       'status TEXT NOT NULL, next_check REAL, updated REAL NOT NULL, request TEXT NOT NULL, record TEXT NOT NULL)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        # sqlite3's own context manager does not close; every connection here is closed explicitly.
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA secure_delete=ON')
            yield db
        finally:
            db.close()

    def create(self, request, record):
        with self.lock, self.connect() as db:
            existing = db.execute('SELECT id FROM outreach WHERE profile=?', (profile_key(request.linkedin_url),)).fetchone()
            if existing: return existing[0], False
            db.execute('INSERT INTO outreach VALUES(?,?,?,?,?,?,?)', (record['id'], profile_key(request.linkedin_url),
                       record['status'], record['connection']['next_check_at'], record['updated_at'],
                       request.model_dump_json(), json.dumps(record)))
            return record['id'], True

    def find(self, linkedin_url):
        with self.connect() as db:
            row = db.execute('SELECT id FROM outreach WHERE profile=?', (profile_key(linkedin_url),)).fetchone()
        return row[0] if row else None

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT request, record FROM outreach WHERE id=?', (ident,)).fetchone()
        return (OutreachRequest.model_validate_json(row[0]), json.loads(row[1])) if row else (None, None)

    def save(self, record):
        record['updated_at'] = time.time()
        OutreachRecord.model_validate(record)  # never persist a record that breaks the contract
        with self.lock, self.connect() as db:
            db.execute('UPDATE outreach SET status=?, next_check=?, updated=?, record=? WHERE id=?',
                       (record['status'], record['connection'].get('next_check_at'), record['updated_at'],
                        json.dumps(record), record['id']))

    def list(self, status=None, limit=200):
        with self.connect() as db:
            rows = db.execute('SELECT record FROM outreach' + (' WHERE status=?' if status else '') +
                              ' ORDER BY updated DESC LIMIT ?', ((status, limit) if status else (limit,))).fetchall()
        return [json.loads(r[0]) for r in rows]

    def due_checks(self, now, limit):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM outreach WHERE status='awaiting_connection' AND next_check<=? "
                              'ORDER BY next_check LIMIT ?', (now, limit)).fetchall()
        return [r[0] for r in rows]

    def with_status(self, status, limit=5):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT id FROM outreach WHERE status=? ORDER BY updated LIMIT ?', (status, limit))]

    def checks_since(self, since):
        return sum(1 for r in self.list(limit=10000) if (r['connection'].get('last_checked_at') or 0) >= since)

    def delete(self, ident):
        with self.lock, self.connect() as db:
            return db.execute('DELETE FROM outreach WHERE id=?', (ident,)).rowcount > 0

    def cleanup(self):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM outreach WHERE updated < ?', (time.time() - self.retention,))


# ---------------------------------------------------------------------------- connection state (read-only)

def classify_connection(text, url):
    """Map the visible profile top card (DE/EN UI) to a connection state. Order matters."""
    path = urlsplit(url or '').path
    if any(p in path for p in ('/login', '/authwall', '/checkpoint', '/uas/')): return 'login_required'
    t = sb.norm(text)[:4000]
    if re.search(r'(?<!\w)(ausstehend|pending)(?!\w)', t): return 'pending'
    if re.search(r'·\s*(1st|1\.)(?!\w)|1st degree connection|kontakt 1\. grades', t): return 'connected'
    if re.search(r'·\s*(2nd|3rd|3rd\+|2\.|3\.)|(?<!\w)(vernetzen|connect)(?!\w)', t): return 'not_connected'
    return 'unknown'


class BrowserProfileReader:
    """Opens the profile in a BACKGROUND tab of the shared Chromium via CDP, reads the top card text,
    closes the tab. No clicks, no typing, no scrolling beyond page load."""
    def __init__(self, cdp='http://127.0.0.1:9222', timeout=25):
        self.cdp, self.timeout = cdp, timeout

    def read(self, url):
        import urllib.request
        from websockets.sync.client import connect
        version = json.load(urllib.request.urlopen(self.cdp + '/json/version', timeout=5))
        ids = iter(range(1, 10_000))
        with connect(version['webSocketDebuggerUrl'], max_size=8_000_000, open_timeout=10) as ws:
            def send(method, params=None, session=None):
                ident = next(ids)
                message = {'id': ident, 'method': method, 'params': params or {}}
                if session: message['sessionId'] = session
                ws.send(json.dumps(message))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    reply = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                    if reply.get('id') == ident:
                        if 'error' in reply: raise RuntimeError('cdp_error')
                        return reply.get('result', {})
                raise TimeoutError('cdp_timeout')
            target = send('Target.createTarget', {'url': 'about:blank', 'background': True})['targetId']
            try:
                session = send('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
                send('Page.navigate', {'url': url}, session)
                text, href = '', url
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    time.sleep(1.5)
                    value = send('Runtime.evaluate', {'returnByValue': True, 'expression':
                        '({href: location.href, text: ((document.querySelector("main section") || document.body || {}).innerText || "").slice(0, 4000)})'},
                        session).get('result', {}).get('value') or {}
                    text, href = value.get('text') or '', value.get('href') or url
                    if classify_connection(text, href) != 'unknown' and len(text) > 80: break
                return text, href
            finally:
                send('Target.closeTarget', {'targetId': target})


# ---------------------------------------------------------------------------- research + writing

def lang_name(code):
    return 'German (Sie-Form)' if code == 'de' else 'English'


class OutreachWriter:
    """Profile lookup, connection note and post-acceptance research; providers are treg + OpenRouter."""
    def __init__(self, providers_factory=None):
        self.providers_factory = providers_factory or (lambda meter: sb.Providers(
            meter, sb.env_value('TREG_TOKEN'), sb.openrouter_key(), sb.env_value('DISCOVERY_SEARCH_MODEL') or None))

    def meter(self, request, seconds=240):
        return sb.Meter(request.budget.max_cost_usd, time.monotonic() + seconds, lambda: False)

    def profile(self, p, request):
        slug = urlsplit(request.linkedin_url).path.rstrip('/').split('/')[-1]
        for r in p.serp(f'site:linkedin.com/in {slug}', 5):
            if sb.linkedin_profile(r['url']) and profile_key(sb.linkedin_profile(r['url'])) == profile_key(request.linkedin_url):
                title = r['title']
                return sb.sanitize(re.split(r'\s[–—-]\s', title)[0]), sb.sanitize(r['description'] or title, 300)
        return None, None

    def note(self, request):
        """Name/headline from the search index + a short note (or none). Returns (fields, cost_usd)."""
        meter = self.meter(request, 90)
        p = self.providers_factory(meter)
        name, headline = self.profile(p, request)
        fields = {'name': name, 'headline': headline, 'connection_note': None}
        if request.connection_note:
            o = p.llm(f'Write a LinkedIn connection request note in {lang_name(request.language)}, at most '
                      f'{request.note_max_chars} characters. Person: {name or "unknown name"}; headline: {headline or "unknown"}; '
                      f'firm: {request.company_name or request.company_website}. Context topic (do NOT pitch it yet): '
                      f'{request.topic}. Be brief, personal, no sales pitch, no links, no invented facts. '
                      f'Guidance from the sender (follow it): {request.note_template or "none"}. '
                      'Return {"note": str}.')
            note = (o.get('note') or '').strip()
            fields['connection_note'] = note[:request.note_max_chars].rstrip() or None
        return fields, meter.cost_micro / 1_000_000

    def site_pages(self, p, request, headline):
        website = request.company_website
        if not website:
            for r in p.serp(f'{request.company_name} {headline or ""}'.strip(), 8):
                host = (urlsplit(r['url']).hostname or '').casefold()
                if r['url'].startswith('https://') and not any(s in host for s in sb.SKIP_HOSTS):
                    website = f'https://{urlsplit(r["url"]).hostname}/'; break
        if not website: return None, []
        home = p.extract(website)
        pages = [(website, home[:6000])] if home else []
        host = urlsplit(website).hostname
        links = []
        for text, href in re.findall(r'\[([^\]]{1,80})\]\(([^)\s]+)\)', home or ''):
            url = urljoin(website, href)
            if urlsplit(url).hostname == host and url.startswith('https://') and any(h in sb.norm(text + ' ' + url) for h in PAGE_HINTS):
                if url.rstrip('/') != website.rstrip('/') and url not in links: links.append(url)
        if len(links) < 2:  # some extractors return plain text without links
            links += [urljoin(website, path) for path in COMMON_PATHS if urljoin(website, path) not in links]
        for url in links:
            if len(pages) >= 5: break
            md = p.extract(url)
            if md and md[:6000] not in (text for _, text in pages): pages.append((url, md[:6000]))
        return website, pages

    def research(self, request, record):
        """After acceptance: findings with verbatim evidence + message draft. Returns updated fields, cost."""
        meter = self.meter(request)
        p = self.providers_factory(meter)
        website, pages = self.site_pages(p, request, record.get('headline'))
        corpus = {u: t for u, t in pages}
        sources = '\n\n'.join(f'### SOURCE {u}\n{t}' for u, t in pages) or '(no website text found)'
        o = p.llm(
            f'You prepare a personal LinkedIn message after a connection was accepted. Language: {lang_name(request.language)}. '
            f'Recipient: {record.get("name") or "unknown"} ({record.get("headline") or "no headline"}) at '
            f'{request.company_name or website}. Topic we offer: {request.topic}.\n'
            'Step 1: from the SOURCES only, find up to 5 concrete reasons why this company could need the topic and WHERE '
            '(business area, product, process, team, location, hiring need). Each finding needs an exact quote (verbatim substring '
            'of one source) and that source URL; "need" and "where" at most 120 characters each. No quote, no finding. '
            'Never invent facts.\n'
            f'Step 2: write the message (max {request.message_max_chars} characters) using only those findings; no '
            'invented numbers, clients or claims; no links unless the guidance asks for one.\n'
            f'SENDER GUIDANCE / TEMPLATE (follow structure and tone): {request.template or "short, personal, one clear question at the end"}\n'
            'Return {"findings": [{"need": str, "where": str, "quote": str, "url": str}], "message": str}.\n\nSOURCES:\n' + sources)
        findings = []
        for f in o.get('findings') or []:
            url, quote = f.get('url'), (f.get('quote') or '').strip()
            text = corpus.get(url)
            if not (text and sb.verbatim(quote, text)): continue  # unsupported findings are dropped
            exact = sb.source_quote(quote, text, max_len=600) or quote
            try:
                findings.append(Finding.model_validate({'need': sb.sanitize(f.get('need')) or '-', 'where': sb.sanitize(f.get('where')) or '-',
                                                        'quote': exact[:600], 'url': url}).model_dump())
            except ValueError:
                continue
        message = (o.get('message') or '').strip()[:request.message_max_chars].rstrip() or None
        return {'company_website': website, 'findings': findings[:8], 'sources': [u for u, _ in pages][:10], 'message': message,
                'error': None if findings else 'research_thin'}, meter.cost_micro / 1_000_000


# ---------------------------------------------------------------------------- service + background loop

class OutreachConflict(Exception):
    """The shared registry says no (person/company blocked or already contacted elsewhere)."""
    def __init__(self, code, registry_hit):
        super().__init__(code)
        self.code, self.registry_hit = code, registry_hit


class OutreachService:
    def __init__(self, store, writer=None, reader=None, browser_lock=None, start=True, registry=None):
        self.store, self.writer, self.registry = store, writer or OutreachWriter(), registry
        self.reader, self.browser_lock = reader or BrowserProfileReader(), browser_lock or threading.Lock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.paused_until = 0.0
        self.spacing = (20, 60)  # seconds between two profile views
        self.thread = threading.Thread(target=self.loop, name='outreach-worker', daemon=True)
        if start and os.environ.get('OUTREACH_BACKGROUND', '1') != '0': self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread.is_alive(): self.thread.join(timeout=8)

    def registry_ref(self, record_or_request):
        get = (lambda k: getattr(record_or_request, k, None)) if not isinstance(record_or_request, dict) else record_or_request.get
        return {'linkedin_url': get('linkedin_url'), 'company_website': get('company_website'), 'company_name': get('company_name')}

    def registry_set(self, record, status):
        if self.registry: self.registry.upsert({**self.registry_ref(record), 'status': status, 'source': 'outreach'})

    def registry_gate(self, request):
        if not self.registry: return
        from registry import HARD_BLOCK
        hit = self.registry.check(self.registry_ref(request))
        person, company = hit['person'], set((hit['company'] or {}).get('statuses') or {})
        if person and person['status'] in HARD_BLOCK: raise OutreachConflict('person_' + person['status'], hit)
        if person and person['status'] in ('in_outreach', 'contacted') and person['source'] != 'outreach':
            raise OutreachConflict('person_in_registry', hit)  # another agent already works on this person
        if company & HARD_BLOCK: raise OutreachConflict('company_blocked', hit)
        if company & {'in_outreach', 'contacted'} and not request.allow_same_company:
            raise OutreachConflict('company_in_outreach', hit)

    def create(self, request):
        existing = self.store.find(request.linkedin_url)
        if existing: return existing, False  # no second note, no second charge
        self.registry_gate(request)  # before any paid call
        now = time.time()
        record = {'id': secrets.token_urlsafe(24), 'status': 'awaiting_connection', 'created_at': now, 'updated_at': now,
                  'linkedin_url': request.linkedin_url, 'company_website': request.company_website,
                  'company_name': request.company_name, 'topic': request.topic,
                  'connection': {'state': 'unknown', 'checks': 0, 'last_checked_at': None,
                                 'next_check_at': now + CHECK_INTERVAL_HOURS * 3600},
                  'cost_usd': 0.0, 'error': None}
        try:
            fields, cost = self.writer.note(request)
            record.update(fields); record['cost_usd'] += cost
        except (sb.SearchError, sb.Stop) as exc:
            record['error'] = getattr(exc, 'code', None) or getattr(exc, 'reason', 'note_failed')
        OutreachRecord.model_validate(record)
        ident, created = self.store.create(request, record)
        if created: self.registry_set(record, 'in_outreach')
        return ident, created

    def mark_do_not_contact(self, ident):
        """Permanent opt-out: no checks, no research; the registry blocks the person for every agent."""
        _, record = self.store.get(ident)
        if not record: return None
        record['status'] = 'do_not_contact'
        record['connection']['next_check_at'] = None
        self.store.save(record)
        self.registry_set(record, 'do_not_contact')
        return record

    def delete(self, ident):
        _, record = self.store.get(ident)
        if record and self.registry:  # a mistaken entry should not block the person; history (contacted, opt-out) stays
            self.registry.remove_person(record['linkedin_url'], only_status='in_outreach', only_source='outreach')
        return self.store.delete(ident)

    def mark_accepted(self, ident):
        request, record = self.store.get(ident)
        if not record: return None
        if record['status'] in ('awaiting_connection', 'expired', 'failed'):
            record['status'] = 'connected'
            record['connection']['next_check_at'] = None
            self.store.save(record)
            self.wake.set()
        return record

    def mark_sent(self, ident):
        """The human sent the message (outside this system); stop showing it as ready."""
        _, record = self.store.get(ident)
        if not record: return None
        record['status'] = 'sent'
        record['connection']['next_check_at'] = None
        self.store.save(record)
        self.registry_set(record, 'contacted')
        return record

    # -- background work
    def loop(self):
        while not self.stop.is_set():
            try:
                self.store.cleanup()
                worked = self.research_one() or self.check_one()
            except Exception:
                worked = False  # never log lead/model data
            if not worked:
                self.wake.wait(timeout=60)
                self.wake.clear()

    def research_one(self):
        ids = self.store.with_status('connected', 1)
        if not ids: return False
        request, record = self.store.get(ids[0])
        record['status'] = 'researching'; self.store.save(record)
        try:
            fields, cost = self.writer.research(request, record)
            record.update(fields); record['cost_usd'] = round(record['cost_usd'] + cost, 6)
            record['status'] = 'ready' if record.get('message') else 'failed'
            if not record.get('message'): record['error'] = 'message_failed'
        except (sb.SearchError, sb.Stop) as exc:
            record['status'], record['error'] = 'failed', getattr(exc, 'code', None) or getattr(exc, 'reason', 'research_failed')
        self.store.save(record)
        return True

    def check_one(self):
        now = time.time()
        if now < self.paused_until: return False
        if self.store.checks_since(now - 86400) >= DAILY_CHECK_CAP: return False
        ids = self.store.due_checks(now, 1)
        if not ids: return False
        _, record = self.store.get(ids[0])
        conn = record['connection']
        if now - record['created_at'] > EXPIRE_DAYS * 86400:
            record['status'], conn['next_check_at'] = 'expired', None
            self.store.save(record); self.registry_set(record, 'expired'); return True
        with self.browser_lock:  # never in parallel with a browser-agent discovery job
            try:
                text, href = self.reader.read(record['linkedin_url'])
                state = classify_connection(text, href)
            except Exception:
                state = 'unknown'
        conn.update(state=state, checks=conn['checks'] + 1, last_checked_at=now,
                    next_check_at=now + CHECK_INTERVAL_HOURS * 3600 * random.uniform(0.9, 1.2))
        if state == 'connected':
            record['status'], conn['next_check_at'] = 'connected', None
            self.wake.set()
        elif state == 'login_required':
            self.paused_until = now + LOGIN_PAUSE_HOURS * 3600  # a human must log in via noVNC
        self.store.save(record)
        self.stop.wait(random.uniform(*self.spacing))
        return True
