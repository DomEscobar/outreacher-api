"""Private SQLite job store; cross-thread calls use independent connections."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


class QueueFull(Exception): pass


class Store:
    def __init__(self, directory, max_queue=8, retention_days=30):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.path = directory / 'jobs.sqlite3'
        self.max_queue = max_queue
        self.retention = retention_days * 86400
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT NOT NULL,
              created REAL NOT NULL, updated REAL NOT NULL, criteria TEXT NOT NULL,
              result TEXT, error TEXT, cancelled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS seen(url TEXT PRIMARY KEY, updated REAL NOT NULL);
            ''')
            db.execute("UPDATE jobs SET status='failed',error='interrupted_restart',updated=? WHERE status IN ('queued','running')", (time.time(),))
        os.chmod(self.path, 0o600)
        self.cleanup()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def cleanup(self):
        cutoff = time.time() - self.retention
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE updated < ? AND status NOT IN ('queued','running')", (cutoff,))
            db.execute('DELETE FROM seen WHERE updated < ?', (cutoff,))
            # Erase freed pages, not merely logical records.
            db.execute('PRAGMA secure_delete=ON')

    def submit(self, criteria):
        self.cleanup()
        now = time.time()
        ident = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= self.max_queue: raise QueueFull()
            db.execute('INSERT INTO jobs(id,status,created,updated,criteria) VALUES(?,?,?,?,?)',
                       (ident, 'queued', now, now, criteria.model_dump_json()))
        return ident

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
        return dict(row) if row else None

    def list(self, status=None, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM jobs' + (' WHERE status=?' if status else '') + ' ORDER BY created DESC LIMIT ?',
                              ((status, limit) if status else (limit,))).fetchall()
        return [dict(r) for r in rows]

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND cancelled=0 ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
                return dict(row)

    def previous(self):
        self.cleanup()
        with self.connect() as db:
            return {r[0] for r in db.execute('SELECT url FROM seen')}

    def cancelled(self, ident):
        row = self.get(ident)
        return not row or bool(row['cancelled'])

    def cancel(self, ident):
        with self.connect() as db:
            db.execute("UPDATE jobs SET cancelled=1,status='failed',error='cancelled',result=NULL,updated=? WHERE id=? AND status IN ('queued','running')", (time.time(), ident))

    def finish(self, ident, status, result=None, error=None):
        from models import profile_key
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT cancelled FROM jobs WHERE id=?', (ident,)).fetchone()
            if not row or row[0]: return
            now = time.time()
            db.execute('UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?',
                       (status, json.dumps(result) if result is not None else None, error, now, ident))
            if status in {'succeeded', 'partial', 'blocked'} and result:
                for lead in result['leads']:
                    db.execute('INSERT OR REPLACE INTO seen VALUES(?,?)', (profile_key(lead['linkedin_url']), now))

    def delete(self, ident):
        with self.connect() as db:
            db.execute('DELETE FROM jobs WHERE id=?', (ident,))
        # Seen keys intentionally remain until retention; deletion does not re-enable duplicates.
