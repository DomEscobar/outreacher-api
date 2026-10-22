"""Message templates ("Vorlagen"): a fixed text with {{placeholders}} plus follow-ups, chosen per search.

A search (discovery job) can be assigned a template; its leads then get that template when the human clicks
"Vernetzen". One template is the default for everything else. Contacts keep a copy of the template at creation,
so editing a template later never changes drafts that already exist.
"""
from contextlib import contextmanager
import json
import os
import secrets
import sqlite3
import threading
import time

from pydantic import Field

from models import Strict, Text
from outreach import FollowUpTemplate, LongText, Topic


class TemplateRequest(Strict):
    name: Text
    topic: Topic | None = None  # what you offer, context for the model; the name when empty
    message: LongText
    followups: list[FollowUpTemplate] = Field(default_factory=list, max_length=5)
    is_default: bool = False


class TemplateRecord(TemplateRequest):
    id: str
    created_at: float
    updated_at: float


class TemplateStore:
    """Private SQLite (0600) next to the other stores."""
    def __init__(self, directory):
        self.path = os.path.join(str(directory), 'templates.sqlite3')
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS templates(id TEXT PRIMARY KEY, updated REAL NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS job_templates(job_id TEXT PRIMARY KEY, template_id TEXT NOT NULL)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA secure_delete=ON')
            yield db
        finally:
            db.close()

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT record FROM templates ORDER BY updated DESC').fetchall()
        records = [json.loads(r[0]) for r in rows]
        return sorted(records, key=lambda r: not r['is_default'])  # the default first

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT record FROM templates WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def default(self):
        records = self.list()
        return records[0] if records else None

    def save(self, request, ident=None):
        """Create (ident None) or replace a template. The first template, or one marked default, becomes the default."""
        now = time.time()
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT record FROM templates WHERE id=?', (ident,)).fetchone() if ident else None
            if ident and not old: return None
            record = TemplateRecord(id=ident or secrets.token_urlsafe(12), created_at=json.loads(old[0])['created_at'] if old else now,
                                    updated_at=now, **request.model_dump()).model_dump()
            others = [json.loads(r[0]) for r in db.execute('SELECT record FROM templates WHERE id<>?', (record['id'],))]
            if not any(o['is_default'] for o in others): record['is_default'] = True  # always exactly one default
            if record['is_default']:
                for o in others:
                    if o['is_default']:
                        o['is_default'] = False
                        db.execute('UPDATE templates SET record=? WHERE id=?', (json.dumps(o), o['id']))
            db.execute('INSERT OR REPLACE INTO templates VALUES(?,?,?)', (record['id'], now, json.dumps(record)))
            db.execute('COMMIT')
        return record

    def delete(self, ident):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM templates WHERE id=?', (ident,)).fetchone()
            if not row:
                db.execute('ROLLBACK')
                return False
            db.execute('DELETE FROM templates WHERE id=?', (ident,))
            db.execute('DELETE FROM job_templates WHERE template_id=?', (ident,))
            if json.loads(row[0])['is_default']:  # hand the default to the most recently edited one
                nxt = db.execute('SELECT record FROM templates ORDER BY updated DESC LIMIT 1').fetchone()
                if nxt:
                    record = {**json.loads(nxt[0]), 'is_default': True}
                    db.execute('UPDATE templates SET record=? WHERE id=?', (json.dumps(record), record['id']))
            db.execute('COMMIT')
        return True

    def assign(self, job_id, template_id):
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO job_templates VALUES(?,?)', (job_id, template_id))

    def job_map(self):
        with self.connect() as db:
            return dict(db.execute('SELECT job_id, template_id FROM job_templates').fetchall())
