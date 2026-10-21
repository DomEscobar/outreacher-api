"""source=search: EXPLICIT SYNTHETIC fixtures only; no network, no model, no real people."""
import base64
import http.client
import io
import json
import os
import re
import stat
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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


PAUL = {'url': LI + 'paul-neumann-synthetic', 'title': 'Paul Neumann – Geschäftsführer – Neu Kanzlei',
        'description': 'Rechtsanwalt bei Neu Kanzlei, München'}
PAUL_IMPRINT = [{'name': 'Paul Neumann', 'current_role': 'Geschäftsführer', 'quote_name_role': 'Geschäftsführer: Rechtsanwalt Paul Neumann'}]


class FakeProviders:
    """Replays synthetic SERP/people-index/page/LLM answers; charges a fixed synthetic cost per call."""
    def __init__(self, meter, profiles=PROFILES, firm_first=False, cost_micro=100, pages=PAGES, firm_search=FIRM_SEARCH,
                 confirm=CONFIRM, sector_quote='Rechtsanw', expansion=None, people=None, people_answers=None,
                 imprint_people=PAUL_IMPRINT, lookups=None, people_order='after_google', imprint_places=None):
        self.meter, self.profiles, self.firm_first, self.cost = meter, profiles, firm_first, cost_micro
        self.pages, self.firm_search, self.confirm, self.sector_quote = pages, firm_search, confirm, sector_quote
        self.serp_queries, self.fetched, self.expansion, self.prompts = [], [], expansion or {}, []
        # people: synthetic raw Exa results (parsed like the real provider); people_answers: name -> LLM item override.
        self.people_results, self.people_answers, self.people_queries, self.people_limits = people or [], people_answers or {}, [], []
        self.people_search, self.people_order = people is not None, people_order
        self.imprint_people, self.lookups = imprint_people, lookups if lookups is not None else {'Paul Neumann': [PAUL]}
        self.imprint_places = imprint_places  # firm-first: only imprint searches naming one of these places find the firm
        self.log = []  # ('people' | 'serp', query) in call order

    def charge(self, kind):
        self.meter.check()
        self.meter.charge(self.cost, kind)

    def people(self, query, limit=sb.PEOPLE_RESULTS):
        self.charge('provider_calls')
        self.people_queries.append(query)
        self.people_limits.append(limit)
        self.log.append(('people', query))
        return [row for row in map(sb.people_row, self.people_results) if row]

    def serp(self, query, limit=10):
        self.charge('provider_calls')
        self.serp_queries.append(query)
        self.log.append(('serp', query))
        row = lambda u: {'url': u, 'title': '', 'description': ''}
        lookup = re.match(r'site:linkedin\.com/in "([^"]+)"', query)
        if lookup:
            return [dict(r) for r in self.lookups.get(lookup.group(1), [])]
        if query.startswith('site:linkedin.com/in'):
            return [] if self.firm_first else [dict(p) for p in self.profiles]
        if 'Impressum' in query and self.firm_first:
            if self.imprint_places and not any(p in query for p in self.imprint_places): return []
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
            parts = re.split(r'^\[(\d+)\] ', user.split('\n\n', 1)[1], flags=re.M)[1:]
            for i, text in zip(parts[::2], parts[1::2]):
                line = text.split('\n')[0]
                position = re.search(r'^Aktuelle Position: (.*?) – (.*)$', text, re.M)
                if position:  # people-index item: name line, then the current positions
                    name, role, company = line, position.group(1), position.group(2)
                else:  # search snippet: "Name – Role bei Company"
                    name, role, company = line.split(' – ')[0], re.search(r'– (.*?) bei', line).group(1), line.split(' bei ')[-1]
                item = {'index': int(i), 'name': name, 'current_role': role, 'company': company,
                        'is_decision_maker': 'Associate' not in line, 'in_sector': True, 'in_location': True,
                        'quote_name': name, 'quote_role': role, 'quote_company': company, 'quote_sector': self.sector_quote}
                items.append({**item, **self.people_answers.get(name, {})})
            return {'items': items}
        if 'From this company imprint' in user:
            return {'in_sector': True, 'quote_sector': 'Rechtsanwaltsgesellschaft', 'company': 'Neu Kanzlei Rechtsanwaltsgesellschaft mbH',
                    'quote_company': 'Neu Kanzlei Rechtsanwaltsgesellschaft mbH', 'people': self.imprint_people}
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
                    {'expand_terms': False}, {'exclude_previous_companies': True}):
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
        self.assertIn('site:linkedin.com/in SaaS (Vertriebsleiter OR Vertriebsleiterin) Berlin', queries)
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


def exa_result(slug, name, current, past=(), highlights=(), url=None, location='München, Bayern, Deutschland'):
    """Synthetic people-index hit in the shape Exa returns (entities.workHistory, highlights)."""
    dated = lambda t, c, to: {'title': t, 'company': {'id': None, 'name': c}, 'dates': {'from': '2010-01-01', 'to': to}}
    return {'id': 'https://exa.ai/library/person/' + slug, 'title': name, 'url': url or 'https://linkedin.com/in/' + slug,
            'highlights': list(highlights), 'entities': [{'type': 'person', 'properties': {
                'name': name, 'location': location,
                'workHistory': [dated(t, c, None) for t, c in current] + [dated(t, c, '2019-12-01') for t, c in past]
                               + ['… 2 more item(s) truncated', {'title': 'Undatiert', 'company': {'name': 'X'}, 'dates': None}]}}]}


