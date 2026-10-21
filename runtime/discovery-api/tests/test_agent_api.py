"""Agent primitives: change feed, action inbox, campaigns, review edits, webhooks. Synthetic, no network."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import create_app

PROFILE = 'https://www.linkedin.com/in/erika-musterfrau-synthetic'
SITE = 'https://musterfrau-legal-synthetic.de/'
LEAD = {
    'name': 'Erika Musterfrau', 'current_role': 'Partnerin', 'company': 'Musterfrau Legal',
    'company_website': SITE, 'linkedin_url': PROFILE, 'identity_status': 'verified',
    'evidence': [{'url': SITE, 'quote': 'Erika Musterfrau, Partnerin', 'supports': ['name']}],
}


class FakeWriter:
    def note(self, request):
        return {'name': 'Erika Musterfrau', 'headline': 'Partnerin', 'connection_note': 'Hallo, gern vernetzen.'}, 0.01

    def research(self, request, record):
        return {'findings': [], 'sources': [], 'message': 'Danke fürs Vernetzen!', 'error': None}, 0.02


class FakeReader:
    def read(self, url): return ('Erika Musterfrau · 1.\nNachricht', PROFILE)
    def read_inbox(self): return [], 'https://www.linkedin.com/messaging/'


class FakeConnector:
    def __init__(self): self.calls = []
    def send_request(self, url, note=None):
        self.calls.append((url, note))
        return {'state': 'sent'}


def outreach_body(**kw):
    return {'linkedin_url': PROFILE, 'company_website': SITE, 'topic': 'KI-Automationen', **kw}


def campaign_body(**kw):
    return {'name': 'Testkampagne', 'topic': 'KI-Automationen', 'max_contacts': 2,
            'criteria': {'source': 'search', 'keywords': ['Kanzlei'], 'locations': ['München'], 'limit': 3,
                         'budget': {'seconds': 300, 'max_turns': 10, 'max_cost_usd': 0.5}}, **kw}


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hours = patch('outreach.ACTIVE_HOURS', (0, 24))
        self.hours.start()
        self.addCleanup(self.hours.stop)

    def client(self, connector=None):
        app = create_app(Path(self.tmp.name) / 'data', start_worker=False, outreach_writer=FakeWriter(),
                         outreach_reader=FakeReader(), outreach_connector=connector or FakeConnector())
        return TestClient(app)

    def create_contact(self, client, **kw):
        response = client.post('/api/v1/outreach', json=outreach_body(**kw))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # -- events
    def test_event_feed_uses_cursor_and_control_events(self):
        with self.client() as client:
            service = client.app.state.outreach
            rec = self.create_contact(client)
            page = client.get('/api/v1/events', params={'since': 0}).json()
            self.assertEqual([(e['type'], e['ref_id'], e['status']) for e in page['events']],
                             [('outreach', rec['id'], 'awaiting_connection')])
            self.assertGreater(page['next_cursor'], 0)
            self.assertNotIn('name', json.dumps(page))  # no personal content in the feed
            self.assertEqual(client.get('/api/v1/events', params={'since': page['next_cursor']}).json()['events'], [])
            service.pause(time.time(), 1)
            paused = client.get('/api/v1/events', params={'since': page['next_cursor']}).json()
            self.assertEqual([e['type'] for e in paused['events']], ['outreach.paused'])
            time.sleep(0.01)  # distinct timestamp for the resume event
            service.resume()
            resumed = client.get('/api/v1/events', params={'since': paused['next_cursor']}).json()
            self.assertEqual([e['type'] for e in resumed['events']], ['outreach.resumed'])

    # -- actions
    def test_action_inbox_prioritizes_review_and_failures(self):
        with self.client() as client:
            service = client.app.state.outreach
            rec = self.create_contact(client)
            service.mark_accepted(rec['id'])
            service.research_one()
            kinds = [a['type'] for a in client.get('/api/v1/actions').json()]
            self.assertIn('review_message', kinds)
            service.pause(time.time(), 1)
            top = client.get('/api/v1/actions').json()[0]
            self.assertEqual((top['type'], top['actor']), ('fix_session', 'human'))
            service.resume()
            stored = service.store.get(rec['id'])[1]
            stored['connect'] = {**stored['connect'], 'state': 'failed', 'error': 'connect_no_button'}
            stored['status'] = 'awaiting_connection'
            service.store.save(stored)
            actions = client.get('/api/v1/actions', params={'kind': 'send_connection'}).json()
            self.assertEqual([a['ref_id'] for a in actions], [rec['id']])
            self.assertEqual(actions[0]['suggested_call']['path'], '/api/v1/outreach/' + rec['id'] + '/connect')

    # -- campaigns
    def test_campaign_runs_discovery_fanout_and_reaches_review(self):
        with self.client() as client:
            campaigns, store, outreach = client.app.state.campaigns, client.app.state.store, client.app.state.outreach
            created = client.post('/api/v1/campaigns', json=campaign_body())
            self.assertEqual(created.status_code, 201, created.text)
            campaign = created.json()
            self.assertEqual(campaign['status'], 'queued')
            campaigns.tick()
            campaign = client.get('/api/v1/campaigns/' + campaign['id']).json()
            self.assertEqual((campaign['status'], bool(campaign['job_id'])), ('discovering', True))
            store.finish(campaign['job_id'], 'succeeded', {'outcome': 'completed', 'leads': [LEAD]})
            campaigns.tick()
            campaign = client.get('/api/v1/campaigns/' + campaign['id']).json()
            self.assertEqual((campaign['status'], len(campaign['contact_ids'])), ('outreaching', 1))
            contact_id = campaign['contact_ids'][0]
            self.assertEqual(client.get('/api/v1/outreach', params={'campaign_id': campaign['id']}).json()[0]['id'],
                             contact_id)
            outreach.mark_accepted(contact_id)
            outreach.research_one()
            campaigns.tick()
            campaign = client.get('/api/v1/campaigns/' + campaign['id']).json()
            self.assertEqual((campaign['status'], campaign['counts']), ('awaiting_review', {'ready': 1}))
            actions = client.get('/api/v1/actions', params={'kind': 'review_campaign'}).json()
            self.assertEqual([a['ref_id'] for a in actions], [campaign['id']])
            self.assertEqual(client.post('/api/v1/campaigns/' + campaign['id'] + '/pause').json()['status'], 'paused')
            self.assertEqual(client.post('/api/v1/campaigns/' + campaign['id'] + '/resume').json()['status'],
                             'awaiting_review')
            self.assertEqual(client.delete('/api/v1/campaigns/' + campaign['id']).status_code, 204)
            self.assertEqual(client.get('/api/v1/campaigns/' + campaign['id']).status_code, 404)

    def test_campaign_skips_registry_blocked_people(self):
        with self.client() as client:
            campaigns, store = client.app.state.campaigns, client.app.state.store
            client.post('/api/v1/registry', json={'items': [{'linkedin_url': PROFILE, 'status': 'do_not_contact'}]})
            campaign = client.post('/api/v1/campaigns', json=campaign_body()).json()
            campaigns.tick()
            campaign = client.get('/api/v1/campaigns/' + campaign['id']).json()
            store.finish(campaign['job_id'], 'succeeded', {'outcome': 'completed', 'leads': [LEAD]})
            campaigns.tick()
            campaign = client.get('/api/v1/campaigns/' + campaign['id']).json()
            self.assertEqual((campaign['contact_ids'], campaign['status']), ([], 'done'))

    # -- review edits
    def test_patch_edits_note_and_message_without_sending(self):
        with self.client() as client:
            service, connector = client.app.state.outreach, client.app.state.outreach.connector
            rec = self.create_contact(client)
            patched = client.patch('/api/v1/outreach/' + rec['id'], json={'connection_note': 'Kurz und persönlich.'})
            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertEqual(patched.json()['connection_note'], 'Kurz und persönlich.')
            self.assertEqual(connector.calls, [])
            self.assertEqual(client.patch('/api/v1/outreach/' + rec['id'], json={'message': 'Zu früh'}).status_code, 409)
            service.mark_accepted(rec['id'])
            service.research_one()
            patched = client.patch('/api/v1/outreach/' + rec['id'], json={'message': 'Neuer Entwurf.'})
            self.assertEqual(patched.json()['message'], 'Neuer Entwurf.')
            stored = service.store.get(rec['id'])[1]
            stored['connect'] = {**stored['connect'], 'state': 'sent'}
            service.store.save(stored)
            self.assertEqual(client.patch('/api/v1/outreach/' + rec['id'],
                                          json={'connection_note': 'Zu spät.'}).status_code, 409)

    # -- webhooks
    def test_webhook_crud_and_signed_delivery(self):
        with self.client() as client:
            dispatcher = client.app.state.webhooks
            sent = []
            dispatcher.sender = lambda url, body, secret: sent.append((url, json.loads(body), secret))
            created = client.post('/api/v1/webhooks', json={'url': 'https://example.com/hook', 'events': ['outreach']})
            self.assertEqual(created.status_code, 201, created.text)
            hook = created.json()
            self.assertTrue(hook['secret'])
            listed = client.get('/api/v1/webhooks').json()[0]
            self.assertNotIn('secret', listed)
            rec = self.create_contact(client)
            self.assertGreater(dispatcher.dispatch_once(), 0)
            self.assertEqual(sent[0][0], 'https://example.com/hook')
            self.assertEqual([(e['type'], e['ref_id']) for e in sent[0][1]['events']], [('outreach', rec['id'])])
            self.assertTrue(sent[0][2])
            dispatcher.dispatch_once()
            self.assertEqual(len(sent), 1)  # cursor advanced, no duplicate delivery
            self.assertEqual(client.delete('/api/v1/webhooks/' + hook['id']).status_code, 204)
            self.assertEqual(client.delete('/api/v1/webhooks/' + hook['id']).status_code, 404)

    def test_webhook_failure_backs_off_and_never_leaks_secret(self):
        with self.client() as client:
            dispatcher = client.app.state.webhooks
            def boom(url, body, secret): raise OSError('down')
            dispatcher.sender = boom
            hook = client.post('/api/v1/webhooks', json={'url': 'https://example.com/hook'}).json()
            self.create_contact(client)
            dispatcher.dispatch_once()
            stored = client.app.state.webhooks.store.get(hook['id'])
            self.assertEqual(stored['failures'], 1)
            self.assertGreater(stored['next_at'], time.time())
            self.assertNotIn(hook['secret'], json.dumps(client.get('/api/v1/webhooks').json()))

    def test_webhook_endpoint_validation(self):
        with self.client() as client:
            for url in ('http://example.com/hook', 'https://127.0.0.1/hook', 'https://localhost/hook'):
                with self.subTest(url=url):
                    self.assertEqual(client.post('/api/v1/webhooks', json={'url': url}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
