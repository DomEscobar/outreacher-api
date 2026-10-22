"""Outreach workflow: connect, then research and write only after the connection is accepted.

  POST /api/v1/outreach            create a contact; by default the human sends the connection request
                                   on LinkedIn (UI: Leads -> "Vernetzen" opens the profile). With
                                   OUTREACH_AUTO_CONNECT=1 connect.py clicks "Vernetzen" instead.
  (background, ~3x a day)          read-only look at the sent-invitations list and the newest connections
                                   of the logged-in account (background tab, no clicks): listed under
                                   "sent" -> invitation pending, among the newest connections -> connected
  (background, after acceptance)   research the company for the requested topic (website pages,
                                   verbatim quotes) and draft the message following the template
  GET  /api/v1/outreach/{id}       status, findings with sources, message draft
  GET  /api/v1/outreach?status=…   list contacts
  POST /api/v1/outreach/sync-invitations   run the invitation look now (counts only)
  POST /api/v1/outreach/{id}/connect    retry a failed/disabled connection request now
  POST /api/v1/outreach/{id}/accepted   start research now (manual override)
  (background, at most twice a day while answers are awaited)   read-only look at the conversation list of
                                   the logged-in inbox (background tab, no thread opened, no clicks): a contact
                                   whose conversation ends with their message becomes "replied"
  POST /api/v1/outreach/{id}/replied | /no-reply   mark or undo an answer by hand
  POST /api/v1/outreach/resume     clear a persisted login/challenge pause after a human logged in
  DELETE /api/v1/outreach/{id}

The only external mutation is the opt-in connection request, sent by connect.py with a daily cap, irregular
spacing, a working-hours window and a hard stop on any challenge/rate-limit page (persisted pause).
Messages after acceptance remain drafts for a human. Nothing is typed into a message composer.
"""
from collections import Counter
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

from connect import BLOCK_TEXT, BrowserBlocked, LinkedInConnector, blocked_page, classify_connection, login_wall
from models import Strict, Text, profile_key, profile_url, public_url
import search_backend as sb

LongText = Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
Topic = Annotated[str, Field(strict=True, min_length=2, max_length=300, pattern=r'^[^\x00-\x1f\x7f]+$')]
Status = Literal['awaiting_connection', 'connected', 'researching', 'ready', 'sent', 'replied', 'failed', 'expired',
                 'do_not_contact']
ConnectionState = Literal['unknown', 'pending', 'not_connected', 'connected', 'login_required']

def active_window(value):
    """Parse "8-20" into (start, end) local hours; anything unusable means no restriction (0, 24)."""
    match = re.fullmatch(r'\s*(\d{1,2})\s*-\s*(\d{1,2})\s*', value or '')
    if not match: return (0, 24)
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if 0 <= start < end <= 24 else (0, 24)


def spacing_window(value):
    """Parse "120-300" into a (low, high) second range; unusable values fall back to 2-5 minutes."""
    match = re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*', value or '')
    if not match: return (120, 300)
    low, high = int(match.group(1)), int(match.group(2))
    return (low, high) if 0 <= low <= high else (120, 300)


CHECK_INTERVAL_HOURS = float(os.environ.get('OUTREACH_CHECK_HOURS', '24'))
# Keep profile views human-scale: 10 per day is plenty for connection checks and far below account-trust limits.
DAILY_CHECK_CAP = int(os.environ.get('OUTREACH_DAILY_CHECKS', '10'))
EXPIRE_DAYS = float(os.environ.get('OUTREACH_EXPIRE_DAYS', '30'))
LOGIN_PAUSE_HOURS = 6
# Reply check: one look at the inbox's conversation list at most every REPLY_CHECK_HOURS (never below 12 h, so
# at most twice a day), only while contacts wait for an answer. OUTREACH_REPLY_CHECK=0 turns it off.
REPLY_CHECK = os.environ.get('OUTREACH_REPLY_CHECK', '1') != '0'
REPLY_CHECK_HOURS = max(12.0, float(os.environ.get('OUTREACH_REPLY_CHECK_HOURS', '12')))
# Restart-safe hard cap per local calendar day, in addition to the interval above.
INBOX_DAILY_CAP = max(1, int(os.environ.get('OUTREACH_INBOX_DAILY_CAP', '2')))
# Automated browser reads (profile views and inbox looks) only happen inside a human-looking window of the
# local day: OUTREACH_ACTIVE_HOURS="8-20" (default). Outside it work is deferred, not fired at night.
ACTIVE_HOURS = active_window(os.environ.get('OUTREACH_ACTIVE_HOURS', '8-20'))
# A challenge/rate-limit/restriction page stops every automated read for this long (restart-safe).
BLOCK_PAUSE_HOURS = float(os.environ.get('OUTREACH_BLOCK_PAUSE_HOURS', '12'))
# Pause between two profile views (seconds): slow and irregular instead of a steady 20-60 s drumbeat.
PROFILE_SPACING = spacing_window(os.environ.get('OUTREACH_PROFILE_SPACING', '120-300'))
# Connection requests by the box: off by default (the human connects by hand); when on, paced like a human
# and hard-capped per local calendar day.
AUTO_CONNECT = os.environ.get('OUTREACH_AUTO_CONNECT', '0') != '0'
CONNECT_SPACING = spacing_window(os.environ.get('OUTREACH_CONNECT_SPACING', '90-240'))
CONNECT_DAILY_CAP = max(1, int(os.environ.get('OUTREACH_CONNECT_DAILY_CAP', '15')))
CONNECT_WEEKLY_CAP = max(CONNECT_DAILY_CAP, int(os.environ.get('OUTREACH_CONNECT_WEEKLY_CAP', '60')))
CONNECT_MAX_ATTEMPTS = max(1, int(os.environ.get('OUTREACH_CONNECT_ATTEMPTS', '3')))
CONNECT_RETRY_HOURS = max(1.0, float(os.environ.get('OUTREACH_CONNECT_RETRY_HOURS', '6')))
# A used-up invitation allowance is not a transient wall: wait days before trying again.
LIMIT_PAUSE_HOURS = max(BLOCK_PAUSE_HOURS, float(os.environ.get('OUTREACH_LIMIT_PAUSE_HOURS', '72')))
REPLY_WATCH = ('ready', 'sent')  # an answer may also come to the connection note, before our message
# Invitation look: while contacts wait for acceptance, the sent-invitations list and the newest connections
# are read about every INVITE_CHECK_HOURS inside the active window, at most INVITE_DAILY_CAP times per local
# calendar day (default 3). OUTREACH_INVITE_CHECK=0 turns it off (then profile checks follow the contacts).
INVITE_CHECK = os.environ.get('OUTREACH_INVITE_CHECK', '1') != '0'
INVITE_CHECK_HOURS = max(2.0, float(os.environ.get('OUTREACH_INVITE_CHECK_HOURS', '4')))
INVITE_DAILY_CAP = max(1, int(os.environ.get('OUTREACH_INVITE_DAILY_CAP', '3')))
INVITE_GRACE = 20 * 60  # a contact opened just now: give the human time to click "Vernetzen" before a look
SENT_URL = 'https://www.linkedin.com/mynetwork/invitation-manager/sent/'
CONNECTIONS_URL = 'https://www.linkedin.com/mynetwork/invite-connect/connections/'  # newest first
LIST_ROWS = 200  # profiles read from the top of each list
# An entry counts only inside its own card with this button (DE/EN), never in a suggestion box on the page.
CARD_ACTIONS = {SENT_URL: ('zurückziehen', 'withdraw'), CONNECTIONS_URL: ('nachricht', 'message')}
INBOX_URL = 'https://www.linkedin.com/messaging/'
INBOX_ROWS = 40  # conversations read from the top of the list
OWN_MESSAGE = ('sie:', 'you:', 'ich:')  # preview prefix when the last message is ours (DE/EN UI)
COMMON_PATHS = ('leistungen', 'produkte', 'ueber-uns', 'unternehmen', 'team', 'karriere')  # tried when the extract has no links
# Sub-pages worth reading for any kind of company (offer, about, people, hiring, news).
PAGE_HINTS = ('leistung', 'produkt', 'loesung', 'lösung', 'service', 'angebot', 'kompetenz', 'schwerpunkt', 'branche',
              'referenz', 'kunden', 'case', 'ueber', 'über', 'about', 'unternehmen', 'firma', 'kanzlei', 'praxis', 'team',
              'karriere', 'jobs', 'stellen', 'news', 'aktuell', 'presse', 'blog')


