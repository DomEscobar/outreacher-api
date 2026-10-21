"""Outreach workflow: EXPLICIT SYNTHETIC people/firms; no network, no browser, no model."""
import json
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


class FakeInboxReader(FakeReader):
    """Synthetic conversation-list rows (visible text lines per conversation), as the browser reader returns them."""
    def __init__(self, inboxes, href=o.INBOX_URL):
        super().__init__([])
        self.inboxes, self.href, self.inbox_reads = list(inboxes), href, 0

    def read_inbox(self):
        self.inbox_reads += 1
        return self.inboxes.pop(0), self.href


class BlockedReader:
    """The browser hit a challenge/rate-limit page instead of content."""
    def __init__(self): self.reads = 0
    def read(self, url):
        self.reads += 1
        raise o.BrowserBlocked()
    def read_inbox(self):
        self.reads += 1
        raise o.BrowserBlocked()


class FakeConnector:
    """Returns a canned send result and remembers the profiles it was asked to invite."""
    def __init__(self, state='sent'): self.state, self.calls = state, []
    def send_request(self, url, note=None):
        self.calls.append((url, note))
        if self.state == 'blocked': raise o.BrowserBlocked()
        return {'state': self.state}


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
        self.hours = patch.object(o, 'ACTIVE_HOURS', (0, 24))  # the working-hours window is tested separately
        self.hours.start()
        self.addCleanup(self.hours.stop)

    def client(self, reader, connector=None):
        app = create_app(Path(self.tmp.name) / 'data', start_worker=False, outreach_writer=self.writer,
                         outreach_reader=reader, outreach_connector=connector)
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

    def test_manual_check_connection_reads_without_sending(self):
        # Two states on demand: not_connected, then pending. No request is ever sent by the check.
        reader = FakeReader([('Erika Musterfrau · 2.\nVernetzen', PROFILE), ('Erika Musterfrau · 2.\nAusstehend', PROFILE)])
        with self.client(reader) as client:
            rec = client.post('/api/v1/outreach', json=request()).json()
            self.assertEqual((rec['status'], rec['connection']['state']), ('awaiting_connection', 'unknown'))  # nothing proven yet
            checked = client.post('/api/v1/outreach/' + rec['id'] + '/check-connection').json()
            self.assertEqual((checked['status'], checked['connection']['state'], checked['connection']['checks']),
                             ('awaiting_connection', 'not_connected', 1))
            checked = client.post('/api/v1/outreach/' + rec['id'] + '/check-connection').json()
            self.assertEqual((checked['connection']['state'], checked['connection']['checks']), ('pending', 2))
            self.assertEqual(reader.urls, [PROFILE, PROFILE])  # only the profile was opened

    def test_manual_check_detects_connected_and_moves_to_research(self):
        with self.client(FakeReader([('Erika Musterfrau · 1.\nNachricht', PROFILE)])) as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=request()).json()
            checked = service.check_connection(rec['id'])
            self.assertEqual((checked['status'], checked['connection']['state']), ('connected', 'connected'))

    def test_manual_check_ignores_cap_but_is_a_noop_after_sent(self):
        reader = FakeReader([('Erika Musterfrau · 1.\nNachricht', PROFILE)])
        with self.client(reader) as client, patch.object(o, 'DAILY_CHECK_CAP', 0):  # background check would be capped
            service = client.app.state.outreach
            sent = self.sent_contact(client)
            before = service.store.get(sent['id'])[1]['connection']['checks']
            self.assertEqual(service.check_connection(sent['id'])['status'], 'sent')  # connection question settled
            self.assertEqual(service.store.get(sent['id'])[1]['connection']['checks'], before)  # no read
            self.assertEqual(reader.urls, [])

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

    def sent_contact(self, client):
        rec = client.post('/api/v1/outreach', json=request()).json()  # name "Erika Musterfrau" from the search index
        return client.post('/api/v1/outreach/' + rec['id'] + '/sent').json()

    def test_inbox_reply_marks_contact_and_keeps_no_inbox_text(self):
        reader = FakeInboxReader([[['Erika Musterfrau', '14:32', 'Gern, passt Ihnen Dienstag?'],
                                   ['Max Unbeteiligt-Synthetic', 'Mo', 'Sie: Hallo Herr Unbeteiligt']]])
        with self.client(reader) as client:
            service = client.app.state.outreach
            sent = self.sent_contact(client)
            self.assertIsNotNone(sent['sent_at'])
            self.assertTrue(service.reply_check())
            rec = client.get('/api/v1/outreach/' + sent['id']).json()
            self.assertEqual((rec['status'], rec['reply']['source'], rec['reply']['previous_status']), ('replied', 'inbox', 'sent'))
            self.assertEqual(reader.inbox_reads, 1)
            self.assertFalse(service.reply_check())  # next look only after REPLY_CHECK_HOURS, and nobody waits now
            status = client.get('/api/v1/status').json()['outreach']
            self.assertEqual(status['counts'], {'replied': 1})
            self.assertIsNotNone(status['last_inbox_check'])
        raw = (Path(self.tmp.name) / 'data' / 'outreach.sqlite3').read_bytes()
        for text in (b'Dienstag', b'Unbeteiligt'):  # previews and other people's names are never stored
            with self.subTest(text=text): self.assertNotIn(text, raw)

    def test_inbox_last_message_ours_or_ambiguous_is_no_reply(self):
        for rows in ([['Erika Musterfrau', '14:32', 'Sie: Hallo Frau Musterfrau, danke fürs Vernetzen']],
                     [['Erika Musterfrau', '14:32', 'Gern'], ['Erika Musterfrau', 'Mo', 'Hallo']],  # two people, same name
                     []):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as tmp:
                app = create_app(Path(tmp) / 'data', start_worker=False, outreach_writer=self.writer,
                                 outreach_reader=FakeInboxReader([rows]))
                with TestClient(app) as client:
                    sent = self.sent_contact(client)
                    self.assertTrue(client.app.state.outreach.reply_check())
                    self.assertEqual(client.get('/api/v1/outreach/' + sent['id']).json()['status'], 'sent')

    def test_reply_check_only_when_waiting_and_pauses_on_login_wall(self):
        reader = FakeInboxReader([[], []], href='https://www.linkedin.com/checkpoint/challenge')
        with self.client(reader) as client:
            service = client.app.state.outreach
            client.post('/api/v1/outreach', json=request())  # awaiting connection: no answer awaited yet
            self.assertFalse(service.reply_check())
            self.assertEqual(reader.inbox_reads, 0)  # no inbox view without a reason
            service.mark_sent(service.store.list()[0]['id'])
            self.assertTrue(service.reply_check())
            self.assertGreater(service.paused_until, time.time())  # a human must log in
            service.store.set_meta('next_inbox_check', 0)
            self.assertFalse(service.reply_check())  # paused
            service.paused_until = 0
            with patch.object(o, 'REPLY_CHECK', False):
                self.assertFalse(service.reply_check())  # OUTREACH_REPLY_CHECK=0
            self.assertEqual(reader.inbox_reads, 1)

    def test_answer_to_the_connection_note_counts_too(self):
        reader = FakeInboxReader([[['Dr. Erika Musterfrau', 'Gestern', 'Danke fürs Vernetzen, worum geht es?']]])
        with self.client(reader) as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'status': 'ready'})  # accepted, our message not yet sent
            self.assertTrue(service.reply_check())
            self.assertEqual(client.get('/api/v1/outreach/' + rec['id']).json()['reply']['previous_status'], 'ready')

    def test_manual_reply_and_undo(self):
        with self.client(FakeReader([])) as client:
            sent = self.sent_contact(client)
            replied = client.post('/api/v1/outreach/' + sent['id'] + '/replied').json()
            self.assertEqual((replied['status'], replied['reply']['source']), ('replied', 'manual'))
            undone = client.post('/api/v1/outreach/' + sent['id'] + '/no-reply').json()
            self.assertEqual((undone['status'], undone['reply']), ('sent', None))
            self.assertEqual(client.post('/api/v1/outreach/unknown/replied').status_code, 404)

    def test_inbox_calibration_reports_counts_only(self):
        with self.client(FakeReader([])) as client:
            self.sent_contact(client)
        reader = FakeInboxReader([[['Erika Musterfrau', '14:32', 'Gern'], ['Max Muster', 'Mo', 'Sie: Hallo']]])
        result = o.inbox_check(Path(self.tmp.name) / 'data', reader)
        self.assertEqual(result, {'login_wall': False, 'rows_read': 2, 'conversations': 2, 'last_message_ours': 1,
                                  'last_message_theirs': 1, 'contacts_waiting': 1, 'contacts_with_reply': 1})
        self.assertNotIn('Erika', json.dumps(result))

    def test_inbox_rows_and_name_matching(self):
        rows = [['Erika Musterfrau', '14:32', 'Gern'], ['Max Muster', 'Mo', 'Sie: Hallo'], ['Gesponsert', 'Angebot'],
                ['Anna Beispiel', 'Di', 'You: Thanks'], 'kein Eintrag', []]
        self.assertEqual(o.inbox_conversations(rows), [('Erika Musterfrau', True), ('Max Muster', False), ('Anna Beispiel', False)])
        contacts = [{'id': 'a', 'name': 'Dr. Erika Musterfrau'}, {'id': 'b', 'name': None}, {'id': 'c', 'name': 'Max Muster'},
                    {'id': 'd', 'name': 'Paul Doppelt'}, {'id': 'e', 'name': 'Paul Doppelt'}]
        talks = [('Erika Musterfrau', True), ('Max Muster', False), ('Paul Doppelt', True)]
        self.assertEqual(o.replied_contacts(contacts, talks), ['a'])  # Max: last message ours; Paul: two contacts

    def test_blocked_page_pauses_for_hours_and_survives_restart(self):
        reader = BlockedReader()
        with self.client(reader) as client:
            service = client.app.state.outreach
            service.spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'connection': {**stored['connection'], 'next_check_at': 0}})
            self.assertTrue(service.check_one())
            self.assertEqual(reader.reads, 1)
            self.assertEqual(service.store.get(rec['id'])[1]['connection']['state'], 'login_required')
            self.assertGreaterEqual(service.paused_until, time.time() + o.BLOCK_PAUSE_HOURS * 3600 - 5)
            self.assertAlmostEqual(service.store.meta('paused_until'), service.paused_until)
            restarted = o.OutreachService(service.store, start=False)  # same DB after a container restart
            self.assertEqual(restarted.paused_until, service.paused_until)
            self.assertFalse(restarted.check_one())  # still paused, no second read
            self.assertEqual(reader.reads, 1)
            self.assertEqual(restarted.check_connection(rec['id'])['connection']['state'], 'login_required')
            self.assertEqual(reader.reads, 1)  # a manual check does not hammer a paused session either

    def test_resume_clears_the_persisted_pause(self):
        reader = FakeReader([('', 'https://www.linkedin.com/authwall')])
        with self.client(reader) as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'connection': {**stored['connection'], 'next_check_at': 0}})
            self.assertTrue(service.check_one())  # login wall -> pause
            self.assertGreater(service.paused_until, time.time())
            self.assertEqual(client.post('/api/v1/outreach/resume').json(), {'paused_until': None})
            self.assertEqual(service.paused_until, 0)
            self.assertEqual(service.store.meta('paused_until'), 0)

    def test_outside_working_hours_defers_profile_and_inbox_reads(self):
        reader = FakeInboxReader([[]])
        with self.client(reader) as client:
            service = client.app.state.outreach
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            service.store.save({**stored, 'connection': {**stored['connection'], 'next_check_at': 0}})
            with patch.object(o, 'within_active_hours', lambda now: False), patch.object(o, 'next_active_at', lambda now: now + 3600):
                self.assertFalse(service.check_one())
                self.assertEqual(reader.urls, [])
            service.mark_sent(rec['id'])
            with patch.object(o, 'within_active_hours', lambda now: False), patch.object(o, 'next_active_at', lambda now: now + 3600):
                self.assertFalse(service.reply_check())
            self.assertEqual(reader.inbox_reads, 0)
            self.assertGreaterEqual(service.store.meta('next_inbox_check'), time.time() + 3599)

    def test_empty_inbox_backs_off_and_daily_cap_stops_it(self):
        reader = FakeInboxReader([[], []])
        with self.client(reader) as client:
            service = client.app.state.outreach
            self.sent_contact(client)
            self.assertTrue(service.reply_check())  # empty list: treated as a failed look
            self.assertEqual(reader.inbox_reads, 1)
            self.assertEqual(service.inbox_failures(), 1)
            self.assertGreaterEqual(service.store.meta('next_inbox_check'),
                                    time.time() + o.REPLY_CHECK_HOURS * 3600 * 2 * 0.9)
            service.store.set_meta('next_inbox_check', 0)
            with patch.object(o, 'INBOX_DAILY_CAP', 1):
                self.assertFalse(service.reply_check())  # hard daily cap, restart-safe
            self.assertEqual(reader.inbox_reads, 1)

    def test_auto_connect_sends_note_and_waits_for_acceptance(self):
        connector = FakeConnector('sent')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            self.assertEqual((rec['connect']['state'], rec['connect']['attempts'], rec['connect']['sent_at']),
                             ('not_sent', 0, None))
            self.assertTrue(service.connect_one())
            self.assertEqual(connector.calls, [(rec['linkedin_url'], rec['connection_note'])])
            sent = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual((sent['status'], sent['connect']['state'], sent['connection']['state']),
                             ('awaiting_connection', 'sent', 'pending'))
            self.assertIsNotNone(sent['connect']['sent_at'])
            self.assertFalse(service.connect_one())  # nothing left to send
            self.assertEqual(client.get('/api/v1/outreach/' + rec['id']).json()['connect']['attempts'], 1)

    def test_auto_connect_already_connected_skips_the_request(self):
        connector = FakeConnector('connected')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            self.assertTrue(service.connect_one())
            after = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual((after['status'], after['connection']['state'], after['connect']['state']),
                             ('connected', 'connected', 'sent'))
            self.assertTrue(service.research_one())  # acceptance handling continues as before

    def test_auto_connect_blocked_hard_stops_and_pauses(self):
        connector = FakeConnector('blocked')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            self.assertTrue(service.connect_one())
            stored = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual((stored['connect']['state'], stored['connect']['error']), ('not_sent', 'connect_blocked'))
            self.assertGreaterEqual(service.paused_until, time.time() + o.BLOCK_PAUSE_HOURS * 3600 - 5)
            service.store.set_meta('paused_until', service.paused_until)
            self.assertFalse(service.connect_one())  # paused: no second attempt
            self.assertEqual(len(connector.calls), 1)
            self.assertEqual(client.post('/api/v1/outreach/resume').json(), {'paused_until': None})
            self.assertFalse(service.connect_one())  # next attempt is scheduled hours out, not immediately
            self.assertEqual(len(connector.calls), 1)

    def test_legacy_contacts_are_not_invited_retroactively(self):
        connector = FakeConnector('sent')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            stored = service.store.get(rec['id'])[1]
            stored.pop('connect', None)  # a record created before auto-connect existed
            service.store.save(stored)
            self.assertFalse(service.connect_one())
            self.assertEqual(connector.calls, [])

    def test_auto_connect_weekly_cap(self):
        connector = FakeConnector('sent')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            client.post('/api/v1/outreach', json=request())
            client.post('/api/v1/outreach', json=request(linkedin_url=PROFILE + '-2', allow_same_company=True))
            with patch.object(o, 'CONNECT_WEEKLY_CAP', 1), patch.object(o, 'within_active_hours', lambda now: True):
                self.assertTrue(service.connect_one())
                self.assertFalse(service.connect_one())  # weekly allowance spent
            self.assertEqual(len(connector.calls), 1)

    def test_auto_connect_invitation_limit_pauses_for_days(self):
        connector = FakeConnector('limit')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            rec = client.post('/api/v1/outreach', json=request()).json()
            self.assertTrue(service.connect_one())
            stored = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual((stored['connect']['state'], stored['connect']['error']), ('not_sent', 'connect_limit'))
            self.assertGreaterEqual(service.paused_until, time.time() + o.LIMIT_PAUSE_HOURS * 3600 - 5)
            self.assertFalse(service.connect_one())

    def test_auto_connect_respects_hours_and_daily_cap(self):
        connector = FakeConnector('sent')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            client.post('/api/v1/outreach', json=request())
            with patch.object(o, 'within_active_hours', lambda now: False):
                self.assertFalse(service.connect_one())
            with patch.object(o, 'CONNECT_DAILY_CAP', 0), patch.object(o, 'within_active_hours', lambda now: True):
                self.assertFalse(service.connect_one())
            self.assertEqual(connector.calls, [])
            self.assertEqual(client.get('/api/v1/outreach').json()[0]['connect']['state'], 'not_sent')

    def test_auto_connect_disabled_or_failed_can_be_retried(self):
        connector = FakeConnector('no_button')
        with self.client(FakeReader([]), connector) as client:
            service = client.app.state.outreach
            service.connect_spacing = (0, 0)
            manual = client.post('/api/v1/outreach', json=request(auto_connect=False)).json()
            self.assertEqual(manual['connect']['state'], 'manual')
            self.assertFalse(service.connect_one())
            other = request(linkedin_url=PROFILE + '-2', allow_same_company=True)
            with patch.object(o, 'CONNECT_MAX_ATTEMPTS', 1):
                rec = client.post('/api/v1/outreach', json=other).json()
                self.assertTrue(service.connect_one())
            failed = client.get('/api/v1/outreach/' + rec['id']).json()
            self.assertEqual((failed['connect']['state'], failed['connect']['error']), ('failed', 'connect_no_button'))
            self.assertFalse(service.connect_one())  # failed is terminal until a human retries
            retried = client.post('/api/v1/outreach/' + rec['id'] + '/connect').json()
            self.assertEqual(retried['connect']['state'], 'not_sent')
            self.assertEqual(connector.calls[-1][0], rec['linkedin_url'])

    def test_reader_only_reads(self):
        source = Path(o.__file__).read_text(encoding='utf-8')
        cdp_methods = set(re.findall(r"send\('([A-Za-z]+\.[A-Za-z]+)'", source))
        self.assertEqual(cdp_methods, {'Target.createTarget', 'Target.attachToTarget', 'Page.navigate',
                                       'Runtime.evaluate', 'Target.closeTarget'})  # no Input.*, no clicks
        self.assertNotRegex(source, r'\.click\(|Input\.dispatch|insertText')

    def test_only_connect_module_mutates_the_page(self):
        import connect
        source = Path(connect.__file__).read_text(encoding='utf-8')
        self.assertRegex(source, r'\.click\(\)')  # the connection request is the one allowed mutation
        self.assertNotRegex(source, r'Input\.dispatch|insertText')
        self.assertIn('CONNECT_LABELS', source)
        self.assertIn('SEND_LABELS', source)


if __name__ == '__main__':
    unittest.main()