CLARA = exa_result('clara-index-synthetic', 'Clara Index', [('Partnerin', 'Index Rechtsanwälte')], highlights=[
    '### Partnerin - [Index Rechtsanwälte](https://www.linkedin.com/company/index-synthetic) (Current)\n...\nRechtsanwälte für Gesellschaftsrecht'])
ERIKA_INDEX = exa_result('erika-musterfrau-synthetic', 'Erika Musterfrau', [('Partnerin', 'Musterfrau Legal')],
                         highlights=['Rechtsanwältin und Partnerin bei Musterfrau Legal'])
# Current role is "Rechtsanwalt"; "Partner" at Alt & Kollegen ended in 2019.
BERND = exa_result('bernd-alt-synthetic', 'Bernd Alt', [('Rechtsanwalt', 'Kanzlei Alt Neu')], [('Partner', 'Alt & Kollegen')],
                   highlights=['### Partner - Alt & Kollegen\nJan 2010 - Dez 2019\nRechtsanwälte'])
STALE_ANSWER = {'Bernd Alt': {'current_role': 'Partner', 'quote_role': 'Partner', 'company': 'Alt & Kollegen',
                              'quote_company': 'Alt & Kollegen'}}  # the LLM quotes the past position
INDEX_PAGE = 'https://index-rechtsanwaelte-synthetic.de/team/clara-index'
PEOPLE = {'pages': {**PAGES, INDEX_PAGE: '# Clara Index\n\nRechtsanwältin | Partnerin\n\nGesellschaftsrecht',
                    'https://index-rechtsanwaelte-synthetic.de/impressum': 'Impressum\nIndex Rechtsanwälte PartG mbB, München'},
          'firm_search': {**FIRM_SEARCH, 'Clara Index': [INDEX_PAGE]},
          'confirm': {**CONFIRM, 'Clara Index': {'person_on_site': True, 'role_on_site': 'Partnerin', 'quote_role': 'Rechtsanwältin | Partnerin',
                                                 'firm_name': 'Index Rechtsanwälte PartG mbB',
                                                 'quote_firm': 'Index Rechtsanwälte PartG mbB, München', 'firm_in_sector': True,
                                                 'quote_sector': 'Rechtsanwälte', 'listed_in_other_role': False}}}