# ---------------------------------------------------------------------------- API models

class OutreachBudget(Strict):
    max_cost_usd: float = Field(default=0.15, gt=0, le=2)


class FollowUpTemplate(Strict):
    after_days: int = Field(ge=1, le=120)  # days after the first message, without an answer
    message: LongText                      # same {{placeholders}} as the message template


class OutreachRequest(Strict):
    linkedin_url: str = Field(max_length=2048)
    company_website: str | None = Field(default=None, max_length=2048)
    company_name: Text | None = None
    name: Text | None = None      # already known (e.g. from a lead): skips the search-index lookup
    headline: Text | None = None
    topic: Topic | None = None    # what you offer; required unless a template_id provides it
    # The message after acceptance: either guidance for a free draft (tone, structure, offer, CTA) or a fixed text
    # with {{placeholders}} (see fill_template), kept word for word except for the placeholders.
    template: LongText | None = None
    template_id: str | None = Field(default=None, max_length=64)  # a stored template: fills template/followups/topic
    followups: list[FollowUpTemplate] = Field(default_factory=list, max_length=5)
    note_template: LongText | None = None  # optional guidance for the connection note
    connection_note: bool = True
    note_max_chars: int = Field(default=200, ge=50, le=300)
    message_max_chars: int = Field(default=1200, ge=200, le=4000)
    language: Literal['de', 'en'] = 'de'
    auto_connect: bool = Field(default_factory=lambda: AUTO_CONNECT)  # send the connection request, not just draft the note
    allow_same_company: bool = False  # create even if another person at this company is already in outreach
    opened: bool = False  # the human opens the profile right now to connect (the UI's "Vernetzen ↗")
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
        if not (self.topic or self.template_id):
            raise ValueError('topic or template_id required')
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


ConnectState = Literal['not_sent', 'sending', 'sent', 'manual', 'failed']


class ConnectInfo(Strict):
    state: ConnectState = 'not_sent'
    attempts: int = 0
    last_attempt_at: float | None = None
    next_attempt_at: float | None = None
    sent_at: float | None = None
    error: str | None = None
    opened_at: float | None = None  # the human opened the profile to connect (UI: "Vernetzen"); unset = still to do


class Reply(Strict):
    detected_at: float
    source: Literal['inbox', 'manual']  # inbox: conversation list showed their message last
    previous_status: Status  # restored by /no-reply when a detection was wrong
    handled_at: float | None = None  # the human answered it: no longer on the to-do list


class FollowUp(Strict):
    after_days: int
    message: str
    sent_at: float | None = None  # when the human copied it into LinkedIn


class OutreachPatch(Strict):
    """Review edits from a human or an agent; nothing is sent by this call."""
    connection_note: LongText | None = None
    message: LongText | None = None
    topic: Topic | None = None
    followup_index: int | None = Field(default=None, ge=0, le=4)  # with followup_message: edit that follow-up
    followup_message: LongText | None = None


class OutreachRecord(Strict):
    id: str
    status: Status
    created_at: float
    updated_at: float
    linkedin_url: str
    campaign_id: str | None = None  # set when a campaign created this contact
    name: str | None = None
    headline: str | None = None
    company_website: str | None = None
    company_name: str | None = None
    topic: str
    connection_note: str | None = None
    connection: Connection = Field(default_factory=Connection)
    connect: ConnectInfo = Field(default_factory=ConnectInfo)
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    sources: list[str] = Field(default_factory=list, max_length=10)
    message: str | None = None
    message_is_draft: Literal[True] = True
    sent_by_system: Literal[False] = False
    sent_at: float | None = None  # when the human marked the message as sent
    followups: list[FollowUp] = Field(default_factory=list, max_length=5)  # due `after_days` after sent_at, until a reply
    template_id: str | None = None
    reply: Reply | None = None
    cost_usd: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------- message templates

# {{name}}, {{vorname}}, {{nachname}}, {{firma}}, {{rolle}} and {{website}} come from the contact; any other {{…}} is
# an instruction for the model, e.g. {{Eisbrecher mit Bezug zur Firma, 1–2 Sätze}}. Everything outside the braces
# stays word for word.
PLACEHOLDER = re.compile(r'\{\{\s*(.+?)\s*\}\}', re.S)
VARIABLES = ('name', 'vorname', 'nachname', 'firma', 'rolle', 'website')
FIELD_MAX = 400  # characters per model-written field


def placeholders(text):
    return [m.group(1) for m in PLACEHOLDER.finditer(text or '')]


def is_variable(inner):
    return sb.norm(inner) in VARIABLES


