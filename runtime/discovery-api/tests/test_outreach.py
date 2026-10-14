"""Outreach workflow: EXPLICIT SYNTHETIC people/firms; no network, no browser, no model."""
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import create_app
import outreach as o
import search_backend as sb

PROFILE = 'https://www.linkedin.com/in/erika-musterfrau-synthetic'
SITE = 'https://musterfrau-legal-synthetic.de/'
PAGES = {
    SITE: '# Musterfrau Legal\n[Leistungen](/leistungen) [Karriere](/karriere) [Facebook](https://facebook.com/x)\nWillkommen.',
    SITE + 'leistungen': 'Arbeitsrecht für Unternehmen\nWir prüfen jährlich über 2.000 Arbeitsverträge für Mandanten.',
    SITE + 'karriere': 'Wir suchen: Rechtsanwaltsfachangestellte (m/w/d) für die Fristenverwaltung und Aktenanlage.',
}


class FakeProviders:
    PROMPTS = []  # every LLM prompt, to check that topic/template reach the writer

    def __init__(self, meter):
        self.meter = meter

    def serp(self, query, limit=10):
        self.meter.charge(500, 'provider_calls')
        return [{'url': 'https://de.linkedin.com/in/erika-musterfrau-synthetic/de',
                 'title': 'Erika Musterfrau – Partnerin bei Musterfrau Legal', 'description': 'Partnerin · Arbeitsrecht · München'}]

    def extract(self, url):
        self.meter.charge(150, 'provider_calls')
        return PAGES.get(url, '')

    def llm(self, user):
        self.meter.charge(1000, 'llm_calls')
        FakeProviders.PROMPTS.append(user)
        if 'connection request note' in user:
            return {'note': 'Hallo Frau Musterfrau, Ihr Fokus auf Arbeitsrecht hat mich angesprochen – gern vernetze ich mich.' + ' x' * 200}
        return {'findings': [
                    {'need': 'Viele Vertragsprüfungen', 'where': 'Arbeitsrecht / Vertragsprüfung',
                     'quote': 'Wir prüfen jährlich über 2.000 Arbeitsverträge', 'url': SITE + 'leistungen'},
                    {'need': 'Engpass Fristen', 'where': 'Sekretariat', 'quote': 'Fristenverwaltung und Aktenanlage',
                     'url': SITE + 'karriere'},
                    {'need': 'erfunden', 'where': 'x', 'quote': 'Wir haben 500 Mitarbeiter in Berlin', 'url': SITE}],
                'message': 'Hallo Frau Musterfrau, danke fürs Vernetzen! ' + 'Text ' * 400}


class FakeReader:
    def __init__(self, answers): self.answers, self.urls = list(answers), []
    def read(self, url):
        self.urls.append(url)
        return self.answers.pop(0)


def request(**kw):
    base = {'linkedin_url': 'https://de.linkedin.com/in/erika-musterfrau-synthetic/de', 'company_website': SITE,
            'topic': 'KI-Automationen', 'template': 'Kurz, Sie-Form, Bezug auf einen Fund, Frage nach 15-Minuten-Call.',
            'note_max_chars': 120, 'message_max_chars': 600}
    return {**base, **kw}