class PeopleIndexTests(unittest.TestCase):
    def test_people_row_current_and_past_positions(self):
        row = sb.people_row(BERND)
        self.assertEqual(row['url'], LI + 'bernd-alt-synthetic')  # canonical www host
        self.assertEqual(row['current'], [('Rechtsanwalt', 'Kanzlei Alt Neu')])  # undated/garbled entries ignored
        self.assertEqual(row['past'], [('Partner', 'Alt & Kollegen')])
        self.assertTrue(row['text'].startswith('Bernd Alt\nAktuelle Position: Rechtsanwalt – Kanzlei Alt Neu\nOrt: München'))
        text = sb.people_row(CLARA)['text']
        self.assertIn('### Partnerin - Index Rechtsanwälte (Current)', text)  # markdown link reduced to its text
        self.assertNotIn('linkedin.com/company', text)
        self.assertNotIn('\n...\n', text)
        long = exa_result('x-synthetic', 'Lang Text', [('CEO', 'Firma')], highlights=['Zeile ' + 'x' * 300] * 10)
        self.assertLessEqual(len(sb.people_row(long)['text']), 1200)
        self.assertIsNone(sb.people_row({**CLARA, 'url': 'https://example-synthetic.de/clara'}))
        self.assertTrue(sb.stale('Partner', 0, row))
        self.assertTrue(sb.stale('Alt & Kollegen', 1, row))
        self.assertFalse(sb.stale('Rechtsanwalt', 0, row))
        self.assertFalse(sb.stale('Partner', 0, {'past': [('Partner', 'A')], 'current': [('Partner', 'B')]}))

    def test_people_index_first_stops_when_target_reached(self):
        b, made = backend(people=[CLARA, BERND], people_answers=STALE_ANSWER, people_order='first', **PEOPLE)
        qualified, _, stats = b.run(criteria(limit=1), set(), lambda: False)
        self.assertEqual([(l['name'], l['verification']) for l in qualified], [('Clara Index', 'verified')])
        self.assertEqual(made['p'].people_queries, ['Partner, Inhaber oder Geschäftsführer im Bereich Rechtsanwalt in München'])
        self.assertEqual(stats['strategies'], ['people_search'])  # no Google LinkedIn round needed
        self.assertFalse(any(q.startswith('site:linkedin.com/in') for q in made['p'].serp_queries))
        self.assertEqual(stats['stale_rejected'], 1)  # Bernd: "Partner" only in a past position
        self.assertEqual(stats['qualified_by_strategy'], {'people_search': 1})
        evidence = qualified[0]['evidence'][0]
        self.assertEqual(evidence['source'], 'people_index')
        self.assertIn('Aktuelle Position: Partnerin – Index Rechtsanwälte', evidence['quote'])
        self.assertEqual(qualified[0]['company'], 'Index Rechtsanwälte PartG mbB')

    def test_search_continues_after_index_and_reads_each_profile_once(self):
        ida = {'url': LI + 'ida-zweit-synthetic', 'title': 'Ida Zweit – Rechtsanwältin & Partnerin bei Index Rechtsanwälte',
               'description': 'Partnerin bei Index Rechtsanwälte · München'}
        b, made = backend(people=[CLARA, ERIKA_INDEX], profiles=[ida] + PROFILES, people_order='first', **PEOPLE)
        qualified, _, stats = b.run(criteria(limit=3), set(), lambda: False)
        self.assertEqual(sorted(l['name'] for l in qualified), ['Clara Index', 'Erika Musterfrau', 'Max Beispiel'])
        self.assertEqual(stats['strategies'], ['people_search', 'linkedin_search'])
        self.assertEqual(stats['qualified_by_strategy'], {'people_search': 2, 'linkedin_search': 1})
        # Erika came from the index; her Google hit is not read again. Index 2, then Ida, Max, Otto, Anna.
        self.assertEqual(stats['candidate_count'], 6)
        # Ida's firm already holds its one lead: skipped before any paid confirmation.
        self.assertFalse(any(p.startswith('Person: Ida Zweit') for p in made['p'].prompts))
        self.assertGreaterEqual(stats['excluded_same_company'], 1)


    def test_people_calls_sized_to_what_is_missing(self):
        b, made = backend(people=[CLARA], people_order='first', **PEOPLE)
        b.run(criteria(limit=1), set(), lambda: False)
        self.assertEqual((made['p'].people_queries, made['p'].people_limits),
                         (['Partner, Inhaber oder Geschäftsführer im Bereich Rechtsanwalt in München'], [10]))
        expansion = {'keywords': ['Anwalt', 'Kanzlei', 'Fachanwalt']}
        b, made = backend(people=[CLARA], expansion=expansion, people_order='first', **PEOPLE)
        b.run(criteria(limit=20), set(), lambda: False)
        self.assertEqual(made['p'].people_limits[:3], [sb.PEOPLE_RESULTS] * 3)  # 20 missing: 3 queries x 25 per round
        self.assertEqual(len(made['p'].people_queries), 4)  # all four planned queries, then Google

    def test_default_google_round_first_then_rounds_alternate(self):
        # Where Google suffices the people index is never paid for.
        b, made = backend(people=[CLARA], **PEOPLE)
        qualified, _, stats = b.run(criteria(limit=1, expand_terms=False), set(), lambda: False)
        self.assertEqual((len(qualified), made['p'].people_queries, stats['strategies']), (1, [], ['linkedin_search']))
        # Where it does not, the index comes right after the first Google round, not after all of them.
        cities = ['München', 'Berlin', 'Hamburg', 'Frankfurt', 'Köln', 'Düsseldorf', 'Stuttgart']
        b, made = backend(people=[CLARA], **PEOPLE)
        qualified, _, stats = b.run(criteria(limit=3, locations=cities, expand_terms=False), set(), lambda: False)
        self.assertEqual(sorted(l['name'] for l in qualified), ['Clara Index', 'Erika Musterfrau', 'Max Beispiel'])
        rounds = [kind for kind, q in made['p'].log if kind == 'people' or q.startswith('site:linkedin.com/in (')
                  or (q.startswith('site:linkedin.com/in ') and '"' not in q)]
        self.assertEqual(rounds, ['serp'] * sb.SERP_PER_ROUND + ['people'])  # one Google round, then the index
        self.assertEqual(stats['strategies'], ['linkedin_search', 'people_search'])
        self.assertEqual(stats['qualified_by_strategy'], {'linkedin_search': 2, 'people_search': 1})


class EvidenceTests(unittest.TestCase):
    def test_website_tier_needs_its_website_facts_in_the_evidence(self):
        b, _ = backend()
        qualified, _, _ = b.run(criteria(include_review=True), set(), lambda: False)
        max_lead = next(l for l in qualified if l['name'] == 'Max Beispiel')
        self.assertEqual(max_lead['verification'], 'plausible')
        site = [e for e in max_lead['evidence'] if e['source'] == 'firm_website']
        self.assertIn('Rechtsanwalt Max Beispiel berät Sie in München.', [e['quote'] for e in site if 'name' in e['supports']])
        self.assertTrue(any('company' in e['supports'] for e in site))
        # The name is on the site only across a line break, the imprint does not name him and the LLM quoted
        # no role: no quote shows the person, so the namesake tier is not granted on the LLM's reading alone.
        pages = {**PAGES, 'https://kanzlei-beispiel-synthetic.de/': 'Kanzlei Beispiel\n\nRechtsanwalt Max\nBeispiel berät Sie.',
                 'https://kanzlei-beispiel-synthetic.de/impressum': 'Impressum\nKanzlei Beispiel, Rechtsanwalt'}
        confirm = {**CONFIRM, 'Max Beispiel': {**CONFIRM['Max Beispiel'], 'quote_role': None, 'role_on_site': None}}
        b, _ = backend(pages=pages, confirm=confirm)
        _, review, _ = b.run(criteria(include_review=True, min_verification='verified'), set(), lambda: False)
        self.assertEqual(next(l['verification'] for l in review if l['name'] == 'Max Beispiel'), 'linkedin')

    def test_quote_kept_when_no_whole_line_matches(self):
        # A quote cut mid-word passes the verbatim check but no whole-word source line: kept as quoted, not dropped.
        confirm = {**CONFIRM, 'Erika Musterfrau': {**CONFIRM['Erika Musterfrau'], 'quote_sector': 'echtsanwälte PartG'}}
        b, _ = backend(confirm=confirm)
        qualified, _, _ = b.run(criteria(), set(), lambda: False)
        erika = next(l for l in qualified if l['name'] == 'Erika Musterfrau')
        self.assertIn({'url': 'https://musterfrau-legal-synthetic.de/impressum', 'quote': 'echtsanwälte PartG',
                       'source': 'firm_website', 'supports': ['sector']}, erika['evidence'])