def model_fields(texts):
    """The model-written placeholders of all texts, unique, in order of appearance."""
    found = []
    for text in texts:
        for inner in placeholders(text):
            if not is_variable(inner) and inner not in found: found.append(inner)
    return found


def name_parts(name):
    words = [w for w in (name or '').split() if sb.norm(w).strip('.,') not in sb.TITLE_WORDS]
    return (words[0], words[-1]) if words else ('', '')


def fill_template(text, variables, fields):
    """Replace variables and model-written fields; a missing value leaves no stray space or blank lines."""
    def value(m):
        key = sb.norm(m.group(1))
        return variables.get(key) or '' if key in VARIABLES else fields.get(m.group(1).strip()) or ''
    out = PLACEHOLDER.sub(value, text or '')
    out = re.sub(r'[ \t]+([,.!?])', r'\1', re.sub(r'[ \t]{2,}', ' ', out))
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', out).strip()


def due_followup(record, now):
    """Index of the follow-up to send now: the first unsent one whose day has come, while no answer arrived."""
    if record.get('status') != 'sent' or not record.get('sent_at'): return None
    for i, f in enumerate(record.get('followups') or []):
        if not f.get('sent_at'):
            return i if now >= record['sent_at'] + f['after_days'] * 86400 else None
    return None


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
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL)')  # scheduling only
        os.chmod(self.path, 0o600)

    def meta(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

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

    def due_connects(self, now, limit=20):
        """Contacts whose connection request is still to be sent (and whose retry is due)."""
        with self.connect() as db:
            rows = db.execute("SELECT record FROM outreach WHERE status='awaiting_connection' "
                              'ORDER BY updated LIMIT 200').fetchall()
        due = []
        for row in rows:
            record = json.loads(row[0])
            info = record.get('connect')
            if not info: continue  # created before auto-connect existed: never invite retroactively
            state = info.get('state', 'not_sent')
            # A crash while sending leaves "sending" behind; retry it after ten minutes.
            stale_sending = state == 'sending' and (info.get('last_attempt_at') or 0) < now - 600
            if (state == 'not_sent' and (info.get('next_attempt_at') or 0) <= now) or stale_sending:
                due.append(record['id'])
                if len(due) >= limit: break
        return due

    def connects_since(self, since):
        return sum(1 for r in self.list(limit=10000) if ((r.get('connect') or {}).get('last_attempt_at') or 0) >= since)

    def delete(self, ident):
        with self.lock, self.connect() as db:
            return db.execute('DELETE FROM outreach WHERE id=?', (ident,)).rowcount > 0

    def cleanup(self):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM outreach WHERE updated < ?', (time.time() - self.retention,))


# ---------------------------------------------------------------------------- connection state (read-only)

def within_active_hours(now):
    start, end = ACTIVE_HOURS
    return start <= time.localtime(now).tm_hour < end


def next_active_at(now):
    """`now` inside the window; otherwise the next window start with a random human-looking offset."""
    if within_active_hours(now): return now
    start, end = ACTIVE_HOURS
    local = time.localtime(now)
    candidate = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, start, 0, 0, 0, 0, -1))
    if candidate <= now:
        candidate = time.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1, start, 0, 0, 0, 0, -1))
    return candidate + random.uniform(0, min(30 * 60, max(60, (end - start) * 180)))


def inbox_conversations(rows):
    """Conversation-list rows (visible text lines each) -> [(name, last_message_is_theirs)]. The preview of a
    conversation whose last message is ours starts with "Sie:"/"You:"; sponsored rows are skipped."""
    found = []
    for lines in rows or []:
        lines = [sb.sanitize(l, 300) for l in (lines if isinstance(lines, list) else []) if isinstance(l, str)]
        lines = [l for l in lines if l]
        if not lines or any(sb.norm(l) in ('gesponsert', 'sponsored') for l in lines): continue
        found.append((lines[0], not any(sb.norm(l).startswith(OWN_MESSAGE) for l in lines[1:])))
    return found


def name_pair(name):
    """(first, last) name tokens without titles, or None when the name has fewer than two."""
    tokens = sb.name_key(name or '')
    return (tokens[0], tokens[-1]) if len(tokens) >= 2 else None


def replied_contacts(records, conversations):
    """Ids of contacts whose conversation ends with their message, matched by first and last name; a name
    shared by two contacts or two conversations is left to the human (no guess)."""
    talks, contacts = {}, {}
    for name, theirs in conversations:
        if name_pair(name): talks.setdefault(name_pair(name), []).append(theirs)
    for r in records:
        if name_pair(r.get('name')): contacts.setdefault(name_pair(r.get('name')), []).append(r['id'])
    return [ids[0] for k, ids in contacts.items() if len(ids) == 1 and talks.get(k) == [True]]


def listed_contacts(records, people):
    """Ids of contacts on an invitation/connection list ([(profile path, name)] as the reader returns it):
    by profile URL, otherwise by a first and last name that is unique on both sides (no guess)."""
    entries = [tuple(e) for e in (people or []) if isinstance(e, (list, tuple)) and len(e) == 2
               and all(isinstance(x, str) for x in e)]
    paths = {profile_key('https://www.linkedin.com' + p) for p, _ in entries}
    names = Counter(name_pair(n) for _, n in entries if name_pair(n))
    contacts = Counter(name_pair(r.get('name')) for r in records if name_pair(r.get('name')))
    found = []
    for r in records:
        pair = name_pair(r.get('name'))
        if profile_key(r['linkedin_url']) in paths or (pair and names[pair] == 1 and contacts[pair] == 1):
            found.append(r['id'])
    return found


# Rows of the conversation list: the visible text of each list item that links to a thread (not opened).
# `blocked` lets the reader stop at a challenge/rate-limit page instead of waiting for rows that never appear.
BLOCK_JS = ('const blockedText = [' + ','.join(json.dumps(x) for x in BLOCK_TEXT) + '];'
            'const pageText = (((document.body || {}).innerText) || "").toLowerCase();'
            'const blocked = blockedText.some((x) => pageText.includes(x));')
INBOX_EXPRESSION = ('(() => {' + BLOCK_JS + ' const rows = [], seen = new Set();'
                    ' for (const a of document.querySelectorAll(\'a[href*="/messaging/thread/"]\')) {'
                    '  const li = a.closest("li") || a; if (seen.has(li)) continue; seen.add(li);'
                    '  rows.push((li.innerText || "").split("\\n").map((s) => s.trim()).filter(Boolean).slice(0, 8));'
                    f'  if (rows.length >= {INBOX_ROWS}) break; }}'
                    ' return {href: location.href, rows, blocked}; })()')


