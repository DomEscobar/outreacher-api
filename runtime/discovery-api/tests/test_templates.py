"""Message templates with {{placeholders}} and follow-ups: EXPLICIT SYNTHETIC data, no network, no model."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import create_app
import outreach as o

PROFILE = 'https://www.linkedin.com/in/erika-musterfrau-synthetic'
SITE = 'https://musterfrau-steuer-synthetic.de/'
PAGES = {SITE: '# Musterfrau Steuerberatung\nUnser Bürohund Bruno begrüßt jeden Mandanten persönlich.'}
TEMPLATE = ('Hallo {{name}},\n\n{{icebreaker (1-2 Zeiler) Firmenbezug / lustiges}}\n\n'
            'In fast jedem Gespräch mit Steuerberatern höre ich: Das Wissen hängt an zwei, drei Köpfen.\n'
            'Viele Grüße, Dominic')
FOLLOWUPS = [{'after_days': 10, 'message': 'Hallo {{vorname}},\nich hole meine Nachricht kurz wieder nach oben.'},
             {'after_days': 20, 'message': 'Hallo {{name}},\nversprochen, das ist meine letzte Nachricht.'}]


class SlotProviders:
    """Fake treg/OpenRouter: one website page; the model fills field 1 and reports one grounded finding."""
    PROMPTS = []

    def __init__(self, meter): self.meter = meter
    def serp(self, query, limit=10): return []
    def extract(self, url):
        self.meter.charge(150, 'provider_calls')
        return PAGES.get(url, '')
    def llm(self, user):
        self.meter.charge(1000, 'llm_calls')
        SlotProviders.PROMPTS.append(user)
        return {'findings': [{'need': 'Persönliche Kanzlei', 'where': 'Empfang', 'quote': 'Unser Bürohund Bruno begrüßt jeden Mandanten',
                              'url': SITE}],
                'fields': {'1': 'Bruno, Ihr Bürohund,\nist der eigentliche Star Ihrer Website – Respekt! [[2]]'},
                'message': 'should be ignored for fixed templates'}


def contact(**kw):
    return {'linkedin_url': PROFILE, 'company_website': SITE, 'company_name': 'Musterfrau Steuerberatung', 'name': 'Dr. Erika Musterfrau',
            'headline': 'Steuerberaterin', 'topic': 'KI-Automatisierung', 'connection_note': False, 'auto_connect': False, **kw}


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        SlotProviders.PROMPTS = []
        hours = patch.object(o, 'ACTIVE_HOURS', (0, 24))
        hours.start(); self.addCleanup(hours.stop)

    def client(self):
        return TestClient(create_app(Path(self.tmp.name) / 'data', start_worker=False, outreach_writer=o.OutreachWriter(SlotProviders)))

    def test_fill_template_keeps_text_and_cleans_gaps(self):
        variables = {'name': 'Erika Musterfrau', 'vorname': 'Erika', 'firma': 'Musterfrau Steuerberatung'}
        self.assertEqual(o.fill_template('Hallo {{ Name }}, Grüße an {{firma}}!', variables, {}), 'Hallo Erika Musterfrau, Grüße an Musterfrau Steuerberatung!')
        self.assertEqual(o.fill_template('Hallo {{name}},\n\n{{Eisbrecher}}\n\nText', {}, {}), 'Hallo,\n\nText')  # nothing dangling
        self.assertEqual(o.model_fields(['{{name}} {{Eisbrecher}}', '{{Eisbrecher}} {{PS-Zeile}} {{vorname}}']), ['Eisbrecher', 'PS-Zeile'])
        self.assertEqual(o.name_parts('Dr. Erika Musterfrau'), ('Erika', 'Musterfrau'))

    def test_crud_default_and_search_assignment(self):
        with self.client() as client:
            first = client.post('/api/v1/templates', json={'name': 'Steuerberater', 'message': TEMPLATE, 'followups': FOLLOWUPS})
            self.assertEqual(first.status_code, 201, first.text)
            first = first.json()
            self.assertTrue(first['is_default'])  # the first template is the default
            second = client.post('/api/v1/templates', json={'name': 'Anwälte', 'message': 'Hallo {{name}}', 'is_default': True}).json()
            listed = client.get('/api/v1/templates').json()
            self.assertEqual([(t['name'], t['is_default']) for t in listed], [('Anwälte', True), ('Steuerberater', False)])
            renamed = client.put('/api/v1/templates/' + first['id'], json={'name': 'Steuerberater DE', 'message': TEMPLATE}).json()
            self.assertEqual((renamed['name'], renamed['created_at']), ('Steuerberater DE', first['created_at']))
            self.assertEqual(client.post('/api/v1/templates', json={'name': 'x', 'message': ''}).status_code, 422)
            # a search gets a template; its leads carry it
            body = {'source': 'search', 'keywords': ['Steuerberater'], 'limit': 1}
            self.assertEqual(client.post('/api/v1/discoveries', params={'template_id': 'nope'}, json=body).status_code, 404)
            job = client.post('/api/v1/discoveries', params={'template_id': first['id']}, json=body).json()
            client.app.state.store.finish(job['id'], 'succeeded', {'leads': [{
                'name': 'Erika Musterfrau', 'linkedin_url': PROFILE, 'company': 'Musterfrau Steuerberatung', 'identity_status': 'verified',
                'evidence': [{'url': SITE, 'quote': 'Erika Musterfrau', 'supports': ['name']}]}], 'returned_count': 1, 'missing_count': 0})
            self.assertEqual(client.get('/api/v1/leads').json()[0]['template_id'], first['id'])
            self.assertEqual(client.get('/api/v1/discoveries').json()[0]['template_id'], first['id'])
            # deleting the default hands it over
            self.assertEqual(client.delete('/api/v1/templates/' + second['id']).status_code, 204)
            self.assertTrue(client.get('/api/v1/templates/' + first['id']).json()['is_default'])
            self.assertEqual(client.delete('/api/v1/templates/' + second['id']).status_code, 404)

    def test_template_message_is_kept_word_for_word_and_only_fields_are_written(self):
        with self.client() as client:
            service = client.app.state.outreach
            tpl = client.post('/api/v1/templates', json={'name': 'Steuerberater', 'message': TEMPLATE, 'followups': FOLLOWUPS}).json()
            rec = client.post('/api/v1/outreach', json=contact(template_id=tpl['id'], topic='wird ersetzt'))
            self.assertEqual(rec.status_code, 201, rec.text)
            rec = rec.json()
            self.assertEqual((rec['template_id'], rec['topic']), (tpl['id'], 'Steuerberater'))  # topic from the template
            service.mark_accepted(rec['id'])
            self.assertTrue(service.research_one())
            done = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual(done['status'], 'ready')
            self.assertEqual(done['message'],
                             'Hallo Dr. Erika Musterfrau,\n\nBruno, Ihr Bürohund, ist der eigentliche Star Ihrer Website – Respekt!\n\n'
                             'In fast jedem Gespräch mit Steuerberatern höre ich: Das Wissen hängt an zwei, drei Köpfen.\nViele Grüße, Dominic')
            self.assertEqual([(f['after_days'], f['message'], f['sent_at']) for f in done['followups']],
                             [(10, 'Hallo Erika,\nich hole meine Nachricht kurz wieder nach oben.', None),
                              (20, 'Hallo Dr. Erika Musterfrau,\nversprochen, das ist meine letzte Nachricht.', None)])
            self.assertEqual(len(done['findings']), 1)
            prompt = SlotProviders.PROMPTS[-1]
            self.assertIn('[[1]]', prompt)
            self.assertIn('1: icebreaker (1-2 Zeiler) Firmenbezug / lustiges', prompt)
            self.assertNotIn('Step 2: write the message', prompt)  # the model never rewrites the fixed text
            # editing a long fixed text is not cut at message_max_chars
            long_text = 'x' * 1500
            self.assertEqual(len(client.patch('/api/v1/outreach/' + rec['id'], json={'message': long_text}).json()['message']), 1500)

    def test_variables_only_template_needs_no_paid_call(self):
        with self.client() as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=contact(template='Hallo {{vorname}}, kurze Frage an {{firma}}.')).json()
            service.mark_accepted(rec['id'])
            service.research_one()
            done = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual((done['message'], done['cost_usd'], done['error']),
                             ('Hallo Erika, kurze Frage an Musterfrau Steuerberatung.', 0.0, None))
            self.assertEqual(SlotProviders.PROMPTS, [])

    def test_free_guidance_still_drafts_freely(self):
        with self.client() as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=contact(template='Kurz, Sie-Form, Frage nach 15-Minuten-Call.')).json()
            service.mark_accepted(rec['id'])
            service.research_one()
            self.assertEqual(client.get('/api/v1/outreach/' + rec['id']).json()['message'], 'should be ignored for fixed templates')
            self.assertIn('Step 2: write the message', SlotProviders.PROMPTS[-1])

    def test_campaign_with_template_hands_contacts_to_the_human(self):
        with self.client() as client:
            campaigns, store = client.app.state.campaigns, client.app.state.store
            tpl = client.post('/api/v1/templates', json={'name': 'Steuerberater', 'topic': 'KI-Automatisierung', 'message': TEMPLATE,
                                                         'followups': FOLLOWUPS}).json()
            criteria = {'source': 'search', 'keywords': ['Steuerberater'], 'limit': 1}
            self.assertEqual(client.post('/api/v1/campaigns', json={'criteria': criteria}).status_code, 422)  # topic or template
            self.assertEqual(client.post('/api/v1/campaigns', json={'criteria': criteria, 'template_id': 'nope'}).status_code, 404)
            campaign = client.post('/api/v1/campaigns', json={'criteria': criteria, 'template_id': tpl['id']})
            self.assertEqual(campaign.status_code, 201, campaign.text)
            campaign = campaign.json()
            self.assertEqual((campaign['request']['topic'], len(campaign['request']['followups'])), ('KI-Automatisierung', 2))
            campaigns.tick()
            job_id = client.get('/api/v1/campaigns/' + campaign['id']).json()['job_id']
            self.assertEqual(client.app.state.templates.job_map()[job_id], tpl['id'])  # its leads carry the template
            store.finish(job_id, 'succeeded', {'leads': [{
                'name': 'Erika Musterfrau', 'current_role': 'Steuerberaterin', 'linkedin_url': PROFILE, 'company': 'Musterfrau Steuerberatung',
                'company_website': SITE, 'identity_status': 'verified', 'evidence': [{'url': SITE, 'quote': 'Erika Musterfrau', 'supports': ['name']}]}],
                'returned_count': 1, 'missing_count': 0})
            campaigns.tick()
            contact = client.get('/api/v1/outreach', params={'campaign_id': campaign['id']}).json()[0]
            self.assertEqual((contact['template_id'], contact['connect']['state'], contact['connect']['opened_at']), (tpl['id'], 'manual', None))
            todo = [a['ref_id'] for a in client.get('/api/v1/actions', params={'kind': 'connect_manually'}).json()]
            self.assertEqual(todo, [contact['id']])  # the human connects; the agent only waits
            stored_request = client.app.state.outreach.store.get(contact['id'])[0]
            self.assertEqual((stored_request.template, len(stored_request.followups)), (TEMPLATE, 2))

    def test_followups_are_due_in_order_and_stop_at_a_reply(self):
        with self.client() as client:
            service = client.app.state.outreach
            tpl = client.post('/api/v1/templates', json={'name': 'Steuerberater', 'message': 'Hallo {{name}}', 'followups': FOLLOWUPS}).json()
            rec = client.post('/api/v1/outreach', json=contact(template_id=tpl['id'])).json()
            service.mark_accepted(rec['id']); service.research_one()
            sent = client.post('/api/v1/outreach/' + rec['id'] + '/sent').json()
            self.assertIsNone(o.due_followup(sent, time.time()))  # not before day 10
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'sent_at': time.time() - 11 * 86400})
            stored = service.store.get(rec['id'])[1]
            self.assertEqual(o.due_followup(stored, time.time()), 0)
            actions = client.get('/api/v1/actions', params={'kind': 'send_followup'}).json()
            self.assertEqual([(a['ref_id'], a['actor']) for a in actions], [(rec['id'], 'human')])
            self.assertEqual(client.post(f"/api/v1/outreach/{rec['id']}/followups/1/sent").status_code, 409)  # first one first
            edited = client.patch('/api/v1/outreach/' + rec['id'], json={'followup_index': 0, 'followup_message': 'Kurz nachgehakt.'})
            self.assertEqual(edited.json()['followups'][0]['message'], 'Kurz nachgehakt.')
            self.assertEqual(client.patch('/api/v1/outreach/' + rec['id'], json={'followup_index': 3, 'followup_message': 'x'}).status_code, 409)
            first = client.post(f"/api/v1/outreach/{rec['id']}/followups/0/sent").json()
            self.assertIsNotNone(first['followups'][0]['sent_at'])
            self.assertIsNone(o.due_followup(first, time.time()))  # the second one waits for day 20
            self.assertIsNotNone(o.due_followup(first, time.time() + 10 * 86400))
            replied = client.post('/api/v1/outreach/' + rec['id'] + '/replied').json()
            self.assertIsNone(o.due_followup(replied, time.time() + 30 * 86400))  # an answer ends the follow-ups
            self.assertEqual(client.post(f"/api/v1/outreach/{rec['id']}/followups/1/sent").status_code, 409)


if __name__ == '__main__':
    unittest.main()