class QueryPlanTests(unittest.TestCase):
    def test_both_title_forms_in_one_query_and_locations_take_turns(self):
        cities = ['München', 'Berlin', 'Hamburg', 'Frankfurt', 'Köln', 'Düsseldorf', 'Stuttgart']
        p = sb.SearchPipeline(criteria(locations=cities), None, None)
        p.extra_keywords = ['Anwalt', 'Kanzlei', 'Fachanwalt', 'Law Firm']
        queries = p.serp_queries(['Managing Partner', 'Kanzleiinhaber', 'Sozius', 'Founding Partner'])
        self.assertEqual(queries[:2], ['site:linkedin.com/in Rechtsanwalt (Partner OR Partnerin) München',
                                       'site:linkedin.com/in Rechtsanwalt (Partner OR Partnerin) Berlin'])
        self.assertIn('site:linkedin.com/in Rechtsanwalt (Geschäftsführer OR Geschäftsführerin) Stuttgart', queries)
        self.assertEqual(len(queries), sb.MAX_QUERIES)
        # Every expanded sector term reaches every city before the cap (the old plan gave them München only).
        for kw in p.extra_keywords:
            for city in cities:
                with self.subTest(kw=kw, city=city):
                    self.assertTrue(any(q.startswith(f'site:linkedin.com/in {kw} ') and q.endswith(city) for q in queries))
        self.assertEqual(sb.title_query('Head of Sales'), 'Head of Sales')
        self.assertEqual(sb.title_query('Vertriebsleiter'), '(Vertriebsleiter OR Vertriebsleiterin)')

    def test_rounds_stop_at_target(self):
        b, made = backend()
        cities = ['München', 'Berlin', 'Hamburg', 'Frankfurt', 'Köln', 'Düsseldorf', 'Stuttgart']
        qualified, _, stats = b.run(criteria(limit=1, locations=cities, expand_terms=False), set(), lambda: False)
        self.assertEqual(len(qualified), 1)
        searches = [q for q in made['p'].serp_queries if q.startswith('site:linkedin.com/in')]
        self.assertEqual(len(searches), sb.SERP_PER_ROUND)  # one round of 21 planned queries
        self.assertEqual(stats['queries'], sb.SERP_PER_ROUND)
        # Near the target only a small batch is confirmed (Erika, Max), not every candidate (Otto too).
        self.assertEqual(sum(p.startswith('Person: ') for p in made['p'].prompts), 2)


class FirmIdentityTests(unittest.TestCase):
    def test_firm_named_ignores_the_persons_own_name(self):
        own = {'thomas', 'mueller'}
        self.assertTrue(sb.firm_named('Thomas Müller – Partner – Müller Rechtsanwälte', {'mueller'}, own))
        self.assertFalse(sb.firm_named('Thomas Müller – Rechtsanwalt in München', {'mueller'}, own))
        self.assertFalse(sb.firm_named('Paul Neumann – Geschäftsführer', {'neu'}, {'paul', 'neumann'}))  # "neu" in "Neumann"
        self.assertTrue(sb.firm_named('Paul Neumann – Geschäftsführer bei Neu Kanzlei', {'neu'}, {'paul', 'neumann'}))

    def test_profile_found_by_name_and_place_only_is_capped(self):
        with_sector = {**PAUL, 'title': 'Paul Neumann – Geschäftsführer', 'description': 'Rechtsanwalt in München'}
        b, _ = backend(firm_first=True, lookups={'Paul Neumann': [with_sector]})
        qualified, _, _ = b.run(criteria(limit=1), set(), lambda: False)
        self.assertEqual([(l['name'], l['verification'], l['identity_status']) for l in qualified],
                         [('Paul Neumann', 'plausible', 'needs_review')])  # imprint alone would make it verified
        self.assertTrue(qualified[0]['verification_reason'].startswith('LinkedIn profile found by name and place only'))
        place_only = {**PAUL, 'title': 'Paul Neumann – Geschäftsführer', 'description': 'München'}
        b, _ = backend(firm_first=True, lookups={'Paul Neumann': [place_only]})
        qualified, review, _ = b.run(criteria(limit=1, include_review=True), set(), lambda: False)
        self.assertEqual(qualified, [])
        self.assertEqual([(l['name'], l['verification']) for l in review], [('Paul Neumann', 'review')])

    def test_firm_match_wins_over_an_earlier_place_match(self):
        place_only = {'url': LI + 'paul-neumann-other-synthetic', 'title': 'Paul Neumann – Arzt', 'description': 'München'}
        b, _ = backend(firm_first=True, lookups={'Paul Neumann': [place_only, PAUL]})
        qualified, _, _ = b.run(criteria(limit=1), set(), lambda: False)
        self.assertEqual([(l['linkedin_url'], l['verification']) for l in qualified], [(PAUL['url'], 'verified')])