def people_expression(actions):
    """Profiles on the sent-invitations or connections page as [path, name]. An entry counts only when a card
    around its link holds exactly one profile and one of `actions` (Zurückziehen / Nachricht), so a
    suggestion box on the same page never counts as an invitation or a connection."""
    return ('(() => {' + BLOCK_JS +
            ' const main = document.querySelector("main") || document.body;'
            ' const path = (a) => { const m = (a.pathname || "").match(/^\\/in\\/[^/]+/); return m ? m[0] : null; };'
            ' const actions = [' + ','.join(json.dumps(x) for x in actions) + '];'
            ' const hasAction = (n) => [...n.querySelectorAll("button, a, [role=\\"button\\"]")].some((b) => !path(b) &&'
            '   actions.some((x) => ((b.innerText || "") + " " + (b.getAttribute("aria-label") || "")).toLowerCase().includes(x)));'
            ' const inCard = (a) => { for (let n = a.parentElement, i = 0; n && n !== main && i < 10; n = n.parentElement, i++) {'
            '   if (new Set([...n.querySelectorAll(\'a[href*="/in/"]\')].map(path).filter(Boolean)).size > 1) return false;'
            '   if (hasAction(n)) return true; } return false; };'
            ' const found = new Map();'
            ' for (const a of main.querySelectorAll(\'a[href*="/in/"]\')) { const p = path(a); if (!p) continue;'
            '  const name = (a.innerText || "").split("\\n").map((s) => s.trim()).filter(Boolean)[0] || "";'
            '  if (found.has(p)) { if (!found.get(p)) found.set(p, name); continue; }'
            f'  if (found.size >= {LIST_ROWS}) break;'
            '  if (inCard(a)) found.set(p, name); }'
            ' return {href: location.href, people: [...found], blocked}; })()')


class BrowserProfileReader:
    """Opens a page in a BACKGROUND tab of the shared Chromium via CDP, reads visible text, closes the tab.
    No clicks, no typing, no scrolling beyond page load: the profile top card (connection state), the
    sent-invitations and connections lists (invitation look) or the conversation list of the inbox (reply
    check; no thread is opened)."""
    def __init__(self, cdp='http://127.0.0.1:9222', timeout=25):
        self.cdp, self.timeout = cdp, timeout

    def read(self, url):
        def ready(value):
            return classify_connection(value.get('text') or '', value.get('href') or url) != 'unknown' and len(value.get('text') or '') > 80
        value = self.open(url, '(() => {' + BLOCK_JS + ' return {href: location.href, blocked, text: '
                               '((document.querySelector("main section") || document.body || {}).innerText || "").slice(0, 4000)}; })()',
                          ready)
        return value.get('text') or '', value.get('href') or url

    def read_inbox(self):
        """(conversation rows, final URL). Rows stay in memory: the caller keeps only matched contact ids."""
        value = self.open(INBOX_URL, INBOX_EXPRESSION, lambda v: bool(v.get('rows')) or login_wall(v.get('href')))
        return value.get('rows') or [], value.get('href') or INBOX_URL

    def read_list(self, url):
        """([(profile path, name)], final URL) from SENT_URL or CONNECTIONS_URL. The caller keeps only matched
        contact ids. An empty list may just be unrendered: it never proves anything."""
        counts = []
        def ready(value):  # the list renders in pieces: wait until two reads agree
            counts.append(len(value.get('people') or []))
            return login_wall(value.get('href')) or (counts[-1] > 0 and counts[-2:] == [counts[-1]] * 2)
        value = self.open(url, people_expression(CARD_ACTIONS[url]), ready)
        return value.get('people') or [], value.get('href') or url

    def open(self, url, expression, ready):
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
                value = {}
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    time.sleep(1.5)
                    value = send('Runtime.evaluate', {'returnByValue': True, 'expression': expression},
                                 session).get('result', {}).get('value') or {}
                    if value.get('blocked') or ready(value): break
                if value.get('blocked'): raise BrowserBlocked()
                return value
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
        if request.name and not request.connection_note:  # nothing to look up or write: no paid call
            return {'name': request.name, 'headline': request.headline, 'connection_note': None}, 0.0
        meter = self.meter(request, 90)
        p = self.providers_factory(meter)
        name, headline = (request.name, request.headline) if request.name else self.profile(p, request)
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
        """After acceptance: findings with verbatim evidence + message draft (+ follow-ups). Returns updated fields, cost.
        A template with {{placeholders}} is kept word for word: variables come from the contact, the model only writes
        the other fields. A template of variables only needs no paid call at all."""
        fixed = bool(placeholders(request.template))
        fields_wanted = model_fields(([request.template] if fixed else []) + [f.message for f in request.followups])
        first, last = name_parts(record.get('name'))
        variables = {'name': record.get('name'), 'vorname': first, 'nachname': last, 'rolle': record.get('headline'),
                     'firma': request.company_name or record.get('company_name'),
                     'website': record.get('company_website') or request.company_website}
        if fixed and not fields_wanted:
            return {'message': fill_template(request.template, variables, {})[:4000], 'followups': self.followups(request, variables, {}),
                    'findings': [], 'sources': [], 'error': None}, 0.0
        meter = self.meter(request)
        p = self.providers_factory(meter)
        website, pages = self.site_pages(p, request, record.get('headline'))
        variables['website'] = variables['website'] or website
        if not variables['firma'] and website: variables['firma'] = (urlsplit(website).hostname or '').removeprefix('www.')
        corpus = {u: t for u, t in pages}
        sources = '\n\n'.join(f'### SOURCE {u}\n{t}' for u, t in pages) or '(no website text found)'
        steps = [f'You prepare a personal LinkedIn message after a connection was accepted. Language: {lang_name(request.language)}. '
                 f'Recipient: {record.get("name") or "unknown"} ({record.get("headline") or "no headline"}) at '
                 f'{request.company_name or website}. Topic we offer: {request.topic}.',
                 'Step 1: from the SOURCES only, find up to 5 concrete reasons why this company could need the topic and WHERE '
                 '(business area, product, process, team, location, hiring need). Each finding needs an exact quote (verbatim substring '
                 'of one source) and that source URL; "need" and "where" at most 120 characters each. No quote, no finding. '
                 'Never invent facts.']
        if not fixed:
            steps.append(f'Step 2: write the message (max {request.message_max_chars} characters) using only those findings; no '
                         'invented numbers, clients or claims; no links unless the guidance asks for one.\n'
                         f'SENDER GUIDANCE / TEMPLATE (follow structure and tone): {request.template or "short, personal, one clear question at the end"}')
        if fields_wanted:
            marked = [PLACEHOLDER.sub(lambda m: f'[[{fields_wanted.index(m.group(1)) + 1}]]' if m.group(1) in fields_wanted else m.group(0), t)
                      for t in ([request.template] if fixed else []) + [f.message for f in request.followups]]
            steps.append('Step 3: the sender wrote fixed texts with numbered FIELDS [[n]] for you. Write only the text that replaces each '
                         'field, following its instruction; it must fit the surrounding sentences in tone, language and form of address '
                         '(Sie or du, as the text uses). Plain text, no brackets or placeholders, no greeting or sign-off, at most '
                         f'{FIELD_MAX - 100} characters unless the instruction asks for less. Facts about the company or person only from '
                         'the SOURCES; if they give nothing usable, write something friendly and general without facts.\n'
                         'FIXED TEXTS:\n' + '\n---\n'.join(marked) + '\nFIELDS:\n' +
                         '\n'.join(f'{i + 1}: {inner}' for i, inner in enumerate(fields_wanted)))
        keys = '"findings": [{"need": str, "where": str, "quote": str, "url": str}]' + (', "message": str' if not fixed else '') + \
               (', "fields": {"1": str, …}' if fields_wanted else '')
        o = p.llm('\n'.join(steps) + '\nReturn {' + keys + '}.\n\nSOURCES:\n' + sources)
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
        written = o.get('fields') if isinstance(o.get('fields'), dict) else {}
        fields = {inner: sb.sanitize(re.sub(r'\[\[\d+\]\]|\{\{.*?\}\}', '', str(written.get(str(i + 1)) or '')), FIELD_MAX) or ''
                  for i, inner in enumerate(fields_wanted)}  # stray field markers never reach the message
        if fixed: message = fill_template(request.template, variables, fields)[:4000] or None
        else: message = (o.get('message') or '').strip()[:request.message_max_chars].rstrip() or None
        error = 'field_missing' if any(not v for v in fields.values()) else None if findings else 'research_thin'
        return {'company_website': website, 'findings': findings[:8], 'sources': [u for u, _ in pages][:10], 'message': message,
                'followups': self.followups(request, variables, fields), 'error': error}, meter.cost_micro / 1_000_000

    @staticmethod
    def followups(request, variables, fields):
        return [{'after_days': f.after_days, 'message': fill_template(f.message, variables, fields)[:4000], 'sent_at': None}
                for f in request.followups]


