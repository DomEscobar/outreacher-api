"""Per-criteria search memory for source="search": a repeat search digs into new corners instead of
re-reading and re-paying for the same results.

For each criteria signature (sector terms, titles, locations, min_verification) it keeps for 90 days:
- every query run, when, and how many of its results were still open when the run ended (found but
  neither delivered, rejected nor checked, e.g. because the target was reached first);
- pseudonymous keys (SHA-256 of signature + profile or firm host) of profiles and firm imprints already
  decided on: rejected at classification, checked below the target tier, delivered, or imprint read.
  Later runs skip them. Open candidates (not yet checked, or counting but beyond the limit) are not
  recorded and come back.
A query run within REPEAT_DAYS that left nothing open moves to the end of the plan, so fresh facets,
places and wordings come first. Queries are stored as run (sector terms, titles, place); no names,
quotes, profile URLs or other lead data are stored.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
import time

RETENTION_DAYS = 90
REPEAT_DAYS = 30


def signature(criteria):
    values = lambda items: sorted({v.strip().casefold() for v in items})
    data = {'keywords': values(criteria.keywords), 'titles': values(criteria.titles),
            'locations': values(criteria.locations), 'min_verification': criteria.min_verification}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SearchMemory:
    """Private SQLite file (0600) next to the job store."""
    def __init__(self, directory, retention_days=RETENTION_DAYS):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.retention = directory / 'search_memory.sqlite3', retention_days * 86400
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS queries(sig TEXT NOT NULL, query TEXT NOT NULL, last_run REAL NOT NULL,
              open_results INTEGER NOT NULL, runs INTEGER NOT NULL, PRIMARY KEY(sig, query));
            CREATE TABLE IF NOT EXISTS decided(sig TEXT NOT NULL, item TEXT NOT NULL, updated REAL NOT NULL,
              PRIMARY KEY(sig, item));
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def view(self, criteria):
        cutoff = time.time() - self.retention
        sig = signature(criteria)
        with self.connect() as db:
            db.execute('DELETE FROM queries WHERE last_run < ?', (cutoff,))
            db.execute('DELETE FROM decided WHERE updated < ?', (cutoff,))
            queries = {q: (t, n) for q, t, n in db.execute('SELECT query, last_run, open_results FROM queries WHERE sig=?', (sig,))}
            decided = {r[0] for r in db.execute('SELECT item FROM decided WHERE sig=?', (sig,))}
        return MemoryView(self, sig, queries, decided)

    def save(self, sig, ran, decided):
        """`ran`: query -> results still open at the end of the run; `decided`: hashed keys."""
        now = time.time()
        with self.connect() as db:
            for query, still_open in ran.items():
                db.execute('INSERT INTO queries(sig, query, last_run, open_results, runs) VALUES(?,?,?,?,1) '
                           'ON CONFLICT(sig, query) DO UPDATE SET last_run=excluded.last_run, '
                           'open_results=excluded.open_results, runs=runs+1', (sig, query, now, still_open))
            db.executemany('INSERT INTO decided(sig, item, updated) VALUES(?,?,?) '
                           'ON CONFLICT(sig, item) DO UPDATE SET updated=excluded.updated',
                           [(sig, item, now) for item in decided])


class MemoryView:
    """One job's view: what earlier runs with the same criteria ran and decided; collects this run's."""
    def __init__(self, memory, sig, queries, decided):
        self.memory, self.sig, self.queries, self.decided = memory, sig, queries, decided
        # found: query -> {(kind, value)} new to these criteria; decided_now: this run's decisions (clear in memory only)
        self.found, self.decided_now, self.lock = {}, set(), Lock()

    def key(self, kind, value):
        return hashlib.sha256(f'{self.sig}\n{kind}\n{value}'.encode()).hexdigest()

    def known(self, kind, value):
        return self.key(kind, value) in self.decided

    def decide(self, kind, value):
        with self.lock: self.decided_now.add((kind, value))

    def note(self, query, kind, values):
        """The query ran; `values` are its results that were new to these criteria (may be none)."""
        with self.lock: self.found.setdefault(query, set()).update((kind, v) for v in values)

    def order(self, queries):
        """Plan order, except queries run within REPEAT_DAYS that left nothing open go last. Returns (queries, demoted)."""
        recent = time.time() - REPEAT_DAYS * 86400
        spent = {q for q in queries if q in self.queries and self.queries[q][0] >= recent and self.queries[q][1] == 0}
        return [q for q in queries if q not in spent] + [q for q in queries if q in spent], len(spent)

    def save(self):
        with self.lock:
            still_open = {q: len(found - self.decided_now) for q, found in self.found.items()}
            decided = {self.key(kind, value) for kind, value in self.decided_now}
        self.memory.save(self.sig, still_open, decided)