NRW_TOP = ['Köln', 'Düsseldorf', 'Dortmund', 'Essen', 'Duisburg', 'Bochum', 'Wuppertal', 'Bielefeld', 'Bonn', 'Münster']


class RegionTests(unittest.TestCase):
    def test_regions_resolve_to_their_largest_cities(self):
        from regions import expand_locations
        self.assertEqual(expand_locations(['NRW']), (NRW_TOP, ['Nordrhein-Westfalen']))
        for alias in (' nrw ', 'Nordrhein-Westfalen', 'nordrhein westfalen', 'NORDRHEIN–WESTFALEN'):
            with self.subTest(alias=alias): self.assertEqual(expand_locations([alias])[1], ['Nordrhein-Westfalen'])
        places, regions = expand_locations(['München', 'Bayern'])
        self.assertEqual((places[:2], places.count('München'), regions), (['München', 'Nürnberg'], 1, ['Bayern']))
        self.assertEqual(expand_locations(['NRW', 'Bayern'])[0][:4], ['Köln', 'München', 'Düsseldorf', 'Nürnberg'])  # take turns
        self.assertEqual(expand_locations(['Gütersloh', 'Brandenburg an der Havel']),
                         (['Gütersloh', 'Brandenburg an der Havel'], []))  # places stay as given

    def test_region_is_searched_city_by_city_and_checked_as_a_region(self):
        b, made = backend(people=[CLARA], people_order='first', **PEOPLE)
        _, _, stats = b.run(criteria(locations=['NRW'], limit=20, expand_terms=False), set(), lambda: False)
        self.assertEqual(stats['regions'], ['Nordrhein-Westfalen'])
        searches = [q for q in made['p'].serp_queries if q.startswith('site:linkedin.com/in Rechtsanwalt (Partner OR Partnerin)')]
        self.assertEqual(searches, [f'site:linkedin.com/in Rechtsanwalt (Partner OR Partnerin) {c}' for c in NRW_TOP])
        self.assertEqual(made['p'].people_queries[:2], ['Partner, Inhaber oder Geschäftsführer im Bereich Rechtsanwalt in Nordrhein-Westfalen',
                                                        'Partner, Inhaber oder Geschäftsführer im Bereich Rechtsanwalt in Köln'])
        classify = next(p for p in made['p'].prompts if 'For EACH item' in p)
        self.assertIn("located in or near one of ['NRW'] (a state or region includes all its towns)", classify)

    def test_firm_first_matches_profiles_in_the_place_the_firm_was_found(self):
        # The imprint search finds the firm only for Berlin; the LinkedIn hit names Berlin, not the firm.
        berlin = {**PAUL, 'title': 'Paul Neumann – Geschäftsführer', 'description': 'Rechtsanwalt in Berlin'}
        b, made = backend(firm_first=True, lookups={'Paul Neumann': [berlin]}, imprint_places=['Berlin'])
        qualified, _, _ = b.run(criteria(limit=1, locations=['München', 'Berlin']), set(), lambda: False)
        self.assertEqual([(l['name'], l['verification']) for l in qualified], [('Paul Neumann', 'plausible')])
        self.assertIn('site:linkedin.com/in "Paul Neumann" Berlin', made['p'].serp_queries)  # not the first place, München
        self.assertNotIn('site:linkedin.com/in "Paul Neumann" München', made['p'].serp_queries)

    def test_candidates_take_turns_by_place(self):
        found = [{'loc': 'Köln', 'n': 1}, {'loc': 'Köln', 'n': 2}, {'loc': 'Köln', 'n': 3}, {'loc': 'Bonn', 'n': 4},
                 {'loc': 'Essen', 'n': 5}, {'loc': 'Bonn', 'n': 6}]
        self.assertEqual([c['n'] for c in sb.SearchPipeline.spread(found)], [1, 4, 5, 2, 6, 3])

    def test_place_of_a_candidate(self):
        p = sb.SearchPipeline(criteria(locations=['München', 'Berlin']), None, None)
        p.query_loc = {'q': 'München'}
        self.assertEqual(p.place_of('Partnerin · Berlin, Deutschland', 'q'), 'Berlin')  # the place the profile names
        self.assertEqual(p.place_of('Partnerin bei Kanzlei X', 'q'), 'München')  # else the place searched


class FacetTests(unittest.TestCase):
    def test_facets_widen_the_search_but_not_the_checks(self):
        expansion = {'facets': ['Arbeitsrecht', 'Familienrecht', 'rechtsanwalt']}
        b, made = backend(expansion=expansion)
        qualified, _, stats = b.run(criteria(limit=10), set(), lambda: False)
        self.assertEqual(stats['expanded_facets'], ['Arbeitsrecht', 'Familienrecht'])  # the sector term itself dropped
        queries = [q for q in made['p'].serp_queries if q.startswith('site:linkedin.com/in Rechtsanwalt')]
        base = queries.index('site:linkedin.com/in Rechtsanwalt (Partner OR Partnerin) München')
        facet = queries.index('site:linkedin.com/in Rechtsanwalt Arbeitsrecht (Partner OR Partnerin) München')
        self.assertLess(base, facet)
        self.assertEqual(sorted(l['name'] for l in qualified), ['Erika Musterfrau', 'Max Beispiel'])  # same checks as without


