"""Private backend: deploy ONLY behind existing authenticated Caddy."""
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from actions import build_actions
from campaigns import CampaignRecord, CampaignRequest, CampaignService, CampaignStore
from events import events_since
from models import Criteria
from outreach import (OutreachConflict, OutreachPatch, OutreachRecord, OutreachRequest, OutreachService, OutreachStore,
                      Status)
from webhooks import (WebhookCreated, WebhookDispatcher, WebhookRequest, WebhookStore, WebhookView,
                      view as webhook_view)
from registry import (CheckResult, Registry, RegistryCheckRequest, RegistryEntry, RegistryStatus, RegistryUpsertRequest,
                      UpsertResult)
from response_models import JobResponse
from search_memory import SearchMemory
from store import Store, QueueFull
from templates import TemplateRecord, TemplateRequest, TemplateStore
from views import DiscoverySummary, LeadRow, SystemStatus, lead_rows, summarize, system_status
from worker import Worker

UI_FILE = Path(__file__).with_name('ui') / 'index.html'

MAX_BODY = 16384
MAX_BODY_BULK = 1_048_576  # registry check/import: up to 500 items per call; templates with long follow-ups


class BodyLimit:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        # Read a bounded request body before FastAPI validation (including chunked requests).
        body = b''
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect': return
            body += message.get('body', b'')
            if len(body) > (MAX_BODY_BULK if scope.get('path', '').startswith(('/api/v1/registry', '/api/v1/templates')) else MAX_BODY):
                await JSONResponse({'detail': 'request_too_large'}, status_code=413)(scope, receive, send)
                return
            if not message.get('more_body'): break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()
        await self.app(scope, bounded_receive, send)


