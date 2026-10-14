"""All fixtures are EXPLICITLY SYNTHETIC. Tests do not call a model or live browser."""
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from pydantic import ValidationError
from app import create_app
from models import Budget, Criteria, DiscoveryResult, Lead, accepted_leads, public_url
from store import QueueFull, Store
from worker import BackendError, HermesBackend, Worker, command, parse_events

PROFILE = 'https://www.linkedin.com/in/synthetic-fixture-person'
SOURCE = 'https://www.example.com/team'
QUOTE = f'Synthetic Fixture Person is Synthetic Engineer at Synthetic Company. Profile: {PROFILE}'
LEAD = {'name': 'Synthetic Fixture Person', 'current_role': 'Synthetic Engineer',
        'company': 'Synthetic Company', 'company_website': None, 'linkedin_url': PROFILE,
        'evidence': [{'url': PROFILE, 'quote': QUOTE, 'supports': ['name', 'current_role', 'company', 'linkedin_url', 'sector']}],
        'identity_status': 'verified', 'missing_information': [], 'fit_reason': 'Synthetic fixture only'}
OBS = [PROFILE + '\n' + QUOTE]


def result(leads=None, outcome='completed', blocked_reason=None):
    return DiscoveryResult.model_validate({'outcome': outcome, 'leads': leads if leads is not None else [copy.deepcopy(LEAD)],
                                           'blocked_reason': blocked_reason})


def criteria(**kwargs): return Criteria(keywords=['synthetic'], **kwargs)


