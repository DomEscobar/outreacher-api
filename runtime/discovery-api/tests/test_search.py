"""source=search: EXPLICIT SYNTHETIC fixtures only; no network, no model, no real people."""
import json
import os
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import create_app
from models import Criteria, Lead, evaluate_lead
from rendering import render_search
import search_backend as sb

LI = 'https://www.linkedin.com/in/'
# Synthetic firms on synthetic domains (never fetched; served by FakeProviders).
PROFILES = [
    {'url': LI + 'erika-musterfrau-synthetic', 'title': 'Erika Musterfrau – Rechtsanwältin & Partnerin bei Musterfrau Legal',
     'description': 'Rechtsanwältin und Partnerin bei Musterfrau Legal · München'},
    {'url': LI + 'max-beispiel-synthetic', 'title': 'Max Beispiel – Rechtsanwalt, Inhaber bei Kanzlei Beispiel',
     'description': 'Rechtsanwalt, Inhaber bei Kanzlei Beispiel in München'},
    {'url': LI + 'otto-probe-synthetic', 'title': 'Otto Probe – Rechtsanwalt / Partner bei Probe Rechtsanwälte',
     'description': 'Rechtsanwalt / Partner bei Probe Rechtsanwälte, München'},
    {'url': LI + 'anna-associate-synthetic', 'title': 'Anna Assoc – Associate bei Musterfrau Legal',
     'description': 'Associate bei Musterfrau Legal, München'},
]
PAGES = {
    'https://musterfrau-legal-synthetic.de/team/erika-musterfrau': '# Erika Musterfrau\n\nRechtsanwältin | Partnerin\n\nArbeitsrecht',
    'https://musterfrau-legal-synthetic.de/impressum': 'Impressum\nMusterfrau Legal Rechtsanwälte PartG mbB, München',
    'https://kanzlei-beispiel-synthetic.de/': 'Kanzlei Beispiel\n\nRechtsanwalt Max Beispiel berät Sie in München.',
    'https://kanzlei-beispiel-synthetic.de/impressum': 'Impressum\nKanzlei Beispiel, Rechtsanwalt Max Beispiel, München',
    'https://probe-rechtsanwaelte-synthetic.de/team': 'Team\n\nOtto Probe\nAssociate\n\nRechtsanwälte',
    'https://probe-rechtsanwaelte-synthetic.de/impressum': 'Impressum Probe Rechtsanwälte GmbH',
    'https://neu-kanzlei-synthetic.de/impressum': ('Impressum\nNeu Kanzlei Rechtsanwaltsgesellschaft mbH, München\n'
                                                 'Geschäftsführer: Rechtsanwalt Paul Neumann\n' + 'Angaben gemäß § 5 DDG. ' * 5),
}
FIRM_SEARCH = {
    'Erika Musterfrau': ['https://gelbeseiten.de/x', 'https://musterfrau-legal-synthetic.de/team/erika-musterfrau'],
    'Max Beispiel': ['https://kanzlei-beispiel-synthetic.de/'],
    'Otto Probe': ['https://probe-rechtsanwaelte-synthetic.de/team'],
}
CONFIRM = {
    'Erika Musterfrau': {'person_on_site': True, 'role_on_site': 'Partnerin', 'quote_role': 'Rechtsanwältin | Partnerin',
                         'firm_name': 'Musterfrau Legal Rechtsanwälte PartG mbB',
                         'quote_firm': 'Musterfrau Legal Rechtsanwälte PartG mbB, München', 'firm_in_sector': True,
                         'quote_sector': 'Rechtsanwälte', 'listed_in_other_role': False},
    'Max Beispiel': {'person_on_site': True, 'role_on_site': 'Rechtsanwalt', 'quote_role': 'Rechtsanwalt Max Beispiel',
                     'firm_name': 'Kanzlei Beispiel', 'quote_firm': 'Kanzlei Beispiel, Rechtsanwalt', 'firm_in_sector': True,
                     'quote_sector': 'Rechtsanwalt', 'listed_in_other_role': False},
    'Otto Probe': {'person_on_site': True, 'role_on_site': 'Associate', 'quote_role': 'Associate',
                   'firm_name': 'Probe Rechtsanwälte GmbH', 'quote_firm': 'Probe Rechtsanwälte GmbH', 'firm_in_sector': True,
                   'quote_sector': 'Rechtsanwälte', 'listed_in_other_role': True},
    'Paul Neumann': {'person_on_site': True, 'role_on_site': 'Geschäftsführer', 'quote_role': 'Geschäftsführer: Rechtsanwalt Paul Neumann',
                     'firm_name': 'Neu Kanzlei Rechtsanwaltsgesellschaft mbH',
                     'quote_firm': 'Neu Kanzlei Rechtsanwaltsgesellschaft mbH, München', 'firm_in_sector': True,
                     'quote_sector': 'Rechtsanwaltsgesellschaft', 'listed_in_other_role': False},
}


