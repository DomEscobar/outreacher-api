"""Campaign engine: discovery -> registry-filtered outreach -> connect (by hand, or by the box with
OUTREACH_AUTO_CONNECT=1) -> acceptance -> research.

A campaign is the agent-facing "run this whole funnel" primitive. It owns one discovery job, then creates
one outreach contact per qualified lead (paced by the existing outreach worker), and reports a funnel. It
never sends messages after acceptance and never bypasses a pause or a registry block.
"""
from contextlib import contextmanager
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Literal

from pydantic import Field, model_validator

from models import Criteria, Strict, Text
from outreach import FollowUpTemplate, LongText, OutreachBudget, OutreachConflict, OutreachRequest, Topic
from store import QueueFull

CampaignStatus = Literal['queued', 'discovering', 'outreaching', 'awaiting_review', 'done', 'paused', 'failed',
                         'cancelled']
ACTIVE = ('queued', 'discovering', 'outreaching', 'awaiting_review')
FINAL = ('done', 'failed', 'cancelled')


class CampaignRequest(Strict):
    name: Text | None = None
    criteria: Criteria
    topic: Topic | None = None  # required unless template_id provides it
    template: LongText | None = None
    template_id: str | None = Field(default=None, max_length=64)  # a stored template: message, follow-ups and topic
    followups: list[FollowUpTemplate] = Field(default_factory=list, max_length=5)
    note_template: LongText | None = None
    connection_note: bool = True
    note_max_chars: int = Field(default=200, ge=50, le=300)
    message_max_chars: int = Field(default=1200, ge=200, le=4000)
    language: Literal['de', 'en'] = 'de'
    auto_connect: bool | None = None  # None -> the outreach default (OUTREACH_AUTO_CONNECT)
    allow_same_company: bool = False
    max_contacts: int | None = Field(default=None, ge=1, le=100)
    budget: OutreachBudget | None = None

    @model_validator(mode='after')
    def contacts(self):
        if self.max_contacts is None: self.max_contacts = self.criteria.limit
        if not (self.topic or self.template_id): raise ValueError('topic or template_id required')
        return self


class CampaignRecord(Strict):
    id: str
    status: CampaignStatus
    created_at: float
    updated_at: float
    request: CampaignRequest
    job_id: str | None = None
    contact_ids: list[str] = Field(default_factory=list, max_length=200)
    counts: dict[str, int] = Field(default_factory=dict)
    connect_counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None
    resume_status: CampaignStatus | None = None


# ---------------------------------------------------------------------------- persistence

class CampaignStore:
    def __init__(self, directory):
        self.path = os.path.join(str(directory), 'campaigns.sqlite3')
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, status TEXT NOT NULL, '
                       'updated REAL NOT NULL, request TEXT NOT NULL, record TEXT NOT NULL)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA secure_delete=ON')
            yield db
        finally:
            db.close()

    def create(self, request):
        now = time.time()
        record = CampaignRecord(id=secrets.token_urlsafe(24), status='queued', created_at=now, updated_at=now,
                                request=request).model_dump()
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO campaigns VALUES(?,?,?,?,?)',
                       (record['id'], record['status'], now, request.model_dump_json(), json.dumps(record)))
        return record

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT record FROM campaigns WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, record):
        record['updated_at'] = time.time()
        CampaignRecord.model_validate(record)
        with self.lock, self.connect() as db:
            db.execute('UPDATE campaigns SET status=?, updated=?, record=? WHERE id=?',
                       (record['status'], record['updated_at'], json.dumps(record), record['id']))

    def list(self, status=None, limit=200):
        with self.connect() as db:
            rows = db.execute('SELECT record FROM campaigns' + (' WHERE status=?' if status else '') +
                              ' ORDER BY updated DESC LIMIT ?', ((status, limit) if status else (limit,))).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete(self, ident):
        with self.lock, self.connect() as db:
            return db.execute('DELETE FROM campaigns WHERE id=?', (ident,)).rowcount > 0


# ---------------------------------------------------------------------------- engine

def outreach_request(settings, lead):
    """Build a validated OutreachRequest from a campaign's settings and one discovery lead."""
    data = {'linkedin_url': lead.get('linkedin_url'), 'topic': settings.topic,
            'connection_note': settings.connection_note, 'note_max_chars': settings.note_max_chars,
            'message_max_chars': settings.message_max_chars, 'language': settings.language,
            'allow_same_company': settings.allow_same_company}
    if lead.get('company_website'): data['company_website'] = lead['company_website']
    if lead.get('company'): data['company_name'] = lead['company']
    if lead.get('name'): data['name'] = lead['name']  # known from discovery: no search-index lookup
    if lead.get('current_role'): data['headline'] = lead['current_role']
    if settings.template: data['template'] = settings.template
    if settings.template_id: data['template_id'] = settings.template_id
    if settings.followups: data['followups'] = [f.model_dump() for f in settings.followups]
    if settings.note_template: data['note_template'] = settings.note_template
    if settings.auto_connect is not None: data['auto_connect'] = settings.auto_connect
    if settings.budget is not None: data['budget'] = settings.budget.model_dump()
    return OutreachRequest.model_validate(data)