# ---------------------------------------------------------------------------- service + background loop

class OutreachConflict(Exception):
    """The shared registry says no (person/company blocked or already contacted elsewhere)."""
    def __init__(self, code, registry_hit):
        super().__init__(code)
        self.code, self.registry_hit = code, registry_hit


class OutreachService:
    def __init__(self, store, writer=None, reader=None, browser_lock=None, start=True, registry=None, connector=None):
        self.store, self.writer, self.registry = store, writer or OutreachWriter(), registry
        self.reader, self.browser_lock = reader or BrowserProfileReader(), browser_lock or threading.Lock()
        self.connector = connector if connector is not None else (LinkedInConnector() if AUTO_CONNECT else None)
        self.stop, self.wake = threading.Event(), threading.Event()
        self.paused_until = float(self.store.meta('paused_until') or 0.0)  # survives a restart
        self.spacing = PROFILE_SPACING  # seconds between two profile views
        self.connect_spacing = CONNECT_SPACING  # seconds between two sent connection requests
        self.thread = threading.Thread(target=self.loop, name='outreach-worker', daemon=True)
        if start and os.environ.get('OUTREACH_BACKGROUND', '1') != '0': self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread.is_alive(): self.thread.join(timeout=8)

    # -- pacing: restart-safe pauses, working hours and backoff
    def pause(self, now, hours):
        self.paused_until = max(self.paused_until, now + hours * 3600)
        self.store.set_meta('paused_until', self.paused_until)
        self.store.set_meta('paused_at', now)

    def resume(self):
        """A human fixed the login/challenge via noVNC: clear the persisted pause."""
        self.paused_until = 0.0
        self.store.set_meta('paused_until', 0.0)
        self.store.set_meta('resumed_at', time.time())
        self.wake.set()

    def inbox_failures(self):
        return int(self.store.meta('inbox_failures') or 0)

    def schedule_inbox(self, now, factor=1.0):
        """Next inbox look: interval with jitter, moved into the active window, scaled by failures."""
        delay = REPLY_CHECK_HOURS * 3600 * random.uniform(1.0, 1.2) * factor
        self.store.set_meta('next_inbox_check', next_active_at(now + delay))

    def reads_today(self, kind, now):
        """Restart-safe count of `kind` page reads ('inbox', 'invite') in the local calendar day."""
        local = time.localtime(now)
        day = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
        return int(self.store.meta(kind + '_reads') or 0) if abs((self.store.meta(kind + '_day') or 0) - day) < 1 else 0

    def note_read(self, kind, now):
        local = time.localtime(now)
        count = self.reads_today(kind, now) + 1
        self.store.set_meta(kind + '_day', time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)))
        self.store.set_meta(kind + '_reads', count)

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

    def create(self, request, campaign_id=None):
        existing = self.store.find(request.linkedin_url)
        if existing: return existing, False  # no second note, no second charge
        self.registry_gate(request)  # before any paid call
        now = time.time()
        manual = not (self.connector and request.auto_connect)
        record = {'id': secrets.token_urlsafe(24), 'status': 'awaiting_connection', 'created_at': now, 'updated_at': now,
                  'linkedin_url': request.linkedin_url, 'campaign_id': campaign_id,
                  'company_website': request.company_website,
                  'company_name': request.company_name, 'topic': request.topic or '',
                  # a request sent by hand is followed through the sent-invitations list, not by profile views
                  'connection': {'state': 'unknown', 'checks': 0, 'last_checked_at': None,
                                 'next_check_at': None if manual and INVITE_CHECK else now + CHECK_INTERVAL_HOURS * 3600},
                  'connect': {'state': 'manual', 'attempts': 0, 'opened_at': now if request.opened else None} if manual
                             else {'state': 'not_sent', 'attempts': 0, 'next_attempt_at': now},
                  'template_id': request.template_id, 'cost_usd': 0.0, 'error': None}
        try:
            fields, cost = self.writer.note(request)
            record.update(fields); record['cost_usd'] += cost
        except (sb.SearchError, sb.Stop) as exc:
            record['error'] = getattr(exc, 'code', None) or getattr(exc, 'reason', 'note_failed')
        OutreachRecord.model_validate(record)
        ident, created = self.store.create(request, record)
        if created:
            self.registry_set(record, 'in_outreach')
            if record['connect']['state'] == 'not_sent': self.wake.set()  # send right after creation
        return ident, created

    def update(self, ident, patch):
        """Review edit from a human or agent (note/message/topic); never sends anything."""
        request, record = self.store.get(ident)
        if not record: return None
        if patch.connection_note is not None:
            if (record.get('connect') or {}).get('state') == 'sent': raise ValueError('connection_note_already_sent')
            record['connection_note'] = patch.connection_note[:request.note_max_chars].rstrip() or None
        if patch.message is not None:
            if record['status'] not in ('researching', 'ready', 'failed', 'sent', 'replied'):
                raise ValueError('message_not_available')
            limit = 4000 if placeholders(request.template) else request.message_max_chars  # a fixed text is never cut short
            record['message'] = patch.message[:limit].rstrip() or None
        if patch.followup_message is not None:
            followups = record.get('followups') or []
            if patch.followup_index is None or patch.followup_index >= len(followups): raise ValueError('followup_not_available')
            followups[patch.followup_index]['message'] = patch.followup_message.strip()
        if patch.topic is not None: record['topic'] = patch.topic
        self.store.save(record)
        return record

    def mark_opened(self, ident):
        """The human opened the profile to connect by hand (a contact an agent or campaign created earlier)."""
        _, record = self.store.get(ident)
        if not record: return None
        info = record.setdefault('connect', {})
        if record['status'] == 'awaiting_connection' and not info.get('opened_at'):
            info['opened_at'] = time.time()
            self.store.save(record)
        return record

    def mark_followup_sent(self, ident, index):
        """The human copied follow-up `index` into LinkedIn; earlier ones must be sent first."""
        _, record = self.store.get(ident)
        if not record: return None
        followups = record.get('followups') or []
        if record['status'] != 'sent' or not 0 <= index < len(followups): raise ValueError('followup_not_available')
        if any(not f.get('sent_at') for f in followups[:index]): raise ValueError('earlier_followup_open')
        if not followups[index].get('sent_at'):
            followups[index]['sent_at'] = time.time()
            self.store.save(record)
        return record

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
            if (record.get('connect') or {}).get('state') in (None, 'not_sent', 'sending', 'failed', 'manual'):
                record['connect'] = {**(record.get('connect') or {}), 'state': 'sent', 'next_attempt_at': None,
                                     'sent_at': time.time(), 'error': None}
            self.store.save(record)
            self.wake.set()
        return record

    def mark_sent(self, ident):
        """The human sent the message (outside this system); stop showing it as ready."""
        _, record = self.store.get(ident)
        if not record: return None
        record['status'] = 'sent'
        record['sent_at'] = time.time()
        record['connection']['next_check_at'] = None
        self.store.save(record)
        self.registry_set(record, 'contacted')
        return record

    def mark_replied(self, ident, source='manual'):
        """The person answered (seen in the inbox's conversation list, or marked by the human)."""
        _, record = self.store.get(ident)
        if not record: return None
        if record['status'] not in ('replied', 'do_not_contact'):
            record['reply'] = {'detected_at': time.time(), 'source': source, 'previous_status': record['status']}
            record['status'] = 'replied'
            record['connection']['next_check_at'] = None
            self.store.save(record)
            self.registry_set(record, 'contacted')  # in conversation: blocked for other agents
        return record

    def mark_reply_handled(self, ident):
        """The human took care of the answer on LinkedIn; the contact stays `replied`."""
        _, record = self.store.get(ident)
        if not record: return None
        if record['status'] == 'replied' and record.get('reply') and not record['reply'].get('handled_at'):
            record['reply']['handled_at'] = time.time()
            self.store.save(record)
        return record

    def unmark_replied(self, ident):
        """Undo a wrong detection: back to the status before it."""
        _, record = self.store.get(ident)
        if not record: return None
        if record['status'] == 'replied':
            record['status'], record['reply'] = (record.get('reply') or {}).get('previous_status') or 'sent', None
            self.store.save(record)
        return record

    # -- background work
    def loop(self):
        while not self.stop.is_set():
            try:
                self.store.cleanup()
                worked = (self.research_one() or self.connect_one() or self.check_one() or self.invitation_check()
                          or self.reply_check())
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

    def connects_today(self, now):
        local = time.localtime(now)
        day = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
        return self.store.connects_since(day)

    def connect_one(self):
        """Send at most one pending connection request per loop tick, paced and inside working hours."""
        now = time.time()
        if not self.connector or now < self.paused_until: return False
        if not within_active_hours(now): return False  # no invitations in the middle of the night
        if self.connects_today(now) >= CONNECT_DAILY_CAP: return False
        if self.store.connects_since(now - 7 * 86400) >= CONNECT_WEEKLY_CAP: return False
        ids = self.store.due_connects(now, 1)
        if not ids: return False
        self.send_connection(ids[0])
        self.stop.wait(random.uniform(*self.connect_spacing))
        return True

    def send_connection(self, ident):
        """One browser action in connect.py: open the profile, click Vernetzen, add the note, send."""
        _, record = self.store.get(ident)
        if not record or record['status'] != 'awaiting_connection' or not self.connector: return record
        now = time.time()
        if now < self.paused_until: return record
        info = record.setdefault('connect', {})
        info.update(state='sending', attempts=(info.get('attempts') or 0) + 1, last_attempt_at=now)
        self.store.save(record)
        try:
            with self.browser_lock:
                result = self.connector.send_request(record['linkedin_url'], record.get('connection_note'))
        except BrowserBlocked:
            result = {'state': 'blocked'}
        except Exception:
            result = {'state': 'error'}
        now, state = time.time(), result.get('state')
        if state == 'connected':  # already connected: no request needed, start research
            record['status'], record['connection']['state'] = 'connected', 'connected'
            record['connection']['next_check_at'] = None
            info.update(state='sent', sent_at=now, next_attempt_at=None, error=None)
            self.registry_set(record, 'contacted')
            self.wake.set()
        elif state in ('sent', 'pending', 'sent_unconfirmed'):
            record['connection']['state'] = 'pending'
            record['connection']['next_check_at'] = now + CHECK_INTERVAL_HOURS * 3600 * random.uniform(0.9, 1.2)
            info.update(state='sent', sent_at=now, next_attempt_at=None, error=None)
            self.registry_set(record, 'contacted')
        elif state == 'limit':
            info.update(state='not_sent', next_attempt_at=now + LIMIT_PAUSE_HOURS * 3600, error='connect_limit')
            self.pause(now, LIMIT_PAUSE_HOURS)  # allowance used up: days, not hours
        elif state == 'blocked':
            info.update(state='not_sent', next_attempt_at=now + BLOCK_PAUSE_HOURS * 3600, error='connect_blocked')
            self.pause(now, BLOCK_PAUSE_HOURS)  # hard stop: a human must fix the session
        elif state == 'login_required':
            info.update(state='not_sent', next_attempt_at=now + LOGIN_PAUSE_HOURS * 3600, error='connect_login_required')
            self.pause(now, LOGIN_PAUSE_HOURS)
        else:
            # no button, no dialog, no send button or a page we did not understand
            error = 'connect_' + (state or 'unknown')
            if (info.get('attempts') or 0) >= CONNECT_MAX_ATTEMPTS:
                info.update(state='failed', next_attempt_at=None, error=error)
            else:
                info.update(state='not_sent', next_attempt_at=now + CONNECT_RETRY_HOURS * 3600, error=error)
        self.store.save(record)
        return record

    def retry_connect(self, ident):
        """Human-triggered retry for a failed or manually-created contact."""
        _, record = self.store.get(ident)
        if not record or record['status'] != 'awaiting_connection' or not self.connector: return record
        record['connect'] = {'state': 'not_sent', 'attempts': 0, 'next_attempt_at': time.time()}
        self.store.save(record)
        self.wake.set()
        return record

    def apply_connection(self, record):
        """Read the profile ONCE (no clicks, nothing sent), update connection state, persist. Returns the
        observed state. On 'connected' the contact moves to research; a login wall pauses background checks."""
        now = time.time()
        conn = record['connection']
        blocked = False
        with self.browser_lock:  # never in parallel with a browser-agent discovery job
            try:
                text, href = self.reader.read(record['linkedin_url'])
                blocked = blocked_page(href, text)
                state = 'login_required' if blocked else classify_connection(text, href)
            except BrowserBlocked:
                blocked, state = True, 'login_required'
            except Exception:
                state = 'unknown'
        conn.update(state=state, checks=conn['checks'] + 1, last_checked_at=now,
                    next_check_at=now + CHECK_INTERVAL_HOURS * 3600 * random.uniform(0.9, 1.2))
        if state == 'connected' and record['status'] in ('awaiting_connection', 'expired', 'failed'):
            record['status'], conn['next_check_at'] = 'connected', None
            self.wake.set()
        elif state == 'login_required':
            # challenge/rate-limit pages are a harder signal than a plain login wall
            self.pause(now, BLOCK_PAUSE_HOURS if blocked else LOGIN_PAUSE_HOURS)
        self.store.save(record)
        return state

    def check_connection(self, ident):
        """Manual 'check profile status now': one read-only look, ignoring schedule and daily cap, but not
        while a pause (login wall or challenge) is active, since another read would only repeat the signal."""
        _, record = self.store.get(ident)
        if not record: return None
        if record['status'] not in ('awaiting_connection', 'connected', 'expired', 'failed'):
            return record  # sent/replied/do_not_contact: the connection question is already settled
        if time.time() < self.paused_until:
            return record
        self.apply_connection(record)
        return self.store.get(ident)[1]

    def check_one(self):
        now = time.time()
        if now < self.paused_until: return False
        if not within_active_hours(now): return False  # never look at profiles at night
        if self.store.checks_since(now - 86400) >= DAILY_CHECK_CAP: return False
        ids = self.store.due_checks(now, 1)
        if not ids: return False
        _, record = self.store.get(ids[0])
        if now - record['created_at'] > EXPIRE_DAYS * 86400:
            record['status'], record['connection']['next_check_at'] = 'expired', None
            self.store.save(record); self.registry_set(record, 'expired'); return True
        self.apply_connection(record)
        self.stop.wait(random.uniform(*self.spacing))
        return True

    def invitation_check(self, manual=False):
        """While contacts wait for acceptance, about every INVITE_CHECK_HOURS (at most INVITE_DAILY_CAP times a
        local day, inside the active window): one read of the sent-invitations list and one of the newest
        connections (background tabs, no clicks). Listed under "sent" -> the invitation is pending (a request
        sent by hand is now confirmed); among the newest connections -> connected, research starts. Not being
        listed proves nothing: the contact stays as it is. Both lists stay in this function; only matched
        contact ids are used. `manual` (a human asked for it) ignores schedule, window and cap, never a pause.
        Returns counts after a look, None when nothing was read."""
        now = time.time()
        read = getattr(self.reader, 'read_list', None)
        if not read or now < self.paused_until: return None
        if not manual:
            if not INVITE_CHECK or now < (self.store.meta('next_invite_check') or 0): return None
            if not within_active_hours(now):
                self.store.set_meta('next_invite_check', next_active_at(now))  # defer to the next daytime window
                return None
            if self.reads_today('invite', now) >= INVITE_DAILY_CAP: return None
        waiting = self.store.list('awaiting_connection', limit=10000)
        if not any(manual or now - r['created_at'] >= INVITE_GRACE for r in waiting): return None
        self.store.set_meta('next_invite_check',
                            next_active_at(now + INVITE_CHECK_HOURS * 3600 * random.uniform(0.9, 1.3)))
        self.note_read('invite', now)
        lists, href = {}, ''
        with self.browser_lock:  # never in parallel with a browser-agent job, a profile check or the inbox read
            try:
                for url in (SENT_URL, CONNECTIONS_URL):
                    lists[url], href = read(url)
                    if login_wall(href) or blocked_page(href): break
            except BrowserBlocked:
                self.pause(now, BLOCK_PAUSE_HOURS)
                return {'checked': False, 'reason': 'blocked'}
            except Exception:
                return {'checked': False, 'reason': 'unreadable'}  # retried at the next scheduled look
        if blocked_page(href):
            self.pause(now, BLOCK_PAUSE_HOURS)
            return {'checked': False, 'reason': 'blocked'}
        if login_wall(href):
            self.pause(now, LOGIN_PAUSE_HOURS)
            return {'checked': False, 'reason': 'login_required'}
        self.store.set_meta('last_invite_check', now)  # last complete look
        records = waiting + self.store.list('expired', limit=10000)  # a late acceptance still counts
        connected = set(listed_contacts(records, lists[CONNECTIONS_URL]))
        pending = set(listed_contacts(waiting, lists[SENT_URL])) - connected
        for r in records:
            if r['id'] in connected: self.apply_listed(r['id'], 'connected', now)
            elif r['id'] in pending: self.apply_listed(r['id'], 'pending', now)
            elif r['status'] == 'awaiting_connection' and now - r['created_at'] > EXPIRE_DAYS * 86400:
                self.apply_listed(r['id'], 'expired', now)
        return {'checked': True, 'sent_listed': len(lists[SENT_URL]), 'connections_listed': len(lists[CONNECTIONS_URL]),
                'contacts_waiting': len(waiting), 'pending': len(pending), 'connected': len(connected)}

    def apply_listed(self, ident, state, now):
        """Outcome of a look for one contact: 'pending' (under "sent"), 'connected' (among the newest
        connections) or 'expired' (not accepted within OUTREACH_EXPIRE_DAYS)."""
        _, record = self.store.get(ident)  # fresh: a human may have changed it during the read
        if not record or record['status'] not in ('awaiting_connection', 'expired'): return
        if state == 'expired':
            if record['status'] == 'awaiting_connection':
                record['status'], record['connection']['next_check_at'] = 'expired', None
                self.store.save(record); self.registry_set(record, 'expired')
            return
        conn, info = record['connection'], record.setdefault('connect', {})
        if state == 'pending' and conn.get('state') == 'pending' and info.get('state') == 'sent': return  # nothing new
        conn.update(state=state, last_checked_at=now)
        if info.get('state') != 'sent':  # confirmed on LinkedIn: the box never sends this request again
            info.update(state='sent', sent_at=info.get('sent_at') or now, next_attempt_at=None, error=None)
        if state == 'connected':
            record['status'], conn['next_check_at'] = 'connected', None
            self.wake.set()
        self.store.save(record)
        self.registry_set(record, 'contacted')

    def reply_check(self):
        """While contacts wait for an answer, at most every REPLY_CHECK_HOURS: one read of the conversation
        list of the logged-in inbox (background tab, no thread opened, no clicks). A contact whose conversation
        ends with their message, matched unambiguously by name, becomes "replied". The list's text (names and
        previews of every conversation) stays in this function: never stored, logged or returned.
        A challenge/rate-limit page pauses everything; empty or unreadable lists back off exponentially."""
        now = time.time()
        read = getattr(self.reader, 'read_inbox', None)
        if not REPLY_CHECK or not read or now < self.paused_until or now < (self.store.meta('next_inbox_check') or 0):
            return False
        if not within_active_hours(now):
            self.store.set_meta('next_inbox_check', next_active_at(now))  # defer to the next daytime window
            return False
        if self.reads_today('inbox', now) >= INBOX_DAILY_CAP: return False
        waiting = [r for status in REPLY_WATCH for r in self.store.list(status, limit=10000)]
        if not waiting: return False
        self.schedule_inbox(now)
        self.store.set_meta('last_inbox_check', now)
        self.note_read('inbox', now)
        with self.browser_lock:  # never in parallel with a browser-agent discovery job or a profile check
            try:
                rows, href = read()
            except BrowserBlocked:
                self.pause(now, BLOCK_PAUSE_HOURS)
                return True
            except Exception:
                self.backoff_inbox(now)
                return True  # counted as a look; retry later with a growing gap
        if blocked_page(href):
            self.pause(now, BLOCK_PAUSE_HOURS)
            return True
        talks = inbox_conversations(rows)
        if not rows or not talks:  # page not rendered or no conversation: do not keep the 12 h rhythm
            self.backoff_inbox(now)
            return True
        self.store.set_meta('inbox_failures', 0)
        for ident in replied_contacts(waiting, talks):
            self.mark_replied(ident, 'inbox')
        return True

    def backoff_inbox(self, now):
        """Unreadable or empty inbox: space the next look out (12 h -> 24 h -> 48 h) instead of hammering."""
        failures = min(self.inbox_failures() + 1, 3)
        self.store.set_meta('inbox_failures', failures)
        self.schedule_inbox(now, 2 ** failures)