class FakeProviders:
    """Replays synthetic SERP/page/LLM answers; charges a fixed synthetic cost per call."""
    def __init__(self, meter, profiles=PROFILES, firm_first=False, cost_micro=100, pages=PAGES, firm_search=FIRM_SEARCH,
                 confirm=CONFIRM, sector_quote='Rechtsanw', expansion=None):
        self.meter, self.profiles, self.firm_first, self.cost = meter, profiles, firm_first, cost_micro
        self.pages, self.firm_search, self.confirm, self.sector_quote = pages, firm_search, confirm, sector_quote
        self.serp_queries, self.fetched, self.expansion, self.prompts = [], [], expansion or {}, []

    def charge(self, kind):
        self.meter.check()
        self.meter.charge(self.cost, kind)

    def serp(self, query, limit=10):
        self.charge('provider_calls')
        self.serp_queries.append(query)
        row = lambda u: {'url': u, 'title': '', 'description': ''}
        if query.startswith('site:linkedin.com/in "Paul Neumann"'):
            return [{'url': LI + 'paul-neumann-synthetic', 'title': 'Paul Neumann – Geschäftsführer – Neu Kanzlei',
                     'description': 'Rechtsanwalt bei Neu Kanzlei, München'}]
        if query.startswith('site:linkedin.com/in'):
            return [] if self.firm_first else [dict(p) for p in self.profiles]
        if 'Impressum' in query and self.firm_first:
            return [row('https://neu-kanzlei-synthetic.de/impressum'), row('https://gelbeseiten.de/y')]
        for name, urls in self.firm_search.items():
            if f'"{name}"' in query: return [row(u) for u in urls]
        return []

    def extract(self, url):
        self.charge('provider_calls')
        self.fetched.append(url)
        return self.pages.get(url, '')

    def llm(self, user):
        self.charge('llm_calls')
        self.prompts.append(user)
        if 'Suggest LinkedIn search terms' in user:
            return self.expansion
        if 'For EACH item' in user:
            items = []
            for i, line in re.findall(r'^\[(\d+)\] (.*)$', user, re.M):
                name = line.split(' – ')[0]
                dm = 'Associate' not in line
                role = re.search(r'– (.*?) bei', line).group(1)
                company = line.split(' bei ')[-1]
                items.append({'index': int(i), 'name': name, 'current_role': role, 'company': company,
                              'is_decision_maker': dm, 'in_sector': True, 'in_location': True, 'quote_name': name,
                              'quote_role': role, 'quote_company': company, 'quote_sector': self.sector_quote})
            return {'items': items}
        if 'From this company imprint' in user:
            return {'in_sector': True, 'quote_sector': 'Rechtsanwaltsgesellschaft', 'company': 'Neu Kanzlei Rechtsanwaltsgesellschaft mbH',
                    'quote_company': 'Neu Kanzlei Rechtsanwaltsgesellschaft mbH',
                    'people': [{'name': 'Paul Neumann', 'current_role': 'Geschäftsführer',
                                'quote_name_role': 'Geschäftsführer: Rechtsanwalt Paul Neumann'}]}
        person = re.search(r'Person: (.*?)\. Claimed', user).group(1)
        return self.confirm[person]


