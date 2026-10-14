"""Private backend: deploy ONLY behind existing authenticated Caddy."""
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from models import Criteria
from outreach import OutreachConflict, OutreachRecord, OutreachRequest, OutreachService, OutreachStore, Status
from registry import (CheckResult, Registry, RegistryCheckRequest, RegistryEntry, RegistryStatus, RegistryUpsertRequest,
                      UpsertResult)
from response_models import JobResponse
from store import Store, QueueFull
from views import DiscoverySummary, LeadRow, SystemStatus, lead_rows, summarize, system_status
from worker import Worker

UI_FILE = Path(__file__).with_name('ui') / 'index.html'

MAX_BODY = 16384
MAX_BODY_BULK = 1_048_576  # registry check/import: up to 500 items per call


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
            if len(body) > (MAX_BODY_BULK if scope.get('path', '').startswith('/api/v1/registry') else MAX_BODY):
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
               search_backend=None, outreach_writer=None, outreach_reader=None):
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
            outreach_store = OutreachStore(directory)
            stored_leads = [l for job in app.state.store.list(limit=100000) if job['result']
                            for l in json.loads(job['result']).get('leads') or []]
            app.state.registry.backfill(app.state.store.previous(), outreach_store.list(limit=100000), stored_leads)
            app.state.worker = Worker(app.state.store, backend, search_backend, browser_lock, app.state.registry)
            app.state.outreach = OutreachService(outreach_store, outreach_writer, outreach_reader,
                                                 browser_lock, start=start_worker, registry=app.state.registry)
            if start_worker: app.state.worker.start()
            try: yield
            finally:
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
    def submit(criteria: Criteria):
        try: ident = app.state.store.submit(criteria)
        except QueueFull: raise HTTPException(429, 'queue_full') from None
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
        try:
            ident, created = app.state.outreach.create(request)
        except OutreachConflict as exc:
            raise HTTPException(409, {'error': exc.code, 'registry': exc.registry_hit}) from None
        if not created: raise HTTPException(409, {'error': 'outreach_exists', 'id': ident})
        return get_outreach(ident)

    @app.get('/api/v1/outreach', response_model=list[OutreachRecord])
    def outreach_list(status: Status | None = None):
        return app.state.outreach.store.list(status)

    @app.get('/api/v1/outreach/{ident}', response_model=OutreachRecord)
    def outreach_get(ident: str):
        return get_outreach(ident)

    @app.post('/api/v1/outreach/{ident}/sent', response_model=OutreachRecord)
    def outreach_sent(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_sent(ident)

    @app.get('/api/v1/discoveries', response_model=list[DiscoverySummary])
    def discoveries_list(status: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        return [summarize(r) for r in app.state.store.list(status, limit)]

    @app.get('/api/v1/leads', response_model=list[LeadRow])
    def leads(verification: Literal['verified', 'plausible', 'linkedin', 'review'] | None = None,
              q: str | None = Query(None, max_length=160), limit: int = Query(500, ge=1, le=5000)):
        return lead_rows(app.state.store, app.state.outreach.store, verification, q, limit, app.state.registry)

    @app.get('/api/v1/status', response_model=SystemStatus)
    def status():
        return system_status(app.state.store, app.state.outreach, app.state.registry)

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

    @app.delete('/api/v1/outreach/{ident}', status_code=204)
    def outreach_delete(ident: str):
        get_outreach(ident)
        app.state.outreach.delete(ident)

    @app.post('/api/v1/outreach/{ident}/do-not-contact', response_model=OutreachRecord)
    def outreach_do_not_contact(ident: str):
        get_outreach(ident)
        return app.state.outreach.mark_do_not_contact(ident)

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