class ContractTests(unittest.TestCase):
    def test_unknown_fields(self):
        for value in [{'prompt': 'execute'}, {'shell': 'id'}, {'keywords': ['x'], 'extra': 1}]:
            with self.subTest(value=value), self.assertRaises(ValidationError): Criteria.model_validate(value)

    def test_type_and_bounds(self):
        for update in [{'limit': 0}, {'limit': 31}, {'limit': '1'}, {'limit': True}, {'exclude_previous': 'yes'},
                       {'source': 'anything'}, {'keywords': ['\n']}, {'keywords': ['x' * 161]},
                       {'budget': {'seconds': 1801}}, {'budget': {'seconds': 14}},
                       {'budget': {'max_turns': 121}}, {'budget': {'max_turns': 0}}, {'budget': {'prompt': 'x'}}]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                Criteria.model_validate({'keywords': ['x'], **update})

    def test_opaque_ids(self):
        c = criteria(geo_urns=['urn:li:geo:0123'], current_company_ids=['000123'], network=['F','S','O'])
        p = parse_qs(urlsplit(c.target_url()).query)
        self.assertEqual(json.loads(p['geoUrn'][0]), ['urn:li:geo:0123'])
        self.assertEqual(json.loads(p['currentCompany'][0]), ['000123'])
        for update in [{'geo_urns': ['Berlin']}, {'current_company_ids': ['urn:li:company:3']}, {'network': ['FIRST']}]:
            with self.assertRaises(ValidationError): criteria(**update)

    def test_url_encode(self):
        c = Criteria(keywords=['a & b', 'x+y'], titles=['CEO / founder'])
        p = parse_qs(urlsplit(c.target_url()).query)
        self.assertEqual(p['keywords'], ['a & b x+y'])
        self.assertEqual(p['title'], ['CEO / founder'])

    def test_preserve_supplied_url(self):
        url = 'https://www.linkedin.com/search/results/people/?keywords=A%20%26%20B&geoUrn=%5B%22urn%3Ali%3Ageo%3A012%22%5D'
        self.assertEqual(Criteria(search_url=url).target_url(), url)

    def test_ssrf_search_urls(self):
        values = ['http://www.linkedin.com/search/results/people/', 'https://localhost/search/results/people/',
                  'https://127.0.0.1/search/results/people/', 'https://www.linkedin.com.evil.com/search/results/people/',
                  'https://user@www.linkedin.com/search/results/people/', 'https://www.linkedin.com:443/search/results/people/',
                  'https://www.linkedin.com/search/results/people/#x', 'https://www.linkedin.com/feed/',
                  'https://www.linkedin.com/search/results/people/?redirect=https://localhost',
                  'https://www.linkedin.com/search/results/people/?network=%5B%22ALL%22%5D',
                  'https://www.linkedin.com/search/results/people/?keywords=a&keywords=b',
                  'https://www.linkedin.com/search/results/people/?geoUrn=%5B123%5D',
                  'https://www.linkedin.com/search/results/people/?keywords=%0A']
        for url in values:
            with self.subTest(url=url), self.assertRaises(ValidationError): Criteria(search_url=url)

    def test_roles_alias(self):
        self.assertEqual(Criteria(roles=['Synthetic Engineer']).titles, ['Synthetic Engineer'])
        with self.assertRaises(ValidationError): Criteria(titles=['x'], roles=['y'])

    def test_encoded_profile_dedup(self):
        from models import profile_key
        self.assertEqual(profile_key(PROFILE), profile_key(PROFILE.replace('synthetic', '%73ynthetic')))
        for suffix in ['%2Fother', '%0A', '%2E%2E']:
            with self.assertRaises(ValidationError): Lead.model_validate({**LEAD, 'linkedin_url': 'https://www.linkedin.com/in/' + suffix})

    def test_search_conflicts_and_empty(self):
        url = 'https://www.linkedin.com/search/results/people/'
        for value in [{}, {'network': ['F']}, {'search_url': url, 'source': 'web'}, {'search_url': url, 'titles': ['x']}]:
            with self.assertRaises(ValidationError): Criteria.model_validate(value)

    def test_public_evidence_urls(self):
        for url in ['https://10.0.0.1/a', 'http://example.com', 'https://[::1]/', 'file:///tmp/x',
                    'https://host.internal/x', 'https://169.254.169.254/x', 'https://127.1/x', 'https://127.0.0.1./x', 'https://example.com:90/', 'https://user:pass@example.com/']:
            with self.assertRaises(ValueError): public_url(url)

    def test_result_unknown_and_limits(self):
        for lead in [{**LEAD, 'email': 'forbidden'}, {**LEAD, 'linkedin_url': 'https://example.com/in/x'},
                     {**LEAD, 'linkedin_url': PROFILE + '?tracking=1'}, {**LEAD, 'name': 'x' * 161}]:
            with self.assertRaises(ValidationError): Lead.model_validate(lead)
        with self.assertRaises(ValidationError): result(outcome='blocked')
        with self.assertRaises(ValidationError): result(blocked_reason='login_required')
        with self.assertRaises(ValidationError): result([LEAD] * 31)

    def test_evidence_accepted(self):
        accepted = accepted_leads(result(), OBS, 1, set())
        self.assertEqual(len(accepted), 1)
        self.assertIn('company_website', accepted[0]['missing_information'])

    def test_fake_unsupported_evidence(self):
        self.assertEqual(accepted_leads(result(), [], 1, set()), [])
        fake = copy.deepcopy(LEAD)
        fake['company'] = 'Imaginary'
        self.assertEqual(accepted_leads(result([fake]), OBS, 1, set()), [])
        fake = copy.deepcopy(LEAD)
        fake['evidence'][0]['quote'] += ' fake'
        self.assertEqual(accepted_leads(result([fake]), OBS, 1, set()), [])
        self.assertEqual(accepted_leads(result(), [QUOTE], 1, set()), [])

    def test_dedup_and_limit(self):
        self.assertEqual(len(accepted_leads(result([LEAD, LEAD]), OBS, 1, set())), 1)
        self.assertEqual(accepted_leads(result(), OBS, 1, {PROFILE}), [])
        alternate = copy.deepcopy(LEAD)
        alternate['linkedin_url'] += '/'
        alternate['evidence'][0]['quote'] += '/'
        self.assertEqual(len(accepted_leads(result([LEAD, alternate]), OBS + [SOURCE + '\n' + alternate['evidence'][0]['quote']], 5, set())), 1)

    def test_missing_downgrades_identity(self):
        lead = copy.deepcopy(LEAD)
        lead['current_role'] = None
        accepted = accepted_leads(result([lead]), OBS, 1, set())
        self.assertEqual(accepted[0]['identity_status'], 'needs_review')
        self.assertIn('current_role', accepted[0]['missing_information'])

    def test_parse_terminal_text_not_deltas(self):
        events = [{'type': 'text', 'text': 'BAD'}, {'type': 'result', 'exit_code': 0, 'text': result().model_dump_json()},
                  {'type': 'tool_result', 'name': 'web_extract', 'output': OBS[0]},
                  {'type': 'tool_result', 'name': 'terminal', 'output': 'FORBIDDEN'},
                  {'type': 'tool_result', 'name': 'browser_vault_fill', 'output': 'FORBIDDEN'},
                  {'type': 'tool_result', 'name': 'web_search', 'output': 'ERROR', 'is_error': True}]
        r, o = parse_events(events)
        self.assertEqual(r.leads[0].name, LEAD['name'])
        self.assertEqual(o, OBS)

    def test_parse_rejects_final_failures(self):
        for events in [[], [{'type': 'result', 'exit_code': 1, 'text': '{}'}],
                       [{'type': 'result', 'exit_code': 0, 'text': '```json\n{}\n```'}],
                       [{'type': 'result', 'exit_code': 0, 'text': '{}'}] * 2]:
            with self.assertRaises(BackendError): parse_events(events)

    def test_restricted_command(self):
        cmd = command(criteria(budget=Budget(seconds=30, max_turns=3)))
        self.assertEqual(cmd[cmd.index('-t') + 1], 'browser,web')
        self.assertEqual(cmd[cmd.index('--query-file') + 1], '-')
        self.assertEqual(cmd[cmd.index('--max-turns') + 1], '3')
        self.assertNotIn('--yolo', cmd)
        self.assertNotIn('synthetic', cmd)


class TemporaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class StoreTests(TemporaryTests):
    def test_private_random_ids_and_queue(self):
        store = Store(self.tmp.name, max_queue=1)
        ident = store.submit(criteria())
        self.assertGreaterEqual(len(ident), 32)
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp.name).st_mode), 0o700)
        with self.assertRaises(QueueFull): store.submit(criteria())
        self.assertEqual(store.claim()['id'], ident)
        self.assertIsNone(store.claim())

    def test_restart_marks_pending_interrupted(self):
        store = Store(self.tmp.name)
        ids = [store.submit(criteria()), store.submit(criteria())]
        store.claim()
        restarted = Store(self.tmp.name)
        for ident in ids:
            self.assertEqual(restarted.get(ident)['status'], 'failed')
            self.assertEqual(restarted.get(ident)['error'], 'interrupted_restart')

    def test_only_accepted_committed(self):
        store = Store(self.tmp.name)
        ident = store.submit(criteria())
        store.finish(ident, 'failed', {'leads': [LEAD]})
        self.assertEqual(store.previous(), set())
        store.finish(ident, 'partial', {'leads': [LEAD]})
        self.assertEqual(store.previous(), {PROFILE})

    def test_cancel_delete_no_resurrection(self):
        store = Store(self.tmp.name)
        ident = store.submit(criteria())
        store.cancel(ident)
        store.finish(ident, 'succeeded', {'leads': [LEAD]})
        self.assertEqual(store.get(ident)['error'], 'cancelled')
        self.assertEqual(store.previous(), set())
        store.delete(ident)
        store.finish(ident, 'succeeded', {'leads': [LEAD]})
        self.assertIsNone(store.get(ident))
        self.assertTrue(store.cancelled(ident))

    def test_retention(self):
        store = Store(self.tmp.name)
        ident = store.submit(criteria())
        store.finish(ident, 'partial', {'leads': [LEAD]})
        with store.connect() as db:
            db.execute('UPDATE jobs SET updated=0')
            db.execute('UPDATE seen SET updated=0')
        store.cleanup()
        self.assertIsNone(store.get(ident))
        self.assertEqual(store.previous(), set())

    def test_concurrent_bounded_queue(self):
        store = Store(self.tmp.name, max_queue=2)
        submitted = []
        def submit():
            try: submitted.append(store.submit(criteria()))
            except QueueFull: pass
        threads = [threading.Thread(target=submit) for _ in range(12)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(submitted), 2)
        self.assertEqual(len(set(submitted)), 2)