SOFTWARE_PROFILES = [
    {'url': LI + 'lena-code-synthetic', 'title': 'Lena Code – CEO & Co-Founder bei Codewerk Software GmbH',
     'description': 'CEO & Co-Founder bei Codewerk Software GmbH · Softwareentwicklung · Berlin'},
    {'url': LI + 'tom-berger-synthetic', 'title': 'Tom Berger – Head of Sales bei Codewerk Software GmbH',
     'description': 'Head of Sales bei Codewerk Software GmbH · Softwareentwicklung · Berlin'},
    {'url': LI + 'ida-dev-synthetic', 'title': 'Ida Dev – Senior Developer bei Codewerk Software GmbH',
     'description': 'Senior Developer bei Codewerk Software GmbH · Softwareentwicklung · Berlin'},
]
SOFTWARE_TEAM = 'https://codewerk-synthetic.de/team'
SOFTWARE_PAGES = {SOFTWARE_TEAM: 'Team\n\nLena Code\nCEO & Co-Founder\n\nTom Berger\nHead of Sales\n\nIda Dev\nSenior Developer',
                  'https://codewerk-synthetic.de/impressum': 'Impressum\nCodewerk Software GmbH, Berlin. Geschäftsführerin: Lena Code. Softwareentwicklung.'}
SOFTWARE_CONFIRM = {name: {'person_on_site': True, 'role_on_site': role, 'quote_role': role, 'listed_in_other_role': False,
                           'firm_name': 'Codewerk Software GmbH', 'quote_firm': 'Codewerk Software GmbH, Berlin',
                           'firm_in_sector': True, 'quote_sector': 'Softwareentwicklung'}
                    for name, role in (('Lena Code', 'CEO & Co-Founder'), ('Tom Berger', 'Head of Sales'), ('Ida Dev', 'Senior Developer'))}
SOFTWARE = {'profiles': SOFTWARE_PROFILES, 'pages': SOFTWARE_PAGES, 'confirm': SOFTWARE_CONFIRM, 'sector_quote': 'Softwareentwicklung',
            'firm_search': {name: [SOFTWARE_TEAM] for name in SOFTWARE_CONFIRM}}


def criteria(**kw):
    base = {'source': 'search', 'keywords': ['Rechtsanwalt'], 'titles': ['Partner', 'Inhaber', 'Geschäftsführer'],
            'locations': ['München'], 'limit': 3, 'budget': {'seconds': 60, 'max_cost_usd': 1}}
    return Criteria.model_validate({**base, **kw})


def backend(**kw):
    made = {}
    def factory(meter):
        made['p'] = FakeProviders(meter, **kw)
        return made['p']
    return sb.SearchBackend(factory), made