class MemoryTests(unittest.TestCase):
    def setUp(self):
        from search_memory import SearchMemory
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = SearchMemory(self.tmp.name)

    def test_repeat_search_skips_people_decided_before(self):
        c = criteria(limit=1, expand_terms=False)
        b, _ = backend()
        qualified, _, stats = b.run(c, set(), lambda: False, None, self.memory.view(c))
        self.assertEqual(([l['name'] for l in qualified], stats['skipped_known']), (['Erika Musterfrau'], 0))
        p = sb.SearchPipeline(c, None, None, memory=self.memory.view(c))
        p.serp_queries([])
        self.assertEqual(p.stats['demoted_queries'], 0)  # Otto is still open: the queries are not spent
        # Delivered: Erika (excluded as before). Decided: Erika, Anna (rejected). Max was checked and counts but
        # was over the limit, Otto never checked: both stay open and come back.
        b, made = backend()
        qualified, review, stats = b.run(criteria(limit=1, expand_terms=False, include_review=True),
                                         {LI + 'erika-musterfrau-synthetic'}, lambda: False, None, self.memory.view(c))
        self.assertEqual(stats['skipped_known'], 1)
        classify = '\n'.join(p for p in made['p'].prompts if 'For EACH item' in p)
        self.assertNotIn('Anna Assoc', classify)
        self.assertIn('Max Beispiel', classify)
        self.assertIn('Otto Probe', classify)
        self.assertEqual(([l['name'] for l in qualified], [l['name'] for l in review]), (['Max Beispiel'], ['Otto Probe']))
        # Other criteria: a fresh start.
        other = criteria(limit=1, expand_terms=False, titles=['Partner'])
        _, _, stats = backend()[0].run(other, set(), lambda: False, None, self.memory.view(other))
        self.assertEqual(stats['skipped_known'], 0)

    def test_queries_that_found_nothing_new_go_last(self):
        from search_memory import signature
        c = criteria()
        self.memory.save(signature(c), {'spent': 0, 'productive': 3}, set())
        view = self.memory.view(c)
        self.assertEqual(view.order(['spent', 'productive', 'never run']), (['productive', 'never run', 'spent'], 1))
        # In a job: an earlier run whose Google queries left nothing open puts them behind the fresh ones.
        p = sb.SearchPipeline(c, None, None, memory=view)
        first = p.serp_queries([])[0]
        self.memory.save(signature(c), {first: 0}, set())
        p = sb.SearchPipeline(c, None, None, memory=self.memory.view(c))
        plan = p.serp_queries([])
        self.assertEqual((plan[-1], p.stats['demoted_queries']), (first, 1))

    def test_a_run_that_decides_every_result_spends_its_queries(self):
        # limit 10: Erika, Max and Otto are checked, Anna rejected; nothing stays open, so a repeat run
        # puts all three Google queries behind fresh ones (facets, other places) at once.
        c = criteria(limit=10, expand_terms=False)
        backend()[0].run(c, set(), lambda: False, None, self.memory.view(c))
        p = sb.SearchPipeline(c, None, None, memory=self.memory.view(c))
        p.serp_queries([])
        self.assertEqual(p.stats['demoted_queries'], 3)

    def test_memory_is_private_pseudonymous_and_expires(self):
        from search_memory import SearchMemory
        c = criteria(limit=1, expand_terms=False)
        backend()[0].run(c, set(), lambda: False, None, self.memory.view(c))
        path = Path(self.tmp.name) / 'search_memory.sqlite3'
        if os.name == 'posix': self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        raw = path.read_bytes()  # queries are stored as run (sector, titles, place), people only as hashes
        for clear in (b'anna-associate', b'max-beispiel', b'Anna', b'Beispiel', b'musterfrau-legal'):
            with self.subTest(clear=clear): self.assertNotIn(clear, raw)
        self.assertEqual(len(self.memory.view(c).decided), 2)  # Erika delivered, Anna rejected (Max over the limit: open)
        self.assertEqual(SearchMemory(self.tmp.name, retention_days=0).view(c).decided, set())

    def test_api_uses_memory_only_when_excluding_previous_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, _ = backend()
            with TestClient(create_app(Path(tmp) / 'data', search_backend=b)) as client:
                def run(body):
                    ident = client.post('/api/v1/discoveries', json=body).json()['id']
                    for _ in range(300):
                        job = client.get('/api/v1/discoveries/' + ident).json()
                        if job['status'] not in ('queued', 'running'): return job
                        time.sleep(0.01)
                body = {'source': 'search', 'keywords': ['Rechtsanwalt'], 'locations': ['München'], 'limit': 2}
                run(body)
                self.assertEqual(run(body)['result']['metadata']['skipped_known'], 2)  # Anna and Otto, decided in run 1
                self.assertEqual(run({**body, 'exclude_previous': False})['result']['metadata']['skipped_known'], 0)
            self.assertTrue((Path(tmp) / 'data' / 'search_memory.sqlite3').exists())


class KnownCompanyTests(unittest.TestCase):
    def test_exclude_previous_companies(self):
        known = {'known_domains': {'musterfrau-legal-synthetic.de'}, 'known_names': {'kanzlei beispiel'}}
        b, made = backend()
        qualified, _, stats = b.run(criteria(exclude_previous_companies=True), set(), lambda: False, known)
        self.assertEqual(qualified, [])
        self.assertEqual(stats['excluded_known_company'], 2)
        self.assertFalse(any(p.startswith('Person: Max Beispiel') for p in made['p'].prompts))  # skipped by name before paying
        qualified, _, _ = backend()[0].run(criteria(), set(), lambda: False, known)  # default: firms may come again
        self.assertEqual(sorted(l['name'] for l in qualified), ['Erika Musterfrau', 'Max Beispiel'])


