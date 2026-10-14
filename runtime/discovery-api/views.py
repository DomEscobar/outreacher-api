"""Read-only overview models for the UI: job list, flattened leads, system status."""
from collections import Counter
import json
import time
from typing import Literal

from pydantic import Field

from models import Lead, Strict, profile_key
import outreach as o
import search_backend as sb

JobStatus = Literal['queued', 'running', 'succeeded', 'partial', 'blocked', 'failed', 'timed_out']


class DiscoverySummary(Strict):
    id: str
    status: JobStatus
    created_at: float
    updated_at: float
    source: str
    keywords: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    requested_count: int
    returned_count: int = 0
    missing_count: int = 0
    verification_counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float | None = None
    stop_reason: str | None = None
    error: str | None = None


class LeadRow(Lead):
    job_id: str
    found_at: float
    outreach_id: str | None = None
    outreach_status: o.Status | None = None
    registry_status: str | None = None                          # this person in the shared registry
    company_registry: dict[str, int] = Field(default_factory=dict)  # other entries at the same company, by status


class Readiness(Strict):
    configured: bool
    treg: bool
    openrouter: bool
    model: str


class OutreachHealth(Strict):
    counts: dict[str, int]
    checks_last_24h: int
    daily_cap: int
    background_enabled: bool
    paused_until: float | None = None


class SystemStatus(Strict):
    search: Readiness
    queue: dict[str, int]
    jobs_total: int
    leads_total: int
    outreach: OutreachHealth
    registry: dict[str, int] = Field(default_factory=dict)
    time: float


def summarize(row):
    criteria = json.loads(row['criteria'])
    result = json.loads(row['result']) if row['result'] else {}
    meta = result.get('metadata') or {}
    return DiscoverySummary(
        id=row['id'], status=row['status'], created_at=row['created'], updated_at=row['updated'],
        source=criteria.get('source', 'linkedin'), keywords=criteria.get('keywords') or [], titles=criteria.get('titles') or [],
        locations=criteria.get('locations') or [], requested_count=criteria['limit'],
        returned_count=result.get('returned_count', 0), missing_count=result.get('missing_count', criteria['limit']),
        verification_counts=meta.get('verification_counts') or {}, cost_usd=meta.get('cost_usd'),
        stop_reason=meta.get('stop_reason'), error=row['error']).model_dump()


def lead_rows(store, outreach_store, verification=None, q=None, limit=500, registry=None):
    """All delivered leads, newest first, one row per LinkedIn profile, annotated with outreach state."""
    contacts = {profile_key(r['linkedin_url']): r for r in outreach_store.list(limit=10000)}
    needle = sb.norm(q) if q else None
    rows, seen = [], set()
    for job in store.list(limit=1000):
        if not job['result']: continue
        for lead in json.loads(job['result']).get('leads') or []:
            key = profile_key(lead['linkedin_url'])
            if key in seen: continue
            seen.add(key)
            if verification and lead.get('verification') != verification: continue
            if needle and needle not in sb.norm(' '.join(str(lead.get(k) or '') for k in ('name', 'current_role', 'company', 'company_website'))):
                continue
            contact = contacts.get(key)
            hit = registry.check({'linkedin_url': lead['linkedin_url'], 'company_website': lead.get('company_website'),
                                  'company_name': lead.get('company')}) if registry else {}
            rows.append(LeadRow.model_validate({**lead, 'job_id': job['id'], 'found_at': job['updated'],
                                                'outreach_id': contact['id'] if contact else None,
                                                'outreach_status': contact['status'] if contact else None,
                                                'registry_status': (hit.get('person') or {}).get('status'),
                                                'company_registry': (hit.get('company') or {}).get('statuses') or {}}).model_dump())
            if len(rows) >= limit: return rows
    return rows


def system_status(store, service, registry=None):
    treg, openrouter = bool(sb.env_value('TREG_TOKEN')), bool(sb.openrouter_key())
    jobs = store.list(limit=100000)
    leads = len({profile_key(l['linkedin_url']) for j in jobs if j['result'] for l in json.loads(j['result']).get('leads') or []})
    contacts = service.store.list(limit=100000)
    counts = {}
    for c in contacts: counts[c['status']] = counts.get(c['status'], 0) + 1
    paused = service.paused_until if service.paused_until > time.time() else None
    return SystemStatus(
        search=Readiness(configured=treg and openrouter, treg=treg, openrouter=openrouter,
                         model=sb.env_value('DISCOVERY_SEARCH_MODEL') or sb.DEFAULT_MODEL),
        queue={s: sum(j['status'] == s for j in jobs) for s in ('queued', 'running')},
        jobs_total=len(jobs), leads_total=leads,
        registry=dict(Counter(e['status'] for e in registry.list(limit=1_000_000))) if registry else {},
        outreach=OutreachHealth(counts=counts, checks_last_24h=service.store.checks_since(time.time() - 86400),
                                daily_cap=o.DAILY_CHECK_CAP, background_enabled=service.thread.is_alive(),
                                paused_until=paused),
        time=time.time()).model_dump()