class CampaignService:
    def __init__(self, store, discovery_store, outreach, worker=None, start=True, templates=None):
        self.store, self.discovery_store, self.outreach, self.worker = store, discovery_store, outreach, worker
        self.templates = templates  # the search's leads carry the campaign's template
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.loop, name='campaign-worker', daemon=True)
        if start and os.environ.get('OUTREACH_BACKGROUND', '1') != '0': self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread.is_alive(): self.thread.join(timeout=8)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # never log lead/model data
            self.wake.wait(timeout=20)
            self.wake.clear()

    def tick(self):
        for record in self.store.list(limit=1000):
            if record['status'] in ACTIVE: self.advance(record)

    def advance(self, record):
        if record['status'] == 'queued':
            try:
                criteria = Criteria.model_validate(record['request']['criteria'])
                job_id = self.discovery_store.submit(criteria)
            except QueueFull:
                return  # try again on the next tick
            template_id = record['request'].get('template_id')
            if self.templates and template_id: self.templates.assign(job_id, template_id)
            record.update(status='discovering', job_id=job_id)
            self.store.save(record)
            if self.worker: self.worker.wake.set()
        elif record['status'] == 'discovering':
            job = self.discovery_store.get(record['job_id']) if record.get('job_id') else None
            if not job or job['status'] in ('queued', 'running'): return
            self.fan_out(record, job)
        else:
            self.refresh(record)

    def fan_out(self, record, job):
        settings = CampaignRequest.model_validate(record['request'])
        result = json.loads(job['result']) if job.get('result') else {}
        leads = result.get('leads') or []
        created = 0
        for lead in leads:
            if len(record['contact_ids']) >= (settings.max_contacts or settings.criteria.limit): break
            try:
                request = outreach_request(settings, lead)
                ident, is_new = self.outreach.create(request, campaign_id=record['id'])
            except (OutreachConflict, ValueError):
                continue
            if is_new:
                record['contact_ids'].append(ident); created += 1
        if job['status'] in ('failed', 'timed_out') and not record['contact_ids']:
            record['status'], record['error'] = 'failed', job.get('error') or job['status']
        elif job['status'] == 'blocked' and not record['contact_ids']:
            record['status'], record['error'] = 'failed', 'discovery_blocked'
        else:
            record['status'] = 'outreaching'
            if job['status'] == 'blocked': record['error'] = 'discovery_blocked'
        self.store.save(record)
        self.refresh(record)
        if created: self.outreach.wake.set()

    def refresh(self, record):
        if record['status'] in ('failed', 'cancelled', 'paused'): return  # terminal/control states win
        contacts = [self.outreach.store.get(i)[1] for i in record['contact_ids']]
        contacts = [c for c in contacts if c]
        counts, connects, cost = {}, {}, 0.0
        for c in contacts:
            counts[c['status']] = counts.get(c['status'], 0) + 1
            state = (c.get('connect') or {}).get('state') or 'none'
            connects[state] = connects.get(state, 0) + 1
            cost += c.get('cost_usd') or 0
        active = any(s in counts for s in ('awaiting_connection', 'connected', 'researching'))
        if active:
            status = 'outreaching'
        elif counts.get('ready'):
            status = 'awaiting_review'
        else:
            status = 'done'
        stats = (status, dict(sorted(counts.items())), dict(sorted(connects.items())), round(cost, 6))
        old = (record['status'], record.get('counts'), record.get('connect_counts'), record.get('cost_usd'))
        record['status'], record['counts'], record['connect_counts'], record['cost_usd'] = stats
        if stats != old: self.store.save(record)

    # -- control
    def pause(self, ident):
        record = self.store.get(ident)
        if not record or record['status'] in FINAL or record['status'] == 'paused': return record
        record['resume_status'], record['status'] = record['status'], 'paused'
        self.store.save(record)
        return record

    def resume(self, ident):
        record = self.store.get(ident)
        if not record or record['status'] != 'paused': return record
        record['status'] = record.get('resume_status') or 'queued'
        record['resume_status'] = None
        self.store.save(record)
        self.wake.set()
        return record

    def cancel(self, ident):
        record = self.store.get(ident)
        if not record or record['status'] in FINAL: return record
        if record.get('job_id'): self.discovery_store.cancel(record['job_id'])
        record['status'], record['resume_status'] = 'cancelled', None
        self.store.save(record)
        return record
