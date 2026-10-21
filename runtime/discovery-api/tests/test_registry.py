"""Shared registry: EXPLICIT SYNTHETIC people/companies; no network."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import create_app
import outreach as o
import registry as r
from test_outreach import FakeProviders as OutreachProviders, FakeReader, request as outreach_request
from test_search import LI, SOFTWARE, backend as search_backend_fake, criteria


def wait(client, ident):
    for _ in range(500):
        job = client.get('/api/v1/discoveries/' + ident).json()
        if job['status'] not in ('queued', 'running'): return job
        time.sleep(0.01)
    raise AssertionError('job did not finish')


class KeyTests(unittest.TestCase):
    def test_company_keys(self):
        self.assertEqual(r.company_domain('https://www.Girona-Synthetic.de/team'), 'girona-synthetic.de')
        self.assertEqual(r.company_domain('https://legal.firm-synthetic.de/'), 'firm-synthetic.de')
        self.assertEqual(r.company_domain('https://www.firm-synthetic.co.uk/x'), 'firm-synthetic.co.uk')
        self.assertEqual(r.company_name_key('Codewerk Software GmbH'), r.company_name_key('CODEWERK Software'))
        self.assertEqual(r.company_name_key('Muster & Partner mbB'), 'muster partner')
        self.assertEqual(r.person_key('https://de.linkedin.com/in/Erika-Synthetic/de'), 'https://www.linkedin.com/in/erika-synthetic')


class SnapshotTests(unittest.TestCase):
    def test_known_companies_include_mere_deliveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = r.Registry(Path(tmp))
            reg.upsert({'linkedin_url': LI + 'erika-synthetic', 'company_website': 'https://delivered-synthetic.de',
                        'company_name': 'Delivered GmbH', 'status': 'delivered', 'source': 'search'})
            reg.upsert({'company_website': 'https://customer-synthetic.de', 'status': 'customer', 'source': 'crm'})
            snap = reg.snapshot()
            self.assertEqual(snap['domains'], {'customer-synthetic.de'})  # blocking only
            self.assertEqual(snap['known_domains'], {'delivered-synthetic.de', 'customer-synthetic.de'})
            self.assertIn('delivered', snap['known_names'])


class RegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'OUTREACH_BACKGROUND': '0', 'HERMES_HOME': self.tmp.name})
        env.start(); self.addCleanup(env.stop)

    def client(self, **kw):
        b, _ = search_backend_fake(**kw)
        return TestClient(create_app(Path(self.tmp.name) / 'data', search_backend=b,
                                     outreach_writer=o.OutreachWriter(OutreachProviders), outreach_reader=FakeReader([])))

    def test_external_agent_check_and_import(self):
        with self.client() as client:
            items = [{'linkedin_url': 'https://de.linkedin.com/in/agent-b-contact/de', 'company_website': 'https://agent-b-firm.de/',
                      'status': 'contacted', 'source': 'agent-b', 'note': 'Kampagne Q4'},
                     {'company_name': 'Kunde Synthetic GmbH', 'status': 'customer', 'source': 'crm'}]
            res = client.post('/api/v1/registry', json={'items': items}).json()
            self.assertEqual((res['created'], res['updated']), (2, 0))
            checks = client.post('/api/v1/registry/check', json={'items': [
                {'linkedin_url': 'https://www.linkedin.com/in/agent-b-contact'},            # same person, other URL form
                {'linkedin_url': LI + 'new-person', 'company_website': 'https://www.agent-b-firm.de/team'},  # colleague
                {'company_name': 'KUNDE Synthetic'},                                        # name match, legal form ignored
                {'linkedin_url': LI + 'unknown', 'company_name': 'Unknown Synthetic AG'}]}).json()
            self.assertEqual([(c['known'], c['blocked'], c['reasons']) for c in checks],
                             [(True, True, ['person_contacted']), (True, True, ['company_contacted']),
                              (True, True, ['company_customer']), (False, False, [])])
            self.assertEqual(checks[0]['person']['source'], 'agent-b')
            listed = client.get('/api/v1/registry', params={'source': 'agent-b'}).json()
            self.assertEqual([e['note'] for e in listed], ['Kampagne Q4'])
            self.assertEqual(client.delete('/api/v1/registry/' + listed[0]['id']).status_code, 204)
            self.assertFalse(client.post('/api/v1/registry/check', json={'items': [{'linkedin_url': LI + 'agent-b-contact'}]}).json()[0]['known'])

    def test_validation_bulk_and_sticky_opt_out(self):
        with self.client() as client:
            self.assertEqual(client.post('/api/v1/registry/check', json={'items': [{}]}).status_code, 422)
            self.assertEqual(client.post('/api/v1/registry', json={'items': [{'linkedin_url': 'https://www.linkedin.com/company/x',
                                                                           'status': 'excluded'}]}).status_code, 422)
            bulk = [{'linkedin_url': f'{LI}bulk-{i}', 'status': 'excluded', 'source': 'import'} for i in range(500)]
            self.assertEqual(client.post('/api/v1/registry', json={'items': bulk}).json()['created'], 500)  # > 16 KB body
            url = LI + 'opt-out'
            client.post('/api/v1/registry', json={'items': [{'linkedin_url': url, 'status': 'do_not_contact', 'source': 'agent-b'}]})
            again = client.post('/api/v1/registry', json={'items': [{'linkedin_url': url, 'status': 'contacted', 'source': 'agent-c'},
                                                                   {'linkedin_url': url, 'status': 'delivered'}]}).json()
            self.assertEqual(again['unchanged'], 2)  # opt-out is sticky; delivered never downgrades
            self.assertEqual(client.post('/api/v1/registry/check', json={'items': [{'linkedin_url': url}]}).json()[0]['person']['status'],
                             'do_not_contact')

    def test_search_skips_registry_and_records_deliveries(self):
        with self.client() as client:
            client.post('/api/v1/registry', json={'items': [
                {'company_website': 'https://musterfrau-legal-synthetic.de/', 'status': 'customer', 'source': 'crm'},
                {'linkedin_url': LI + 'max-beispiel-synthetic', 'status': 'contacted', 'source': 'agent-b'}]})
            body = {'source': 'search', 'keywords': ['Rechtsanwalt'], 'locations': ['München'], 'limit': 2, 'include_review': True}
            job = wait(client, client.post('/api/v1/discoveries', json=body).json()['id'])
            names = [l['name'] for l in job['result']['leads']]
            self.assertNotIn('Erika Musterfrau', names)  # company is a customer
            self.assertNotIn('Max Beispiel', names)      # person contacted by another agent
            self.assertEqual(job['result']['metadata']['excluded_by_registry'], 1)
            delivered = client.get('/api/v1/registry', params={'status': 'delivered'}).json()
            self.assertEqual([e['linkedin_url'] for e in delivered], [LI + 'otto-probe-synthetic'])
            self.assertEqual(delivered[0]['source'], 'search')

    def test_max_per_company(self):
        with self.client(**SOFTWARE) as client:
            body = {'source': 'search', 'keywords': ['Softwareentwicklung'], 'titles': ['CEO', 'Head of Sales'],
                    'locations': ['Berlin'], 'limit': 5, 'expand_terms': False}
            job = wait(client, client.post('/api/v1/discoveries', json=body).json()['id'])
            self.assertEqual([l['name'] for l in job['result']['leads']], ['Lena Code'])  # default: one per company
            self.assertEqual(job['result']['metadata']['excluded_same_company'], 1)

    def test_outreach_gates_and_registry_sync(self):
        with self.client() as client:
            first = client.post('/api/v1/outreach', json=outreach_request()).json()
            self.assertEqual(client.post('/api/v1/registry/check', json={'items': [{'linkedin_url': first['linkedin_url']}]}).json()[0]
                             ['person']['status'], 'in_outreach')
            colleague = outreach_request(linkedin_url=LI + 'colleague-synthetic')
            blocked = client.post('/api/v1/outreach', json=colleague)
            self.assertEqual((blocked.status_code, blocked.json()['detail']['error']), (409, 'company_in_outreach'))
            self.assertEqual(client.post('/api/v1/outreach', json={**colleague, 'allow_same_company': True}).status_code, 201)
            client.post(f"/api/v1/outreach/{first['id']}/sent")
            self.assertEqual(client.get('/api/v1/registry', params={'status': 'contacted'}).json()[0]['linkedin_url'], first['linkedin_url'])
            # Opt-out blocks the person for every agent; another agent's contact blocks a new outreach here.
            third = client.post('/api/v1/outreach', json=outreach_request(linkedin_url=LI + 'third-synthetic', company_website='https://other-synthetic.de/')).json()
            client.post(f"/api/v1/outreach/{third['id']}/do-not-contact")
            client.delete(f"/api/v1/outreach/{third['id']}")
            self.assertEqual(client.post('/api/v1/outreach', json=outreach_request(linkedin_url=LI + 'third-synthetic',
                             company_website='https://other-synthetic.de/')).json()['detail']['error'], 'person_do_not_contact')
            client.post('/api/v1/registry', json={'items': [{'linkedin_url': LI + 'elsewhere', 'status': 'in_outreach', 'source': 'agent-b'}]})
            self.assertEqual(client.post('/api/v1/outreach', json=outreach_request(linkedin_url=LI + 'elsewhere', company_website='https://x-synthetic.de/'))
                             .json()['detail']['error'], 'person_in_registry')
            # Deleting a mistaken contact frees the person again.
            fourth = client.post('/api/v1/outreach', json=outreach_request(linkedin_url=LI + 'fourth-synthetic', company_website='https://y-synthetic.de/')).json()
            client.delete(f"/api/v1/outreach/{fourth['id']}")
            self.assertFalse(client.post('/api/v1/registry/check', json={'items': [{'linkedin_url': LI + 'fourth-synthetic'}]}).json()[0]['known'])

    def test_backfill_existing_data(self):
        directory = Path(self.tmp.name) / 'data'
        with self.client() as client:
            body = {'source': 'search', 'keywords': ['Rechtsanwalt'], 'locations': ['München'], 'limit': 1}
            wait(client, client.post('/api/v1/discoveries', json=body).json()['id'])
        (directory / 'registry.sqlite3').unlink()  # simulate an installation from before the registry
        with self.client() as client:
            delivered = client.get('/api/v1/registry', params={'status': 'delivered'}).json()
            self.assertEqual(len(delivered), 1)
            self.assertTrue(delivered[0]['company_domain'])  # company recovered from the stored job result
        with self.client() as client:  # restart: backfill must not touch status or timestamps
            again = client.get('/api/v1/registry', params={'status': 'delivered'}).json()
            self.assertEqual((again[0]['updated_at'], again[0]['expires_at']), (delivered[0]['updated_at'], delivered[0]['expires_at']))


if __name__ == '__main__':
    unittest.main()
