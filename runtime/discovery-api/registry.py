"""Shared contact registry: one lookup/suppression list for search, outreach and external agents.

Persons are keyed by the canonical LinkedIn profile, companies by website domain (preferred) or a
normalised company name. Only keys, status, source and timestamps are stored (no lead content).

Statuses
  delivered       found and returned by a search (expires after REGISTRY_DELIVERED_DAYS, default 365)
  in_outreach     an outreach contact exists (this box or another agent)
  contacted       a message/connection request was sent
  expired         an outreach attempt ended without acceptance
  do_not_contact  never contact again (sticky: only DELETE removes it)
  customer        existing customer (usually a company entry)
  excluded        any other reason to skip (competitor, partner, ...)

A person with ANY entry is never delivered again by search (exclude_previous). Companies with a
BLOCKING status are skipped by search and need allow_same_company for a new outreach contact.
"""
from contextlib import contextmanager
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from models import Strict, Text, profile_key, profile_url, public_url
import search_backend as sb

RegistryStatus = Literal['delivered', 'in_outreach', 'contacted', 'expired', 'do_not_contact', 'customer', 'excluded']
BLOCKING = {'in_outreach', 'contacted', 'do_not_contact', 'customer', 'excluded'}
HARD_BLOCK = {'do_not_contact', 'customer', 'excluded'}  # never overridable by allow_same_company
DELIVERED_DAYS = float(os.environ.get('REGISTRY_DELIVERED_DAYS', '365'))
LEGAL_FORMS = {'gmbh', 'mbh', 'ag', 'kg', 'co', 'ug', 'se', 'ev', 'partg', 'mbb', 'llp', 'ltd', 'inc', 'llc', 'plc', 'ohg',
               'gbr', 'kgaa', 'haftungsbeschränkt', 'haftungsbeschraenkt', 'und', 'and', 'the'}
SECOND_LEVEL = {'co', 'com', 'org', 'net', 'gov', 'ac', 'or'}


def company_domain(website):
    host = (urlsplit(website or '').hostname or '').casefold().removeprefix('www.')
    labels = [l for l in host.split('.') if l]
    if len(labels) < 2: return None
    keep = 3 if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return '.'.join(labels[-keep:])


def company_name_key(name):
    tokens = [t for t in re.split(r'[^\w]+', sb.norm(name or '')) if t and t not in LEGAL_FORMS]
    return ' '.join(tokens) or None


def person_key(linkedin_url):
    canonical = sb.linkedin_profile(linkedin_url or '')
    return profile_key(canonical) if canonical else None


# ---------------------------------------------------------------------------- API models

class RegistryRef(Strict):
    linkedin_url: str | None = Field(default=None, max_length=2048)
    company_website: str | None = Field(default=None, max_length=2048)
    company_name: Text | None = None

    @model_validator(mode='after')
    def something(self):
        if not (self.linkedin_url or self.company_website or self.company_name):
            raise ValueError('linkedin_url, company_website or company_name required')
        if self.linkedin_url:
            canonical = sb.linkedin_profile(self.linkedin_url)
            if not canonical: raise ValueError('LinkedIn profile URL (/in/...) required')
            self.linkedin_url = profile_url(canonical)
        if self.company_website: public_url(self.company_website)
        return self


class RegistryItem(RegistryRef):
    status: RegistryStatus
    source: Text = 'api'   # who registered it, e.g. "agent-b", "crm-import"
    note: Text | None = None


class RegistryCheckRequest(Strict):
    items: list[RegistryRef] = Field(min_length=1, max_length=500)


class RegistryUpsertRequest(Strict):
    items: list[RegistryItem] = Field(min_length=1, max_length=500)


class RegistryEntry(Strict):
    id: str
    kind: Literal['person', 'company']
    status: RegistryStatus
    source: str
    linkedin_url: str | None = None
    company_name: str | None = None
    company_website: str | None = None
    company_domain: str | None = None
    note: str | None = None
    created_at: float
    updated_at: float
    expires_at: float | None = None


class CompanyHit(Strict):
    domain: str | None = None
    name_key: str | None = None
    statuses: dict[str, int]
    entries: list[str] = Field(default_factory=list, max_length=20)  # registry ids


class CheckResult(Strict):
    index: int
    known: bool                 # any entry for the person or the company
    blocked: bool               # should NOT be searched/contacted (blocking status)
    reasons: list[str]
    person: RegistryEntry | None = None
    company: CompanyHit | None = None


