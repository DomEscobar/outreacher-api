"""Overview endpoints and the /ui page: EXPLICIT SYNTHETIC data only, no network."""
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import UI_FILE, create_app
import outreach as o
import search_backend as sb
from test_outreach import FakeProviders, FakeReader, request as outreach_request
from test_search import backend as search_backend_fake


def wait(client, ident):
    for _ in range(500):
        job = client.get('/api/v1/discoveries/' + ident).json()
        if job['status'] not in ('queued', 'running'): return job
        time.sleep(0.01)
    raise AssertionError('job did not finish')


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'OUTREACH_BACKGROUND': '0', 'TREG_TOKEN': '', 'OPENROUTER_API_KEY': '',
                                      'HERMES_HOME': self.tmp.name})
        env.start(); self.addCleanup(env.stop)

    def client(self):
        b, _ = search_backend_fake()
        return TestClient(create_app(Path(self.tmp.name) / 'data', search_backend=b,
                                     outreach_writer=o.OutreachWriter(FakeProviders), outreach_reader=FakeReader([])))

    def test_jobs_leads_status_and_sent(self):
        with self.client() as client:
            body = {'source': 'search', 'keywords': ['Rechtsanwalt'], 'locations': ['München'], 'limit': 2, 'include_review': True}
            job = wait(client, client.post('/api/v1/discoveries', json=body).json()['id'])
            jobs = client.get('/api/v1/discoveries').json()
            self.assertEqual(len(jobs), 1)
            self.assertEqual((jobs[0]['id'], jobs[0]['status'], jobs[0]['locations']), (job['id'], 'succeeded', ['München']))
            self.assertEqual(jobs[0]['verification_counts'], {'verified': 1, 'plausible': 1, 'linkedin': 0, 'review': 1})
            self.assertEqual(client.get('/api/v1/discoveries', params={'status': 'failed'}).json(), [])

            leads = client.get('/api/v1/leads').json()
            self.assertEqual([l['verification'] for l in leads], ['verified', 'plausible', 'review'])
            self.assertTrue(all(l['job_id'] == job['id'] and l['outreach_id'] is None for l in leads))
            self.assertEqual([l['name'] for l in client.get('/api/v1/leads', params={'verification': 'plausible'}).json()], ['Max Beispiel'])
            self.assertEqual([l['name'] for l in client.get('/api/v1/leads', params={'q': 'musterfrau legal'}).json()], ['Erika Musterfrau'])
            self.assertEqual(client.get('/api/v1/leads', params={'verification': 'bogus'}).status_code, 422)

            # Outreach for a found lead shows up on the lead row; "sent" closes the loop.
            rec = client.post('/api/v1/outreach', json=outreach_request(linkedin_url=leads[0]['linkedin_url'])).json()
            row = client.get('/api/v1/leads', params={'q': 'Erika'}).json()[0]
            self.assertEqual((row['outreach_id'], row['outreach_status']), (rec['id'], 'awaiting_connection'))
            self.assertEqual((row['outreach_connection'], row['outreach_message']), ('unknown', None))  # nothing seen, no draft
            self.assertEqual(client.post(f"/api/v1/outreach/{rec['id']}/sent").json()['status'], 'sent')
            self.assertEqual(client.get('/api/v1/outreach', params={'status': 'sent'}).json()[0]['id'], rec['id'])

            status = client.get('/api/v1/status').json()
            self.assertEqual((status['jobs_total'], status['leads_total']), (1, 3))
            self.assertEqual(status['outreach']['counts'], {'sent': 1})
            self.assertFalse(status['search']['configured'])  # no TREG_TOKEN in this environment
            self.assertEqual(status['search']['model'], sb.DEFAULT_MODEL)

    def test_ui_page_is_self_contained_and_safe(self):
        with self.client() as client:
            page = client.get('/ui/')
            self.assertEqual(page.status_code, 200)
            self.assertIn('Hermes Leads', page.text)
            self.assertIn("default-src 'self'", page.headers['content-security-policy'])
            self.assertEqual(client.get('/ui', follow_redirects=False).status_code, 308)
        html = UI_FILE.read_text(encoding='utf-8')
        self.assertNotRegex(html, r'<script[^>]+src=|<link[^>]+stylesheet|https?://(?!www\.w3\.org|www\.linkedin\.com/in/|firma\.de)')
        self.assertNotRegex(html, r'innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(')  # web data only via textContent
        # Every API path the page calls exists.
        paths = set(re.findall(r"'/api/v1/([a-z]+)", html))
        self.assertEqual(paths, {'status', 'discoveries', 'leads', 'outreach', 'campaigns', 'actions', 'registry', 'templates'})


if __name__ == '__main__':
    unittest.main()