class ClassifyTests(unittest.TestCase):
    def test_connection_states_de_en(self):
        cases = [('Erika Musterfrau · 1.\nPartnerin\nNachricht', 'connected'), ('Erika Musterfrau · 1st\nMessage', 'connected'),
                 ('Erika Musterfrau · 2.\nAusstehend', 'pending'), ('Erika · 3rd+\nPending', 'pending'),
                 ('Erika Musterfrau · 2.\nVernetzen\nNachricht', 'not_connected'), ('Erika · 2nd\nConnect', 'not_connected'),
                 ('Lade…', 'unknown')]
        for text, state in cases:
            with self.subTest(text=text): self.assertEqual(o.classify_connection(text, PROFILE), state)
        self.assertEqual(o.classify_connection('Erika · 1.', 'https://www.linkedin.com/authwall?x'), 'login_required')
        self.assertEqual(o.classify_connection('', 'https://www.linkedin.com/checkpoint/challenge'), 'login_required')

    def test_request_contract(self):
        r = o.OutreachRequest.model_validate(request())
        self.assertEqual(r.linkedin_url, PROFILE)  # canonicalised
        for bad in ({'company_website': None}, {'linkedin_url': 'https://www.linkedin.com/company/x'},
                    {'topic': ''}, {'company_website': 'http://insecure.de'}, {'note_max_chars': 400}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError): o.OutreachRequest.model_validate(request(**bad))
        self.assertIsNotNone(o.OutreachRequest.model_validate(request(company_website=None, company_name='Musterfrau Legal')))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.writer = o.OutreachWriter(FakeProviders)
        FakeProviders.PROMPTS = []

    def client(self, reader):
        app = create_app(Path(self.tmp.name) / 'data', start_worker=False, outreach_writer=self.writer, outreach_reader=reader)
        return TestClient(app)

    def test_full_flow_connect_check_research_ready(self):
        reader = FakeReader([('Erika Musterfrau · 2.\nAusstehend', PROFILE), ('Erika Musterfrau · 1.\nNachricht', PROFILE)])
        with self.client(reader) as client:
            service = client.app.state.outreach
            service.spacing = (0, 0)
            created = client.post('/api/v1/outreach', json=request())
            self.assertEqual(created.status_code, 201)
            rec = created.json()
            self.assertEqual((rec['status'], rec['name']), ('awaiting_connection', 'Erika Musterfrau'))
            self.assertLessEqual(len(rec['connection_note']), 120)
            self.assertEqual(client.post('/api/v1/outreach', json=request()).status_code, 409)  # one contact per profile
            for expected in ('pending', 'connected'):
                service.store.save({**service.store.get(rec['id'])[1],
                                    'connection': {**service.store.get(rec['id'])[1]['connection'], 'next_check_at': 0}})
                self.assertTrue(service.check_one())
                self.assertEqual(client.get('/api/v1/outreach/' + rec['id']).json()['connection']['state'], expected)
            self.assertEqual(reader.urls, [PROFILE, PROFILE])
            self.assertTrue(service.research_one())
            ready = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual(ready['status'], 'ready')
            self.assertEqual([f['where'] for f in ready['findings']], ['Arbeitsrecht / Vertragsprüfung', 'Sekretariat'])  # invented one dropped
            self.assertTrue(all(f['url'].startswith(SITE) for f in ready['findings']))
            self.assertLessEqual(len(ready['message']), 600)
            self.assertEqual((ready['message_is_draft'], ready['sent_by_system']), (True, False))
            self.assertEqual(sorted(ready['sources']), sorted(PAGES))  # only same-host pages, no facebook link
            self.assertGreater(ready['cost_usd'], 0)
            # Topic and template reached the writer; the stored template is not echoed in the record.
            research_prompt = FakeProviders.PROMPTS[-1]
            self.assertIn('KI-Automationen', research_prompt)
            self.assertIn('15-Minuten-Call', research_prompt)
            self.assertNotIn('template', ready)
            listed = client.get('/api/v1/outreach', params={'status': 'ready'}).json()
            self.assertEqual([r['id'] for r in listed], [rec['id']])
            self.assertEqual(client.delete('/api/v1/outreach/' + rec['id']).status_code, 204)
            self.assertEqual(client.get('/api/v1/outreach/' + rec['id']).status_code, 404)

    def test_manual_accept_override_and_cross_origin(self):
        with self.client(FakeReader([])) as client:
            rec = client.post('/api/v1/outreach', json=request()).json()
            self.assertEqual(client.post('/api/v1/outreach/' + rec['id'] + '/accepted').json()['status'], 'connected')
            blocked = client.post('/api/v1/outreach', json=request(linkedin_url=PROFILE + '-2'), headers={'Origin': 'https://evil.invalid'})
            self.assertEqual(blocked.status_code, 403)

    def test_login_wall_pauses_and_daily_cap(self):
        reader = FakeReader([('', 'https://www.linkedin.com/authwall')])
        with self.client(reader) as client:
            service = client.app.state.outreach
            service.spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'connection': {**stored['connection'], 'next_check_at': 0}})
            self.assertTrue(service.check_one())
            self.assertGreater(service.paused_until, time.time())
            self.assertFalse(service.check_one())  # paused until a human logs in
            service.paused_until = 0
            with patch.object(o, 'DAILY_CHECK_CAP', 1):
                self.assertFalse(service.check_one())  # one check already today

    def test_expiry_after_configured_days(self):
        with self.client(FakeReader([])) as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'created_at': time.time() - 40 * 86400,
                                'connection': {**stored['connection'], 'next_check_at': 0}})
            self.assertTrue(service.check_one())
            self.assertEqual(service.store.get(rec['id'])[1]['status'], 'expired')

    def test_common_paths_when_extract_has_no_links(self):
        texts = {SITE: 'Startseite ohne Links', SITE + 'leistungen': 'Arbeitsrecht', SITE + 'team': 'Startseite ohne Links'}
        fetched = []
        p = type('P', (), {'extract': lambda self, url: fetched.append(url) or texts.get(url, '')})()
        r = o.OutreachRequest.model_validate(request())
        website, pages = o.OutreachWriter(FakeProviders).site_pages(p, r, None)
        self.assertEqual([u for u, _ in pages], [SITE, SITE + 'leistungen'])  # duplicate text of /team skipped
        self.assertEqual(fetched[0], SITE)

    def test_reader_only_reads(self):
        source = Path(o.__file__).read_text(encoding='utf-8')
        cdp_methods = set(re.findall(r"send\('([A-Za-z]+\.[A-Za-z]+)'", source))
        self.assertEqual(cdp_methods, {'Target.createTarget', 'Target.attachToTarget', 'Page.navigate',
                                       'Runtime.evaluate', 'Target.closeTarget'})  # no Input.*, no clicks
        self.assertNotRegex(source, r'\.click\(|Input\.dispatch|insertText')


if __name__ == '__main__':
    unittest.main()