class UpsertResult(Strict):
    created: int
    updated: int
    unchanged: int
    ids: list[str]


# ---------------------------------------------------------------------------- store

class Registry:
    def __init__(self, directory):
        self.path = os.path.join(str(directory), 'registry.sqlite3')
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS registry(id TEXT PRIMARY KEY, profile TEXT UNIQUE, domain TEXT, name_key TEXT, '
                       'linkedin_url TEXT, company_name TEXT, company_website TEXT, status TEXT NOT NULL, source TEXT NOT NULL, '
                       'note TEXT, created REAL NOT NULL, updated REAL NOT NULL, expires REAL)')
            db.execute('CREATE INDEX IF NOT EXISTS registry_domain ON registry(domain)')
            db.execute('CREATE INDEX IF NOT EXISTS registry_name ON registry(name_key)')
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

    @staticmethod
    def entry(row):
        return RegistryEntry(id=row['id'], kind='person' if row['profile'] else 'company', status=row['status'],
                             source=row['source'], linkedin_url=row['linkedin_url'], company_name=row['company_name'],
                             company_website=row['company_website'], company_domain=row['domain'], note=row['note'],
                             created_at=row['created'], updated_at=row['updated'], expires_at=row['expires']).model_dump()

    def cleanup(self, db=None):
        def run(d): d.execute('DELETE FROM registry WHERE expires IS NOT NULL AND expires < ?', (time.time(),))
        if db: run(db)
        else:
            with self.connect() as d: run(d)

    # -- writes
    def upsert(self, item, db=None, fill_only=False):
        """Returns ('created'|'updated'|'unchanged', id). do_not_contact is sticky; delivered never downgrades.
        fill_only (backfill): existing entries only gain missing company fields; status and timestamps stay."""
        def run(d):
            pkey = person_key(item.get('linkedin_url'))
            domain = company_domain(item.get('company_website'))
            name_key = company_name_key(item.get('company_name'))
            if pkey:
                row = d.execute('SELECT * FROM registry WHERE profile=?', (pkey,)).fetchone()
            else:
                row = d.execute('SELECT * FROM registry WHERE profile IS NULL AND ((domain IS NOT NULL AND domain=?) OR '
                                '(domain IS NULL AND name_key=?))', (domain, name_key)).fetchone()
            now, status = time.time(), item['status']
            expires = now + DELIVERED_DAYS * 86400 if status == 'delivered' else None
            if row and fill_only:
                if (row['domain'] or not domain) and (row['name_key'] or not name_key): return 'unchanged', row['id']
                d.execute('UPDATE registry SET domain=COALESCE(domain, ?), name_key=COALESCE(name_key, ?), '
                          'company_name=COALESCE(company_name, ?), company_website=COALESCE(company_website, ?) WHERE id=?',
                          (domain, name_key, item.get('company_name'), item.get('company_website'), row['id']))
                return 'updated', row['id']
            if row:
                if row['status'] == 'do_not_contact' or (status == 'delivered' and row['status'] != 'delivered'):
                    return 'unchanged', row['id']
                d.execute('UPDATE registry SET status=?, source=?, note=COALESCE(?, note), domain=COALESCE(?, domain), '
                          'name_key=COALESCE(?, name_key), company_name=COALESCE(?, company_name), '
                          'company_website=COALESCE(?, company_website), updated=?, expires=? WHERE id=?',
                          (status, item.get('source') or row['source'], item.get('note'), domain, name_key,
                           item.get('company_name'), item.get('company_website'), now, expires, row['id']))
                return 'updated', row['id']
            ident = secrets.token_urlsafe(12)
            d.execute('INSERT INTO registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (ident, pkey, domain, name_key, sb.linkedin_profile(item.get('linkedin_url') or '') if pkey else None,
                       item.get('company_name'), item.get('company_website'), status, item.get('source') or 'api',
                       item.get('note'), now, now, expires))
            return 'created', ident
        with self.lock:
            if db: return run(db)
            with self.connect() as d:
                d.execute('BEGIN IMMEDIATE')
                try:
                    result = run(d)
                    d.execute('COMMIT')
                    return result
                except Exception:
                    d.execute('ROLLBACK'); raise

    def upsert_many(self, items):
        counts, ids = {'created': 0, 'updated': 0, 'unchanged': 0}, []
        for item in items:
            outcome, ident = self.upsert(item)
            counts[outcome] += 1; ids.append(ident)
        return {**counts, 'ids': ids}

    def remove_person(self, linkedin_url, only_status=None, only_source=None):
        pkey = person_key(linkedin_url)
        if not pkey: return
        with self.lock, self.connect() as db:
            row = db.execute('SELECT status, source FROM registry WHERE profile=?', (pkey,)).fetchone()
            if row and (only_status is None or row['status'] == only_status) and (only_source is None or row['source'] == only_source):
                db.execute('DELETE FROM registry WHERE profile=?', (pkey,))

    def delete(self, ident):
        with self.lock, self.connect() as db:
            return db.execute('DELETE FROM registry WHERE id=?', (ident,)).rowcount > 0

    # -- reads
    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM registry WHERE id=?', (ident,)).fetchone()
        return self.entry(row) if row else None

    def list(self, status=None, source=None, q=None, limit=500):
        self.cleanup()
        sql, args = 'SELECT * FROM registry WHERE 1=1', []
        if status: sql += ' AND status=?'; args.append(status)
        if source: sql += ' AND source=?'; args.append(source)
        if q:
            sql += " AND (lower(coalesce(linkedin_url,'')||' '||coalesce(company_name,'')||' '||coalesce(domain,'')||' '||coalesce(note,'')) LIKE ?)"
            args.append('%' + q.casefold() + '%')
        with self.connect() as db:
            rows = db.execute(sql + ' ORDER BY updated DESC LIMIT ?', (*args, limit)).fetchall()
        return [self.entry(r) for r in rows]

    def company_rows(self, db, domain, name_key):
        if not (domain or name_key): return []
        return db.execute('SELECT * FROM registry WHERE (domain IS NOT NULL AND domain=?) OR (name_key IS NOT NULL AND name_key=?)',
                          (domain, name_key)).fetchall()

    def check(self, ref, exclude_profile=None):
        """Lookup for one person/company reference (dict with linkedin_url/company_website/company_name)."""
        pkey = person_key(ref.get('linkedin_url'))
        domain, name_key = company_domain(ref.get('company_website')), company_name_key(ref.get('company_name'))
        with self.connect() as db:
            person = db.execute('SELECT * FROM registry WHERE profile=?', (pkey,)).fetchone() if pkey else None
            rows = [r for r in self.company_rows(db, domain, name_key) if not (pkey and r['profile'] == pkey)
                    and not (exclude_profile and r['profile'] == exclude_profile)]
        reasons = []
        if person and person['status'] in BLOCKING: reasons.append('person_' + person['status'])
        statuses = {}
        for r in rows: statuses[r['status']] = statuses.get(r['status'], 0) + 1
        for s in sorted(set(statuses) & BLOCKING): reasons.append('company_' + s)
        company = CompanyHit(domain=domain, name_key=name_key, statuses=statuses, entries=[r['id'] for r in rows][:20]).model_dump() if rows else None
        return {'known': bool(person or rows), 'blocked': bool(reasons), 'reasons': reasons,
                'person': self.entry(person) if person else None, 'company': company}

    def snapshot(self):
        """For a search run: known persons, persons with a blocking status, blocked company domains and name keys."""
        self.cleanup()
        with self.connect() as db:
            rows = db.execute('SELECT profile, domain, name_key, status FROM registry').fetchall()
        blocked = [r for r in rows if r['status'] in BLOCKING]
        return {'people': {r['profile'] for r in rows if r['profile']},
                'blocked_people': {r['profile'] for r in blocked if r['profile']},
                'domains': {r['domain'] for r in blocked if r['domain']},
                'names': {r['name_key'] for r in blocked if r['name_key']}}

    def backfill(self, delivered_profiles, outreach_records, delivered_leads=()):
        """Idempotent import of data that predates the registry (company fields from stored job results)."""
        for record in outreach_records:
            status = {'sent': 'contacted', 'expired': 'expired', 'do_not_contact': 'do_not_contact'}.get(record['status'], 'in_outreach')
            self.upsert({'linkedin_url': record['linkedin_url'], 'company_website': record.get('company_website'),
                         'company_name': record.get('company_name'), 'status': status, 'source': 'outreach'}, fill_only=True)
        for lead in delivered_leads:
            self.upsert({'linkedin_url': lead['linkedin_url'], 'company_website': lead.get('company_website'),
                         'company_name': lead.get('company'), 'status': 'delivered', 'source': 'search'}, fill_only=True)
        for key in delivered_profiles:
            self.upsert({'linkedin_url': key, 'status': 'delivered', 'source': 'search'}, fill_only=True)