def create_app(directory=None, origin=None, backend=None, start_worker=True, max_queue=8, retention_days=30,
               search_backend=None, outreach_writer=None, outreach_reader=None, outreach_connector=None):
    directory = Path(directory or os.environ.get('DISCOVERY_DATA_DIR', '/opt/data/discovery-api'))
    origin = origin or os.environ.get('DISCOVERY_ALLOWED_ORIGIN')

    @asynccontextmanager
    async def lifespan(app):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        lock = open(directory / 'worker.lock', 'a')
        os.chmod(directory / 'worker.lock', 0o600)
        try:
            # More than one uvicorn worker/service against this DB must fail, not double-drive Chromium.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            app.state.store = Store(directory, max_queue, retention_days)
            browser_lock = threading.Lock()  # agent jobs and outreach checks never share Chromium at once
            app.state.registry = Registry(directory)
            app.state.templates = TemplateStore(directory)
            outreach_store = OutreachStore(directory)
            stored_leads = [l for job in app.state.store.list(limit=100000) if job['result']
                            for l in json.loads(job['result']).get('leads') or []]
            app.state.registry.backfill(app.state.store.previous(), outreach_store.list(limit=100000), stored_leads)
            app.state.worker = Worker(app.state.store, backend, search_backend, browser_lock, app.state.registry,
                                      SearchMemory(directory))
            app.state.outreach = OutreachService(outreach_store, outreach_writer, outreach_reader,
                                                 browser_lock, start=start_worker, registry=app.state.registry,
                                                 connector=outreach_connector)
            app.state.campaigns = CampaignService(CampaignStore(directory), app.state.store, app.state.outreach,
                                                  app.state.worker, start=start_worker, templates=app.state.templates)
            app.state.webhooks = WebhookDispatcher(WebhookStore(directory), app.state.outreach, app.state.store,
                                                   app.state.campaigns.store, start=start_worker)
            if start_worker: app.state.worker.start()
            try: yield
            finally:
                app.state.webhooks.close()
                app.state.campaigns.close()
                app.state.outreach.close()
                if start_worker: app.state.worker.close()
        finally:
            lock.close()

    app = FastAPI(title='On-demand professional discovery', version='1.0.0', lifespan=lifespan,
                  openapi_url='/api/v1/openapi.json', docs_url='/api/v1/docs', redoc_url=None)
    app.add_middleware(BodyLimit)

    @app.middleware('http')
    async def security(request: Request, call_next):
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            supplied = request.headers.get('origin')
            if supplied is not None and (not origin or supplied != origin):
                return JSONResponse({'detail': 'origin_forbidden'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # No echo of rejected criteria, URLs, model output or exception strings.
        return JSONResponse({'detail': 'invalid_request'}, status_code=422)

    def get_job(ident):
        row = app.state.store.get(ident)
        if not row: raise HTTPException(404, 'job_not_found')
        return row

    def render(row):
        criteria = json.loads(row['criteria'])
        result = json.loads(row['result']) if row['result'] else None
        return {'id': row['id'], 'status': row['status'], 'created_at': row['created'],
                'updated_at': row['updated'], 'requested_count': criteria['limit'],
                'returned_count': result['returned_count'] if result else 0,
                'missing_count': result['missing_count'] if result else criteria['limit'],
                'metadata': {'retention_days': retention_days, 'discovery_only': True},
                'result': result, 'error': row['error']}

    @app.post('/api/v1/discoveries', status_code=202, response_model=JobResponse)
    def submit(criteria: Criteria, template_id: str | None = Query(None, max_length=64)):
        """`template_id`: the message template the leads of this search get (a "campaign" = search + template)."""
        if template_id and not app.state.templates.get(template_id): raise HTTPException(404, 'template_not_found')
        try: ident = app.state.store.submit(criteria)
        except QueueFull: raise HTTPException(429, 'queue_full') from None
        if template_id: app.state.templates.assign(ident, template_id)
        app.state.worker.wake.set()
        return render(get_job(ident))

    @app.get('/api/v1/discoveries/{ident}', response_model=JobResponse)
    def get(ident: str):
        return render(get_job(ident))

    @app.post('/api/v1/discoveries/{ident}/cancel', response_model=JobResponse)
    def cancel(ident: str):
        get_job(ident)
        app.state.store.cancel(ident)
        app.state.worker.wake.set()
        return render(get_job(ident))

    def get_outreach(ident):
        record = app.state.outreach.store.get(ident)[1]
        if not record: raise HTTPException(404, 'outreach_not_found')
        return record

    @app.post('/api/v1/outreach', status_code=201, response_model=OutreachRecord)
    def outreach_create(request: OutreachRequest):
        if request.template_id:  # a stored template: the contact keeps a copy of its texts
            template = app.state.templates.get(request.template_id)
            if not template: raise HTTPException(404, 'template_not_found')
            request = OutreachRequest.model_validate({**request.model_dump(), 'template': template['message'],
                                                     'followups': template['followups'], 'topic': template['topic'] or template['name']})
        try:
            ident, created = app.state.outreach.create(request)
        except OutreachConflict as exc:
            raise HTTPException(409, {'error': exc.code, 'registry': exc.registry_hit}) from None
        if not created: raise HTTPException(409, {'error': 'outreach_exists', 'id': ident})
        return get_outreach(ident)

    @app.get('/api/v1/outreach', response_model=list[OutreachRecord])
    def outreach_list(status: Status | None = None, campaign_id: str | None = Query(None, max_length=64)):
        return [r for r in app.state.outreach.store.list(status)
                if not campaign_id or r.get('campaign_id') == campaign_id]

    @app.get('/api/v1/outreach/{ident}', response_model=OutreachRecord)
    def outreach_get(ident: str):
        return get_outreach(ident)

    @app.patch('/api/v1/outreach/{ident}', response_model=OutreachRecord)
    def outreach_patch(ident: str, patch: OutreachPatch):
        """Review edit of the note/message/topic; never sends anything."""
        get_outreach(ident)
        try:
            return app.state.outreach.update(ident, patch)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/v1/outreach/{ident}/sent', response_model=OutreachRecord)
    def outreach_sent(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_sent(ident)

    @app.post('/api/v1/outreach/{ident}/replied', response_model=OutreachRecord)
    def outreach_replied(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_replied(ident)

    @app.post('/api/v1/outreach/{ident}/opened', response_model=OutreachRecord)
    def outreach_opened(ident: str):
        """The human opened the profile to connect by hand (UI "Vernetzen ↗"). Agents never call this."""
        get_outreach(ident)
        return app.state.outreach.mark_opened(ident)

    @app.post('/api/v1/outreach/{ident}/followups/{index}/sent', response_model=OutreachRecord)
    def outreach_followup_sent(ident: str, index: int):
        """The human copied this follow-up into LinkedIn (nothing is sent by the box)."""
        get_outreach(ident)
        try:
            return app.state.outreach.mark_followup_sent(ident, index)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/v1/outreach/{ident}/reply-handled', response_model=OutreachRecord)
    def outreach_reply_handled(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_reply_handled(ident)

    @app.post('/api/v1/outreach/{ident}/no-reply', response_model=OutreachRecord)
    def outreach_no_reply(ident: str):
        get_outreach(ident)
        return app.state.outreach.unmark_replied(ident)

    @app.get('/api/v1/discoveries', response_model=list[DiscoverySummary])
    def discoveries_list(status: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        templates = app.state.templates.job_map()
        return [{**summarize(r), 'template_id': templates.get(r['id'])} for r in app.state.store.list(status, limit)]

    @app.get('/api/v1/leads', response_model=list[LeadRow])
    def leads(verification: Literal['verified', 'plausible', 'linkedin', 'review'] | None = None,
              q: str | None = Query(None, max_length=160), limit: int = Query(500, ge=1, le=5000)):
        return lead_rows(app.state.store, app.state.outreach.store, verification, q, limit, app.state.registry,
                         app.state.templates.job_map())

    # -- message templates ("Vorlagen"), chosen per search
    def get_template(ident):
        record = app.state.templates.get(ident)
        if not record: raise HTTPException(404, 'template_not_found')
        return record

    @app.get('/api/v1/templates', response_model=list[TemplateRecord])
    def template_list():
        return app.state.templates.list()

    @app.post('/api/v1/templates', status_code=201, response_model=TemplateRecord)
    def template_create(request: TemplateRequest):
        return app.state.templates.save(request)

    @app.get('/api/v1/templates/{ident}', response_model=TemplateRecord)
    def template_get(ident: str):
        return get_template(ident)

    @app.put('/api/v1/templates/{ident}', response_model=TemplateRecord)
    def template_update(ident: str, request: TemplateRequest):
        get_template(ident)
        return app.state.templates.save(request, ident)

    @app.delete('/api/v1/templates/{ident}', status_code=204)
    def template_delete(ident: str):
        if not app.state.templates.delete(ident): raise HTTPException(404, 'template_not_found')

    @app.get('/api/v1/status', response_model=SystemStatus)
    def status():
        return system_status(app.state.store, app.state.outreach, app.state.registry,
                             app.state.campaigns.store, app.state.webhooks.store)

    @app.get('/ui', include_in_schema=False)
    def ui_redirect():
        return RedirectResponse('/ui/', status_code=308)

    @app.get('/ui/', include_in_schema=False, response_class=HTMLResponse)
    def ui():
        # Single self-contained page (no external scripts/fonts); served behind the same Basic Auth.
        return HTMLResponse(UI_FILE.read_text(encoding='utf-8'), headers={
            'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    @app.post('/api/v1/outreach/{ident}/accepted', response_model=OutreachRecord)
    def outreach_accepted(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_accepted(ident)

    @app.post('/api/v1/outreach/{ident}/connect', response_model=OutreachRecord)
    def outreach_connect(ident: str):
        """Retry a failed/manual connection request (sent by the box, not by an agent)."""
        get_outreach(ident)
        return app.state.outreach.retry_connect(ident)

    @app.post('/api/v1/outreach/{ident}/check-connection', response_model=OutreachRecord)
    def outreach_check_connection(ident: str):
        get_outreach(ident)
        return app.state.outreach.check_connection(ident)

    @app.post('/api/v1/outreach/sync-invitations')
    def outreach_sync_invitations():
        """Read the sent invitations and the newest connections now (read-only, counts only). Ignores the
        schedule and the daily cap (a deliberate user action), never a pause."""
        result = app.state.outreach.invitation_check(manual=True)
        if result is None:
            paused = app.state.outreach.paused_until > time.time()
            return {'checked': False, 'reason': 'paused' if paused else 'nothing_waiting'}
        return result

    @app.post('/api/v1/outreach/resume')
    def outreach_resume():
        """A human fixed the login/challenge via noVNC: clear the persisted pause now."""
        app.state.outreach.resume()
        return {'paused_until': None}

    @app.delete('/api/v1/outreach/{ident}', status_code=204)
    def outreach_delete(ident: str):
        get_outreach(ident)
        app.state.outreach.delete(ident)

    @app.post('/api/v1/outreach/{ident}/do-not-contact', response_model=OutreachRecord)
    def outreach_do_not_contact(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_do_not_contact(ident)

    # -- agent primitives: change feed and prioritized next actions
    @app.get('/api/v1/events')
    def events(since: float = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        items, cursor = events_since(app.state.outreach, app.state.store, app.state.campaigns.store, since, limit)
        return {'events': items, 'next_cursor': cursor}

    @app.get('/api/v1/actions')
    def actions(kind: str | None = Query(None, max_length=40), limit: int = Query(100, ge=1, le=500)):
        return build_actions(app.state.outreach, app.state.store, app.state.campaigns.store, kind=kind, limit=limit)

    # -- campaigns: one call runs discovery -> outreach -> connect -> acceptance -> research/draft
    def get_campaign(ident):
        record = app.state.campaigns.store.get(ident)
        if not record: raise HTTPException(404, 'campaign_not_found')
        return record

    @app.post('/api/v1/campaigns', status_code=201, response_model=CampaignRecord)
    def campaign_create(request: CampaignRequest):
        if request.template_id:  # the campaign keeps a copy of the template's texts
            template = app.state.templates.get(request.template_id)
            if not template: raise HTTPException(404, 'template_not_found')
            request = CampaignRequest.model_validate({**request.model_dump(), 'template': template['message'],
                                                      'followups': template['followups'], 'topic': template['topic'] or template['name']})
        record = app.state.campaigns.store.create(request)
        app.state.campaigns.wake.set()
        return record

    @app.get('/api/v1/campaigns', response_model=list[CampaignRecord])
    def campaign_list(status: str | None = Query(None, max_length=24)):
        return app.state.campaigns.store.list(status)

    @app.get('/api/v1/campaigns/{ident}', response_model=CampaignRecord)
    def campaign_get(ident: str):
        return get_campaign(ident)

    @app.post('/api/v1/campaigns/{ident}/pause', response_model=CampaignRecord)
    def campaign_pause(ident: str):
        get_campaign(ident)
        return app.state.campaigns.pause(ident)

    @app.post('/api/v1/campaigns/{ident}/resume', response_model=CampaignRecord)
    def campaign_resume(ident: str):
        get_campaign(ident)
        return app.state.campaigns.resume(ident)

    @app.post('/api/v1/campaigns/{ident}/cancel', response_model=CampaignRecord)
    def campaign_cancel(ident: str):
        get_campaign(ident)
        return app.state.campaigns.cancel(ident)

    @app.delete('/api/v1/campaigns/{ident}', status_code=204)
    def campaign_delete(ident: str):
        get_campaign(ident)
        app.state.campaigns.store.delete(ident)

    # -- webhooks: signed push of events to an agent
    @app.post('/api/v1/webhooks', status_code=201, response_model=WebhookCreated)
    def webhook_create(request: WebhookRequest):
        record = app.state.webhooks.store.create(request)
        return {**webhook_view(record), 'secret': record['secret']}
    @app.get('/api/v1/webhooks', response_model=list[WebhookView])
    def webhook_list():
        return [webhook_view(r) for r in app.state.webhooks.store.list()]

    @app.delete('/api/v1/webhooks/{ident}', status_code=204)
    def webhook_delete(ident: str):
        if not app.state.webhooks.store.delete(ident): raise HTTPException(404, 'webhook_not_found')

    # -- shared registry: lookup / suppression list for this box and external agents
    @app.post('/api/v1/registry/check', response_model=list[CheckResult])
    def registry_check(request: RegistryCheckRequest):
        return [{'index': i, **app.state.registry.check(item.model_dump())} for i, item in enumerate(request.items)]

    @app.post('/api/v1/registry', response_model=UpsertResult)
    def registry_upsert(request: RegistryUpsertRequest):
        return app.state.registry.upsert_many([item.model_dump() for item in request.items])

    @app.get('/api/v1/registry', response_model=list[RegistryEntry])
    def registry_list(status: RegistryStatus | None = None, source: str | None = Query(None, max_length=160),
                      q: str | None = Query(None, max_length=160), limit: int = Query(500, ge=1, le=10000)):
        return app.state.registry.list(status, source, q, limit)

    @app.get('/api/v1/registry/{ident}', response_model=RegistryEntry)
    def registry_get(ident: str):
        entry = app.state.registry.get(ident)
        if not entry: raise HTTPException(404, 'registry_entry_not_found')
        return entry

    @app.delete('/api/v1/registry/{ident}', status_code=204)
    def registry_delete(ident: str):
        if not app.state.registry.delete(ident): raise HTTPException(404, 'registry_entry_not_found')

    @app.delete('/api/v1/discoveries/{ident}', status_code=204)
    def delete(ident: str):
        get_job(ident)
        app.state.store.cancel(ident)
        app.state.store.delete(ident)
        app.state.worker.wake.set()

    return app


app = create_app()
