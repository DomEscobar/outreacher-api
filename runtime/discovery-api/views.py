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
    template_id: str | None = None  # message template its leads get


class LeadRow(Lead):
    job_id: str
    found_at: float
    template_id: str | None = None                              # from the search that found it (else the default)
    outreach_id: str | None = None
    outreach_status: o.Status | None = None
    outreach_connection: o.ConnectionState | None = None        # pending = invitation seen under "sent"
    outreach_message: str | None = None                         # draft to copy once connected
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
    reply_check_enabled: bool = False
    last_inbox_check: float | None = None  # last read of the inbox's conversation list (reply check)
    next_inbox_check: float | None = None  # earliest next look (working hours, jitter, backoff)
    inbox_reads_today: int = 0
    invite_check_enabled: bool = False
    last_invite_check: float | None = None  # last complete look at sent invitations + newest connections
    next_invite_check: float | None = None
    invite_reads_today: int = 0
    invite_daily_cap: int = 0
    connect_enabled: bool = False
    connect_last_24h: int = 0
    connect_daily_cap: int = 0
    connect_last_7d: int = 0
    connect_weekly_cap: int = 0


class SystemStatus(Strict):
    search: Readiness
    queue: dict[str, int]
    jobs_total: int
    leads_total: int
    outreach: OutreachHealth
    registry: dict[str, int] = Field(default_factory=dict)
    campaigns: dict[str, int] = Field(default_factory=dict)
    webhooks: int = 0
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


def lead_rows(store, outreach_store, verification=None, q=None, limit=500, registry=None, job_templates=None):
    """All delivered leads, newest first, one row per LinkedIn profile, annotated with outreach state."""
    job_templates = job_templates or {}
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
                                                'template_id': job_templates.get(job['id']),
                                                'outreach_id': contact['id'] if contact else None,
                                                'outreach_status': contact['status'] if contact else None,
                                                'outreach_connection': contact['connection'].get('state') if contact else None,
                                                'outreach_message': contact.get('message') if contact else None,
                                                'registry_status': (hit.get('person') or {}).get('status'),
                                                'company_registry': (hit.get('company') or {}).get('statuses') or {}}).model_dump())
            if len(rows) >= limit: return rows
    return rows


def system_status(store, service, registry=None, campaign_store=None, webhook_store=None):
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
        campaigns=dict(Counter(c['status'] for c in campaign_store.list(limit=10000))) if campaign_store else {},
        webhooks=len(webhook_store.list(active_only=True)) if webhook_store else 0,
        outreach=OutreachHealth(counts=counts, checks_last_24h=service.store.checks_since(time.time() - 86400),
                                daily_cap=o.DAILY_CHECK_CAP, background_enabled=service.thread.is_alive(),
                                paused_until=paused, reply_check_enabled=o.REPLY_CHECK and service.thread.is_alive(),
                                last_inbox_check=service.store.meta('last_inbox_check'),
                                next_inbox_check=service.store.meta('next_inbox_check'),
                                inbox_reads_today=service.reads_today('inbox', time.time()),
                                invite_check_enabled=o.INVITE_CHECK and service.thread.is_alive(),
                                last_invite_check=service.store.meta('last_invite_check'),
                                next_invite_check=service.store.meta('next_invite_check'),
                                invite_reads_today=service.reads_today('invite', time.time()),
                                invite_daily_cap=o.INVITE_DAILY_CAP,
                                connect_enabled=service.connector is not None and service.thread.is_alive(),
                                connect_last_24h=service.store.connects_since(time.time() - 86400),
                                connect_daily_cap=o.CONNECT_DAILY_CAP,
                                connect_last_7d=service.store.connects_since(time.time() - 7 * 86400),
                                connect_weekly_cap=o.CONNECT_WEEKLY_CAP),
        time=time.time()).model_dump()