class SubprocessTests(TemporaryTests):
    def setUp(self):
        super().setUp()
        fixture = Path(__file__).with_name('synthetic_backend.py').read_text()
        self.executable = str(Path(self.tmp.name) / 'synthetic-hermes')
        Path(self.executable).write_text('#!' + sys.executable + '\n' + fixture)
        os.chmod(self.executable, 0o700)

    def run_fixture(self, mode='valid', cancelled=lambda: False, budget=2, max_output=100000):
        # Short test-only budget bypasses production bounds; public API still enforces 15..600.
        c = criteria().model_copy(update={'budget': Budget.model_construct(seconds=budget, max_turns=1)})
        with patch.dict(os.environ, {'SYNTHETIC_MODE': mode}):
            return HermesBackend(self.executable, max_output).run(c, set(), cancelled)

    def test_real_subprocess_jsonl_fixture(self):
        r, obs = self.run_fixture()
        self.assertEqual(len(accepted_leads(r, obs, 1, set())), 1)

    def test_api_with_real_subprocess_synthetic_backend(self):
        backend = HermesBackend(self.executable)
        with patch.dict(os.environ, {'SYNTHETIC_MODE': 'valid'}):
            with TestClient(create_app(Path(self.tmp.name) / 'data', backend=backend)) as client:
                response = client.post('/api/v1/discoveries', json={'keywords': ['synthetic'], 'limit': 1})
                self.assertEqual(response.status_code, 202)
                ident = response.json()['id']
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    response = client.get('/api/v1/discoveries/' + ident).json()
                    if response['status'] not in {'queued', 'running'}: break
                    time.sleep(0.01)
                self.assertEqual(response['status'], 'succeeded')
                self.assertEqual(response['returned_count'], 1)
                self.assertEqual(response['result']['leads'][0]['name'], 'Synthetic Fixture Person')

    def test_timeout_terminates_group(self):
        started = time.monotonic()
        with self.assertRaises(BackendError) as caught: self.run_fixture('hang', budget=1)
        self.assertEqual(caught.exception.code, 'timed_out')
        self.assertLess(time.monotonic() - started, 3)

    def test_cancel_terminates_group(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('hang', cancelled=lambda: True)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_stdout_limit(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('flood', max_output=1024)
        self.assertEqual(caught.exception.code, 'output_limit')

    def test_stderr_limit(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('stderr_flood', max_output=1024)
        self.assertEqual(caught.exception.code, 'output_limit')

    def test_invalid_stream(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('invalid')
        self.assertEqual(caught.exception.code, 'invalid_stream')

    def test_nonzero_exit(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('exit')
        self.assertEqual(caught.exception.code, 'backend_exit')

    def test_closed_pipes_timeout(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('closed_hang', budget=1)
        self.assertEqual(caught.exception.code, 'timed_out')


class SyntheticBackend:
    """Explicit synthetic integration fixture; no browser/model calls."""
    def __init__(self, outcome='completed', failure=None, wait=0):
        self.outcome, self.failure, self.wait = outcome, failure, wait
        self.active = self.maximum = self.calls = 0
        self.lock = threading.Lock()
    def run(self, c, previous, cancelled):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls += 1
        try:
            end = time.monotonic() + self.wait
            while time.monotonic() < end:
                if cancelled(): raise BackendError('cancelled')
                time.sleep(0.01)
            if self.failure: raise self.failure
            return result(outcome=self.outcome, blocked_reason='login_required' if self.outcome == 'blocked' else None), OBS
        finally:
            with self.lock: self.active -= 1


class APITests(TemporaryTests):
    def client(self, backend=None, start=True, queue=8):
        return TestClient(create_app(self.tmp.name, origin='https://appliance.example.com:8443',
                                    backend=backend or SyntheticBackend(), start_worker=start, max_queue=queue))

    def submit(self, client, **kwargs):
        r = client.post('/api/v1/discoveries', json={'keywords': ['synthetic'], **kwargs})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()['id']

    def done(self, client, ident):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            r = client.get('/api/v1/discoveries/' + ident).json()
            if r['status'] not in {'queued', 'running'}: return r
            time.sleep(0.01)
        self.fail('synthetic job did not finish')

    def test_submit_get_success_and_dedup(self):
        with self.client() as client:
            first = self.done(client, self.submit(client, limit=1))
            self.assertEqual(first['status'], 'succeeded')
            self.assertEqual(first['returned_count'], 1)
            second = self.done(client, self.submit(client, limit=1))
            self.assertEqual(second['status'], 'partial')
            self.assertEqual(second['returned_count'], 0)
            self.assertEqual(second['missing_count'], 1)
            third = self.done(client, self.submit(client, limit=1, exclude_previous=False))
            self.assertEqual(third['status'], 'succeeded')

    def test_partial_counts_not_guaranteed(self):
        with self.client() as client:
            r = self.done(client, self.submit(client, limit=15))
            self.assertEqual(r['status'], 'partial')
            self.assertEqual((r['requested_count'], r['returned_count'], r['missing_count']), (15, 1, 14))
            self.assertFalse(r['result']['metadata']['contact_permission_established'])

    def test_blocked(self):
        with self.client(SyntheticBackend(outcome='blocked')) as client:
            r = self.done(client, self.submit(client))
            self.assertEqual(r['status'], 'blocked')
            self.assertEqual(r['result']['metadata']['blocked_reason'], 'login_required')

    def test_timeout_status(self):
        with self.client(SyntheticBackend(failure=BackendError('timed_out'))) as client:
            r = self.done(client, self.submit(client))
            self.assertEqual(r['status'], 'timed_out')
            self.assertIsNone(r['result'])

    def test_exception_no_secret_echo(self):
        with self.client(SyntheticBackend(failure=RuntimeError('SECRET_MODEL_TEXT'))) as client:
            r = self.done(client, self.submit(client))
            self.assertEqual(r['error'], 'backend_failed')
            self.assertNotIn('SECRET_MODEL_TEXT', json.dumps(r))
            self.assertIsNone(r['result'])

    def test_origin_and_unknown_fields(self):
        with self.client(start=False) as client:
            self.assertEqual(client.post('/api/v1/discoveries', json={'keywords': ['x']}, headers={'Origin': 'https://evil.com'}).status_code, 403)
            r = client.post('/api/v1/discoveries', json={'keywords': ['x'], 'prompt': 'SECRET_REJECTED_TEXT'})
            self.assertEqual(r.status_code, 422)
            self.assertNotIn('SECRET_REJECTED_TEXT', r.text)
            self.assertEqual(client.post('/api/v1/discoveries', json={'keywords': ['x']}, headers={'Origin': 'https://appliance.example.com:8443'}).status_code, 202)

    def test_queue_bound_and_no_fake_pending_result(self):
        with self.client(start=False, queue=1) as client:
            ident = self.submit(client)
            self.assertIsNone(client.get('/api/v1/discoveries/' + ident).json()['result'])
            self.assertEqual(client.post('/api/v1/discoveries', json={'keywords': ['x']}).status_code, 429)

    def test_cancel_delete_and_404(self):
        with self.client(start=False) as client:
            ident = self.submit(client)
            self.assertEqual(client.post('/api/v1/discoveries/' + ident + '/cancel').json()['error'], 'cancelled')
            self.assertEqual(client.delete('/api/v1/discoveries/' + ident).status_code, 204)
            self.assertEqual(client.get('/api/v1/discoveries/' + ident).status_code, 404)
            self.assertEqual(client.delete('/api/v1/discoveries/not-a-job').status_code, 404)

    def test_running_cancel(self):
        backend = SyntheticBackend(wait=2)
        with self.client(backend) as client:
            ident = self.submit(client)
            end = time.monotonic() + 1
            while backend.calls == 0 and time.monotonic() < end: time.sleep(0.01)
            self.assertEqual(backend.calls, 1)
            r = client.post('/api/v1/discoveries/' + ident + '/cancel').json()
            self.assertEqual(r['error'], 'cancelled')
            self.assertIsNone(r['result'])

    def test_serialized_shared_browser(self):
        backend = SyntheticBackend(wait=0.05)
        with self.client(backend) as client:
            ids = [self.submit(client, exclude_previous=False) for _ in range(3)]
            for ident in ids: self.done(client, ident)
            self.assertEqual(backend.calls, 3)
            self.assertEqual(backend.maximum, 1)

    def test_lock_prevents_second_service(self):
        with self.client(start=False):
            with self.assertRaises(BlockingIOError):
                with self.client(start=False): pass

    def test_schema_and_docs_private_paths(self):
        with self.client(start=False) as client:
            r = client.get('/api/v1/openapi.json')
            self.assertEqual(r.status_code, 200)
            self.assertIn('/api/v1/discoveries', r.json()['paths'])
            self.assertIn('Lead', r.json()['components']['schemas'])
            self.assertIn('JobResponse', r.json()['components']['schemas'])
            self.assertEqual(client.get('/api/v1/docs').status_code, 200)
            self.assertEqual(client.get('/docs').status_code, 404)
            self.assertEqual(r.headers['cache-control'], 'no-store')

    def test_body_limit(self):
        with self.client(start=False) as client:
            self.assertEqual(client.post('/api/v1/discoveries', content='x' * 16385).status_code, 413)
            def chunks():
                yield b'x' * 10000
                yield b'y' * 10000
            self.assertEqual(client.post('/api/v1/discoveries', content=chunks()).status_code, 413)

    def test_restart_integration(self):
        with self.client(start=False) as client: ident = self.submit(client)
        with self.client(start=False) as client:
            r = client.get('/api/v1/discoveries/' + ident).json()
            self.assertEqual(r['error'], 'interrupted_restart')
            self.assertIsNone(r['result'])



class PaginationTests(unittest.TestCase):
    def test_normal_search_pages_and_policy(self):
        from policy import allowed_call
        for page in ('1','2','10'):
            url='https://www.linkedin.com/search/results/people/?keywords=kanzlei&page='+page
            self.assertEqual(Criteria(search_url=url).target_url(),url)
            self.assertTrue(allowed_call('browser_navigate',{'url':url}))
        for page in ('0','11','99','-1','1.5'):
            url='https://www.linkedin.com/search/results/people/?keywords=kanzlei&page='+page
            with self.assertRaises(ValidationError):Criteria(search_url=url)
            self.assertFalse(allowed_call('browser_navigate',{'url':url}))


class OutputWrapperTests(unittest.TestCase):
    def test_one_wrapped_json_object_and_ambiguous_rejection(self):
        payload={"outcome":"completed","leads":[],"blocked_reason":None}
        event={"type":"result","exit_code":0,"text":"Here is the result:\n"+json.dumps(payload)+"\nDone."}
        parsed,observations=parse_events([event])
        self.assertEqual(parsed.outcome,'completed')
        self.assertEqual(observations,[])
        event['text']=json.dumps(payload)+json.dumps(payload)
        with self.assertRaises(BackendError):parse_events([event])


class ProfileSourceTests(unittest.TestCase):
    def test_direct_profile_address_with_verbatim_name_quote(self):
        data=copy.deepcopy(LEAD)
        data['evidence']=[{'url':PROFILE,'quote':'Synthetic Fixture Person is Synthetic Engineer at Synthetic Company.',
                         'supports':['name','current_role','company','linkedin_url','sector']}]
        observed=PROFILE+'\n'+data['evidence'][0]['quote']
        accepted=accepted_leads(result([data]),[observed],1,set())
        self.assertEqual(len(accepted),1)
        data['evidence'][0]['url']=SOURCE
        self.assertEqual(accepted_leads(result([data]),[SOURCE+'\n'+data['evidence'][0]['quote']],1,set()),[])

if __name__ == '__main__': unittest.main()