def inbox_check(directory=None, reader=None):
    """Calibration, read-only: one read of the inbox's conversation list in the logged-in browser; returns
    counts only (no names, no previews) and changes nothing. Run it once after logging in via noVNC."""
    store = OutreachStore(directory or os.environ.get('DISCOVERY_DATA_DIR', '/opt/data/discovery-api'))
    try:
        rows, href = (reader or BrowserProfileReader()).read_inbox()
    except BrowserBlocked:
        return {'blocked': True}  # challenge/rate-limit page: do not retry here
    talks = inbox_conversations(rows)
    waiting = [r for status in REPLY_WATCH for r in store.list(status, limit=10000)]
    return {'login_wall': login_wall(href), 'rows_read': len(rows), 'conversations': len(talks),
            'last_message_ours': sum(not theirs for _, theirs in talks), 'last_message_theirs': sum(theirs for _, theirs in talks),
            'contacts_waiting': len(waiting), 'contacts_with_reply': len(replied_contacts(waiting, talks))}


def invitations_check(directory=None, reader=None):
    """Calibration, read-only: one read of the sent-invitations list and one of the newest connections in the
    logged-in browser; returns counts only (no names) and changes nothing. Run it once after logging in."""
    store = OutreachStore(directory or os.environ.get('DISCOVERY_DATA_DIR', '/opt/data/discovery-api'))
    reader = reader or BrowserProfileReader()
    try:
        sent, sent_href = reader.read_list(SENT_URL)
        newest, newest_href = reader.read_list(CONNECTIONS_URL)
    except BrowserBlocked:
        return {'blocked': True}  # challenge/rate-limit page: do not retry here
    waiting = store.list('awaiting_connection', limit=10000)
    return {'login_wall': login_wall(sent_href) or login_wall(newest_href), 'sent_listed': len(sent),
            'connections_listed': len(newest), 'contacts_waiting': len(waiting),
            'contacts_pending': len(listed_contacts(waiting, sent)), 'contacts_connected': len(listed_contacts(waiting, newest))}


if __name__ == '__main__':
    import sys
    checks = {'inbox-check': inbox_check, 'invitations-check': invitations_check}
    if len(sys.argv) != 2 or sys.argv[1] not in checks:
        print('usage: outreach.py inbox-check | invitations-check', file=sys.stderr)
        sys.exit(2)
    print(json.dumps(checks[sys.argv[1]](), indent=1))