def synthetic_token():
    return base64.urlsafe_b64encode(json.dumps({'org': 'synthetic-team'}).encode()).decode().rstrip('=') + '.signature'


class Route:
    """Synthetic HTTP layer for the real Providers class: `answer(url, body)` returns (body, headers) or raises."""
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, url, body, headers, timeout=90, retries=4, auth_fatal=True):
        self.calls.append({'url': url, 'body': body, 'headers': headers, 'auth_fatal': auth_fatal,
                           'timeout': timeout, 'retries': retries})
        return self.answer(url, body)


def providers(answer, max_cost=1.0):
    meter = sb.Meter(max_cost, time.monotonic() + 60, lambda: False)
    return sb.Providers(meter, synthetic_token(), 'sk-or-synthetic'), meter, Route(answer)


def fail(kind):
    def answer(url, body): raise sb.ProviderFailure(kind)
    return answer


class ProviderTests(unittest.TestCase):
    def test_people_index_direct_body_and_cost_header(self):
        p, meter, route = providers(lambda url, body: ({'results': [CLARA, {'url': 'not-a-profile'}]}, {'X-Treg-Cost-Micro': '22000'}))
        with mock.patch.object(sb, 'http_post', route):
            rows = p.people('Partner im Bereich Rechtsanwalt in München')
        self.assertEqual([r['url'] for r in rows], [LI + 'clara-index-synthetic'])
        call = route.calls[0]
        self.assertTrue(call['url'].endswith('/exa.people.search'))
        self.assertEqual((call['body']['category'], call['body']['includeDomains']), ('people', ['linkedin.com']))
        self.assertEqual(call['headers']['X-Treg-Route-Max-Cost'], '0.2')
        self.assertFalse(call['auth_fatal'])  # an endpoint not enabled for the team is not a broken configuration
        self.assertEqual(meter.cost_micro, 22000)
        # Without a cost header: the provider's own costDollars, else the documented price as an estimate.
        p, meter, route = providers(lambda url, body: ({'results': [], 'costDollars': {'total': 0.007}}, {}))
        with mock.patch.object(sb, 'http_post', route): p.people('x')
        self.assertEqual(meter.cost_micro, 7000)
        p, meter, route = providers(lambda url, body: ({'results': []}, {}))
        with mock.patch.object(sb, 'http_post', route): p.people('x', limit=25)
        self.assertEqual(meter.cost_micro, 7000 + 15 * 1000)

    def test_routed_serp_output_and_charge(self):
        body = {'output': {'results': [{'link': 'https://firma-synthetic.de', 'title': 'T', 'snippet': 'S'}]},
                '_treg': {'charged_micro': 900}}
        p, meter, route = providers(lambda url, b: (body, {}))
        with mock.patch.object(sb, 'http_post', route):
            self.assertEqual(p.serp('q'), [{'url': 'https://firma-synthetic.de', 'title': 'T', 'description': 'S'}])
        self.assertEqual((meter.cost_micro, route.calls[0]['body']['limit']), (900, sb.SERP_DEPTH))

    def test_page_fetches_get_little_time_and_nothing_runs_past_the_deadline(self):
        p, meter, route = providers(lambda url, body: ({'output': {'pages': [{'markdown': 'x'}]}}, {}))
        with mock.patch.object(sb, 'http_post', route):
            p.extract('https://firma-synthetic.de')
            p.serp('q')
            meter.deadline = time.monotonic() + 12  # the job ends in 12 s
            p.llm('x')
        self.assertEqual([(c['timeout'], c['retries']) for c in route.calls[:2]], [(30, 2), (45, 3)])
        self.assertLessEqual(route.calls[2]['timeout'], 12)  # not the LLM's 120 s
        self.assertEqual(set(meter.timing), {'extract', 'serp', 'llm'})
        self.assertEqual(meter.timing['extract']['calls'], 1)

    def test_payment_required_stops_the_job(self):
        p, meter, route = providers(fail('http_402'))
        with mock.patch.object(sb, 'http_post', route), self.assertRaises(sb.Stop) as ctx: p.extract('https://firma-synthetic.de')
        self.assertEqual(ctx.exception.reason, 'provider_error')
        with self.assertRaises(sb.Stop): meter.check()  # every other worker stops at its next call

    def test_repeated_search_failures_stop_but_page_fetch_failures_only_count(self):
        p, meter, route = providers(fail('http_404'))
        with mock.patch.object(sb, 'http_post', route):
            for _ in range(10): self.assertEqual(p.extract('https://firma-synthetic.de/impressum'), '')
        self.assertEqual(meter.errors, {'treg_http_404': 10})
        meter.check()  # still running
        answers = iter([sb.ProviderFailure('http_500')] * 4 + [({'output': {'results': []}}, {})] + [sb.ProviderFailure('http_500')] * 5)
        def flaky(url, body):
            a = next(answers)
            if isinstance(a, Exception): raise a
            return a
        p, meter, route = providers(flaky)
        with mock.patch.object(sb, 'http_post', route):
            for _ in range(5): p.serp('q')  # four failures, then a success resets the streak
            for _ in range(4): p.serp('q')
            with self.assertRaises(sb.Stop) as ctx: p.serp('q')
        self.assertEqual((ctx.exception.reason, meter.errors), ('provider_error', {'treg_http_500': 9}))

    def test_people_index_disabled_after_failures(self):
        p, meter, route = providers(fail('http_403'))
        with mock.patch.object(sb, 'http_post', route):
            self.assertEqual(p.people('x'), [])
            self.assertTrue(p.people_search)
            self.assertEqual(p.people('x'), [])
        self.assertFalse(p.people_search)
        self.assertEqual(meter.errors, {'treg_http_403': 2})
        meter.check()  # the job goes on with Google

    def test_llm_error_body_is_a_failure(self):
        p, meter, route = providers(lambda url, body: ({'error': {'message': 'synthetic'}}, {}))
        with mock.patch.object(sb, 'http_post', route): self.assertEqual(p.llm('x'), {})
        self.assertEqual(meter.errors, {'openrouter_invalid_response': 1})
        p, meter, route = providers(fail('http_402'))
        with mock.patch.object(sb, 'http_post', route), self.assertRaises(sb.Stop): p.llm('x')
        self.assertEqual(meter.errors, {'openrouter_http_402': 1})  # says which account to top up
        answer = {'choices': [{'message': {'content': '{"a": 1}'}}], 'usage': {'cost': 0.0001}}
        p, meter, route = providers(lambda url, body: (answer, {}))
        with mock.patch.object(sb, 'http_post', route): self.assertEqual(p.llm('x'), {'a': 1})
        self.assertEqual((meter.cost_micro, meter.errors), (100, {}))

    def test_provider_error_without_leads_fails_the_job_and_partial_otherwise(self):
        class Failing(FakeProviders):
            def serp(self, query, limit=10):
                if len(self.serp_queries) >= self.ok_calls: self.meter.failure('http_402')
                return super().serp(query, limit)
        def run(ok_calls, **kw):
            def factory(meter):
                p = Failing(meter, **kw)
                p.ok_calls = ok_calls
                return p
            return sb.SearchBackend(factory).run(criteria(limit=3, expand_terms=False), set(), lambda: False)
        with self.assertRaises(sb.SearchError) as ctx: run(0)
        self.assertEqual((ctx.exception.code, list(ctx.exception.provider_errors)), ('provider_error', ['http_402']))
        qualified, review, stats = run(1, people=[CLARA], people_order='first', **PEOPLE)  # Clara's firm lookup works, then the balance is gone
        self.assertEqual(([l['name'] for l in qualified], stats['stop_reason']), (['Clara Index'], 'provider_error'))
        self.assertEqual(list(stats['provider_errors']), ['http_402'])  # one per parallel in-flight search
        payload = render_search(criteria(limit=3, expand_terms=False), qualified, review, stats)
        self.assertEqual(payload['metadata']['stop_reason'], 'provider_error')
        self.assertEqual(payload['metadata']['provider_errors'], stats['provider_errors'])


class HttpPostTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __init__(self, body, headers=None):
            super().__init__(json.dumps(body).encode())
            self.headers = headers or {}

    def http_error(self, code):
        return urllib.error.HTTPError('https://synthetic.invalid', code, 'synthetic', http.client.HTTPMessage(), None)

    def post(self, *effects, **kw):
        with mock.patch('urllib.request.urlopen', side_effect=list(effects)), mock.patch.object(sb.time, 'sleep'):
            return sb.http_post('https://synthetic.invalid/x', {}, {}, **kw)

    def test_retries_then_returns_body_and_headers(self):
        body, headers = self.post(self.http_error(503), self.Response({'ok': 1}, {'X-Treg-Cost-Micro': '5'}))
        self.assertEqual((body, headers['X-Treg-Cost-Micro']), ({'ok': 1}, '5'))

    def test_final_failures_raise_instead_of_returning_nothing(self):
        for effects, kind in (((self.http_error(402),), 'http_402'), ((self.http_error(404),), 'http_404'),
                              ((urllib.error.URLError('down'),) * 4, 'network'), ((self.http_error(500),) * 4, 'http_500')):
            with self.subTest(kind=kind), self.assertRaises(sb.ProviderFailure) as ctx: self.post(*effects)
            self.assertEqual(ctx.exception.kind, kind)
        with self.assertRaises(sb.SearchError) as ctx: self.post(self.http_error(401))
        self.assertEqual(ctx.exception.code, 'search_not_configured')
        with self.assertRaises(sb.ProviderFailure) as ctx: self.post(self.http_error(403), auth_fatal=False)
        self.assertEqual(ctx.exception.kind, 'http_403')


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

    def test_provider_error_fails_the_job_visibly(self):
        class Broke(FakeProviders):
            def serp(self, query, limit=10): self.meter.failure('http_402')  # e.g. treg balance used up
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(Path(tmp) / 'data', search_backend=sb.SearchBackend(Broke))) as client:
                ident = client.post('/api/v1/discoveries', json={'source': 'search', 'keywords': ['x']}).json()['id']
                for _ in range(300):
                    value = client.get('/api/v1/discoveries/' + ident).json()
                    if value['status'] not in ('queued', 'running'): break
                    time.sleep(0.01)
                self.assertEqual((value['status'], value['error']), ('failed', 'provider_error'))  # not "0 leads found"


if __name__ == '__main__':
    unittest.main()