class CriteriaTests(unittest.TestCase):
    def test_search_contract(self):
        self.assertEqual(criteria(limit=100).limit, 100)
        for bad in ({'keywords': []}, {'geo_urns': ['123']}, {'network': ['F']}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError): criteria(**bad)
        for bad in ({'limit': 31}, {'locations': ['München']}, {'include_review': True}, {'min_verification': 'linkedin'},
                    {'expand_terms': False}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Criteria.model_validate({'keywords': ['x'], **bad})
        with self.assertRaises(ValidationError): criteria(budget={'max_cost_usd': 0})
        with self.assertRaises(ValidationError): Criteria.model_validate({'keywords': ['x'], 'max_per_company': 2})
        # Stored jobs are re-validated with EVERY field set: defaults must round-trip for every source.
        for source in ('linkedin', 'web', 'search'):
            stored = Criteria.model_validate({'keywords': ['x'], 'source': source}).model_dump_json()
            self.assertEqual(Criteria.model_validate_json(stored).source, source)

    def test_agent_leads_cannot_self_certify_a_tier(self):
        from test_discovery import LEAD, OBS
        lead = Lead.model_validate({**LEAD, 'verification': 'verified', 'verification_reason': 'claimed by model',
                                    'evidence': [{**LEAD['evidence'][0], 'source': 'firm_website'}]})
        data, verdict, _ = evaluate_lead(lead, OBS)
        self.assertEqual(verdict, 'accepted')
        self.assertIsNone(data['verification'])
        self.assertIsNone(data['evidence'][0]['source'])


class PipelineTests(unittest.TestCase):
    def test_tiers_from_firm_websites(self):
        b, made = backend()
        qualified, review, stats = b.run(criteria(include_review=True), set(), lambda: False)
        tiers = {l['name']: l['verification'] for l in qualified + review}
        self.assertEqual(tiers, {'Erika Musterfrau': 'verified', 'Max Beispiel': 'plausible', 'Otto Probe': 'review'})
        self.assertEqual(stats['verification_counts'], {'verified': 1, 'plausible': 1, 'linkedin': 0, 'review': 1})
        erika = qualified[0]
        self.assertEqual(erika['company'], 'Musterfrau Legal Rechtsanwälte PartG mbB')  # legal name from the imprint
        self.assertEqual(erika['company_website'], 'https://musterfrau-legal-synthetic.de')
        self.assertEqual(erika['identity_status'], 'verified')
        # Every quote is literal text of a fetched source (snippet or page), >= 8 chars for the schema.
        sources = '\n'.join(list(PAGES.values()) + [f"{p['title']}\n{p['description']}" for p in PROFILES])
        for lead in qualified + review:
            for e in lead['evidence']:
                self.assertIn(sb.norm(e['quote']), sb.norm(sources))
                self.assertGreaterEqual(len(e['quote']), 8)
        # The associate is dropped at classification; directories are never fetched.
        self.assertNotIn('Anna Assoc', tiers)
        self.assertFalse(any('gelbeseiten' in u for u in made['p'].fetched))

    def test_review_leads_only_on_request_and_render_validates(self):
        b, _ = backend()
        c = criteria(limit=5)
        qualified, review, stats = b.run(c, set(), lambda: False)
        self.assertEqual(review, [])
        payload = render_search(c, qualified, review, stats)
        self.assertEqual((payload['returned_count'], payload['missing_count']), (2, 3))
        self.assertEqual(payload['metadata']['verification_counts']['review'], 1)
        self.assertEqual(payload['metadata']['source'], 'search')

    def test_other_sector_with_custom_titles(self):
        # Requested titles win over the built-in decision-maker vocabulary ("Head of Sales" is not in it).
        b, _ = backend(**SOFTWARE)
        c = criteria(keywords=['Softwareentwicklung'], titles=['CEO', 'Head of Sales'], locations=['Berlin'], limit=5, max_per_company=None)
        qualified, _, stats = b.run(c, set(), lambda: False)
        self.assertEqual({l['name']: l['verification'] for l in qualified}, {'Lena Code': 'verified', 'Tom Berger': 'verified'})
        self.assertEqual(qualified[0]['company'], 'Codewerk Software GmbH')
        self.assertEqual(qualified[0]['company_website'], 'https://codewerk-synthetic.de')
        # Default target (no titles): decision makers of any kind -> CEO and Head of Sales, not the developer.
        b, _ = backend(**SOFTWARE)
        qualified, _, _ = b.run(criteria(keywords=['Softwareentwicklung'], titles=[], locations=['Berlin'], limit=5, max_per_company=None), set(), lambda: False)
        self.assertEqual(sorted(l['name'] for l in qualified), ['Lena Code', 'Tom Berger'])

    def test_min_verification_linkedin_counts_snippet_only_leads(self):
        # Tech case: the website lists Tom without a title -> website silent, LinkedIn snippet proved role + firm.
        pages = {**SOFTWARE_PAGES, SOFTWARE_TEAM: SOFTWARE_PAGES[SOFTWARE_TEAM].replace('Head of Sales', 'Vertrieb & Kunden')}
        confirm = {**SOFTWARE_CONFIRM, 'Tom Berger': {**SOFTWARE_CONFIRM['Tom Berger'], 'role_on_site': None, 'quote_role': None}}
        data = {**SOFTWARE, 'pages': pages, 'confirm': confirm}
        c = dict(keywords=['Softwareentwicklung'], titles=['CEO', 'Head of Sales'], locations=['Berlin'], limit=5, expand_terms=False,
                 max_per_company=None)
        b, _ = backend(**data)
        qualified, rest, stats = b.run(criteria(**c, include_review=True), set(), lambda: False)
        self.assertEqual([l['name'] for l in qualified], ['Lena Code'])  # default min: plausible
        self.assertEqual([(l['name'], l['verification']) for l in rest], [('Tom Berger', 'linkedin')])
        self.assertEqual(stats['verification_counts']['linkedin'], 1)
        b, _ = backend(**data)
        qualified, _, _ = b.run(criteria(**c, min_verification='linkedin'), set(), lambda: False)
        self.assertEqual([(l['name'], l['verification']) for l in qualified], [('Lena Code', 'verified'), ('Tom Berger', 'linkedin')])
        self.assertEqual(qualified[1]['identity_status'], 'needs_review')
        b, _ = backend(**data)
        qualified, _, _ = b.run(criteria(**c, min_verification='verified'), set(), lambda: False)
        self.assertEqual([l['name'] for l in qualified], ['Lena Code'])

    def test_contradiction_stays_review_even_with_min_linkedin(self):
        b, _ = backend()
        qualified, rest, _ = b.run(criteria(min_verification='linkedin', include_review=True, expand_terms=False), set(), lambda: False)
        self.assertIn(('Otto Probe', 'review'), [(l['name'], l['verification']) for l in rest])  # site lists him as associate
        self.assertNotIn('Otto Probe', [l['name'] for l in qualified])

    def test_term_expansion_searches_and_accepts_equivalent_titles(self):
        profiles = SOFTWARE_PROFILES + [{'url': LI + 'paula-vertrieb-synthetic',
                                         'title': 'Paula Vertrieb – Vertriebsleiterin bei Codewerk Software GmbH',
                                         'description': 'Vertriebsleiterin bei Codewerk Software GmbH · Softwareentwicklung · Berlin'}]
        pages = {**SOFTWARE_PAGES, SOFTWARE_TEAM: SOFTWARE_PAGES[SOFTWARE_TEAM] + '\n\nPaula Vertrieb\nVertriebsleiterin'}
        confirm = {**SOFTWARE_CONFIRM, 'Paula Vertrieb': {**SOFTWARE_CONFIRM['Tom Berger'], 'role_on_site': 'Vertriebsleiterin',
                                                          'quote_role': 'Vertriebsleiterin'}}
        expansion = {'keywords': ['SaaS', 'Softwareentwicklung', 'IT-Dienstleister'], 'titles': ['Vertriebsleiter', 'head of sales']}
        data = {**SOFTWARE, 'profiles': profiles, 'pages': pages, 'confirm': confirm,
                'firm_search': {n: [SOFTWARE_TEAM] for n in confirm}, 'expansion': expansion}
        b, made = backend(**data)
        c = criteria(keywords=['Softwareentwicklung'], titles=['Head of Sales'], locations=['Berlin'], limit=5, max_per_company=None)
        qualified, _, stats = b.run(c, set(), lambda: False)
        self.assertEqual(sorted(l['name'] for l in qualified), ['Paula Vertrieb', 'Tom Berger'])  # CEO not requested
        self.assertEqual(stats['expanded_keywords'], ['SaaS', 'IT-Dienstleister'])  # duplicates of the request dropped
        self.assertEqual(stats['expanded_titles'], ['Vertriebsleiter'])
        queries = made['p'].serp_queries
        self.assertEqual(queries[0], 'site:linkedin.com/in Softwareentwicklung Head of Sales Berlin')  # originals first
        self.assertIn('site:linkedin.com/in SaaS Vertriebsleiter Berlin', queries)
        # Without expansion: no extra LLM call, and the Vertriebsleiterin does not count as Head of Sales.
        b, made = backend(**{**data, 'expansion': expansion})
        qualified, _, stats = b.run(criteria(keywords=['Softwareentwicklung'], titles=['Head of Sales'], locations=['Berlin'],
                                             limit=5, expand_terms=False), set(), lambda: False)
        self.assertEqual([l['name'] for l in qualified], ['Tom Berger'])
        self.assertFalse(any('Suggest LinkedIn search terms' in u for u in made['p'].prompts))

    def test_requested_titles_filter_roles(self):
        b, _ = backend()
        qualified, _, _ = b.run(criteria(titles=['Inhaber']), set(), lambda: False)
        self.assertEqual([l['name'] for l in qualified], ['Max Beispiel'])

    def test_previous_profiles_are_excluded(self):
        b, _ = backend()
        qualified, _, _ = b.run(criteria(), {LI + 'erika-musterfrau-synthetic'}, lambda: False)
        self.assertNotIn('Erika Musterfrau', [l['name'] for l in qualified])

    def test_firm_first_supplement_finds_imprint_decision_makers(self):
        b, _ = backend(firm_first=True)
        qualified, _, stats = b.run(criteria(limit=1), set(), lambda: False)
        self.assertEqual([(l['name'], l['verification']) for l in qualified], [('Paul Neumann', 'verified')])
        self.assertEqual(qualified[0]['linkedin_url'], LI + 'paul-neumann-synthetic')
        self.assertEqual(stats['strategies'], ['linkedin_search', 'firm_websites'])

    def test_cost_cap_stops_and_keeps_partial_result(self):
        b, _ = backend(cost_micro=400_000)
        qualified, _, stats = b.run(criteria(budget={'seconds': 60, 'max_cost_usd': 0.5}), set(), lambda: False)
        self.assertEqual(stats['stop_reason'], 'budget_exhausted')
        # In-flight parallel calls may overshoot by at most one call per worker.
        self.assertLessEqual(stats['cost_usd'], 0.5 + sb.WORKERS * 0.4)

    def test_cancel_raises(self):
        b, _ = backend()
        with self.assertRaises(sb.SearchError) as ctx: b.run(criteria(), set(), lambda: True)
        self.assertEqual(ctx.exception.code, 'cancelled')

    def test_missing_configuration(self):
        meter = sb.Meter(1, time.monotonic() + 10, lambda: False)
        with self.assertRaises(sb.SearchError) as ctx: sb.Providers(meter, None, 'key')
        self.assertEqual(ctx.exception.code, 'search_not_configured')


class ConfigTests(unittest.TestCase):
    def test_env_file_roundtrip_private(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, '.env').write_text('OTHER=1\nTREG_TOKEN=old\n')
            sb.save_env_value('TREG_TOKEN', 'new', home)
            self.assertEqual(Path(home, '.env').read_text(), 'OTHER=1\nTREG_TOKEN=new\n')
            self.assertEqual(sb.env_value('TREG_TOKEN', home), 'new')
            if os.name == 'posix': self.assertEqual(stat.S_IMODE(os.stat(Path(home, '.env')).st_mode), 0o600)

    def test_openrouter_key_from_env_file_or_credential_pool(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, 'auth.json').write_text(json.dumps({'credential_pool': {'openrouter': [
                {'id': 'x', 'label': 'api-key-1', 'api_key': 'sk-or-synthetic-pool'}]}}))
            saved = os.environ.pop('OPENROUTER_API_KEY', None)
            try:
                self.assertEqual(sb.openrouter_key(home), 'sk-or-synthetic-pool')  # hermes auth add
                sb.save_env_value('OPENROUTER_API_KEY', 'sk-or-synthetic-env', home)
                self.assertEqual(sb.openrouter_key(home), 'sk-or-synthetic-env')  # hermes setup model wins
            finally:
                if saved is not None: os.environ['OPENROUTER_API_KEY'] = saved

    def test_team_from_token_payload(self):
        import base64
        payload = base64.urlsafe_b64encode(json.dumps({'org': 'synthetic-team'}).encode()).decode().rstrip('=')
        self.assertEqual(sb.treg_org(payload + '.signature'), 'synthetic-team')
        self.assertIsNone(sb.treg_org('not-a-token'))

    def test_decision_maker_titles(self):
        for text in ('Rechtsanwalt & Partner', 'Rechtsanwältin & Partnerin', 'Rechtsanwalt / Partner', 'Gründungspartner',
                     'Inhaber der Kanzlei', 'Geschäftsführender Gesellschafter', 'Managing Partner', 'Local Partner',
                     'Zahnärztin & Partnerin', 'Architekt und Partner', 'CEO & Co-Founder', 'Head of Sales', 'Vorstand',
                     'CTO', 'Partner bei Muster & Partner mbB'):
            with self.subTest(text=text): self.assertTrue(sb.is_decision_maker_text(text))
        for text in ('Rechtsanwalt', 'Lawyer', 'Associate Partner', 'Senior Associate', 'Rechtsanwalt bei Muster & Partner mbB',
                     'Ansprechpartner', 'Muster Partnerschaft mbB', 'Doctor', 'Senior Developer', 'Business Partner HR',
                     'Zahnärztin bei Muster & Partner'):
            with self.subTest(text=text): self.assertFalse(sb.is_decision_maker_text(text))

    def test_source_quote_returns_literal_line(self):
        self.assertEqual(sb.source_quote('partnerin', 'Team\nRechtsanwältin | Partnerin\nX'), 'Rechtsanwältin | Partnerin')
        self.assertIsNone(sb.source_quote('Gründerin', 'Team\nPartnerin'))
        # Panzer case: whole words only, and the occurrence next to the person, not the first on the page.
        page = '[#COOKIE_BODY_PARTNERS_INTRO#]\nTeam\n# Dr. Erika Musterfrau\n\nPartner\n\nArbeitsrecht'
        self.assertEqual(sb.source_quote('Partner', page, anchor='musterfrau'), '# Dr. Erika Musterfrau\n\nPartner')


class APITests(unittest.TestCase):
    def test_submit_poll_and_dedup_via_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, _ = backend()
            with TestClient(create_app(Path(tmp) / 'data', search_backend=b)) as client:
                body = {'source': 'search', 'keywords': ['Rechtsanwalt'], 'locations': ['München'], 'limit': 2}
                ident = client.post('/api/v1/discoveries', json=body).json()['id']
                for _ in range(300):
                    value = client.get('/api/v1/discoveries/' + ident).json()
                    if value['status'] not in ('queued', 'running'): break
                    time.sleep(0.01)
                self.assertEqual(value['status'], 'succeeded')
                self.assertEqual([l['verification'] for l in value['result']['leads']], ['verified', 'plausible'])
                again = client.post('/api/v1/discoveries', json=body).json()['id']
                for _ in range(300):
                    second = client.get('/api/v1/discoveries/' + again).json()
                    if second['status'] not in ('queued', 'running'): break
                    time.sleep(0.01)
                self.assertEqual(second['returned_count'], 0)  # exclude_previous: already delivered profiles

    def test_unconfigured_search_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            def factory(meter): raise sb.SearchError('search_not_configured')
            with TestClient(create_app(Path(tmp) / 'data', search_backend=sb.SearchBackend(factory))) as client:
                ident = client.post('/api/v1/discoveries', json={'source': 'search', 'keywords': ['x']}).json()['id']
                for _ in range(300):
                    value = client.get('/api/v1/discoveries/' + ident).json()
                    if value['status'] not in ('queued', 'running'): break
                    time.sleep(0.01)
                self.assertEqual((value['status'], value['error']), ('failed', 'search_not_configured'))


if __name__ == '__main__':
    unittest.main()
