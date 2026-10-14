"""EXPLICIT SYNTHETIC fixtures only, including actual subprocess/API integration."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import create_app
from models import Budget, Criteria, accepted_leads, role_matches, scope_matches
from policy import install_policy
from research import BackendError, parse_events, pass_data
from worker import HermesBackend, prompt
from test_discovery import LEAD, OBS, PROFILE, SOURCE


def without_sector(lead):
    lead = copy.deepcopy(lead)
    for e in lead['evidence']: e['supports'] = [s for s in e['supports'] if s != 'sector']
    return lead


def events(entries, **updates):
    value = {'outcome': 'completed', 'leads': entries, **updates}
    return [{'type': 'tool_result', 'name': 'web_extract', 'output': OBS[0]},
            {'type': 'result', 'exit_code': 0, 'text': json.dumps(value)}]


class SalvageTests(unittest.TestCase):
    def test_invalid_optional_website_salvages_all_six(self):
        leads = [copy.deepcopy(LEAD) for _ in range(6)]
        observations = []
        for i, lead in enumerate(leads):
            lead['name'] += f' {i}'
            lead['linkedin_url'] += f'-{i}'
            evidence = lead['evidence'][0]
            evidence['url'] = lead['linkedin_url']
            evidence['quote'] = f"{lead['name']} is Synthetic Engineer at Synthetic Company."
            observations.append(evidence['url'] + '\n' + evidence['quote'])
        leads[3]['company_website'] = 'http://localhost/private'
        ev = events(leads)
        ev[0]['output'] = '\n'.join(observations)
        parsed, observed = parse_events(ev)
        self.assertEqual(len(parsed.leads), 6)
        self.assertIsNone(parsed.leads[3].company_website)
        self.assertIn('company_website', parsed.leads[3].missing_information)
        self.assertEqual(parsed._stats['normalized_website_count'], 1)
        self.assertEqual(len(accepted_leads(parsed, observed, 10, set())), 6)

    def test_bad_individual_required_and_unknown_fields_are_dropped(self):
        variants = [{**LEAD, 'name': ''}, {**LEAD, 'linkedin_url': SOURCE}, {**LEAD, 'email': 'synthetic'},
                    {**LEAD, 'company_website': 123},
                    {**LEAD, 'evidence': [{**LEAD['evidence'][0], 'url': 'http://localhost'}]},
                    {**LEAD, 'evidence': [{**LEAD['evidence'][0], 'extra': 'forbidden'}]}]
        parsed, observed = parse_events(events([LEAD] + variants))
        self.assertEqual(len(parsed.leads), 1)
        self.assertEqual(parsed._stats['schema_rejected_count'], 6)
        self.assertEqual(len(accepted_leads(parsed, observed, 10, set())), 1)

    def test_strict_envelope_and_no_raw_error(self):
        for update in [{'extra': 'SYNTHETIC SECRET'}, {'outcome': 'invented'}, {'blocked_reason': 'challenge'}, {'leads': [LEAD] * 31}, {'leads': 'bad'}]:
            with self.subTest(update=update), self.assertRaises(BackendError) as caught:
                parse_events(events([LEAD], **update))
            self.assertNotIn('SECRET', str(caught.exception))

    def test_unambiguous_prose_only_not_multiple_objects(self):
        ev = events([LEAD])
        ev[-1]['text'] = 'Results\n' + ev[-1]['text'] + '\nEnd'
        self.assertEqual(len(parse_events(ev)[0].leads), 1)
        ev[-1]['text'] += '\n{"debug":"SYNTHETIC SECRET"}'
        with self.assertRaises(BackendError): parse_events(ev)

    def test_only_successful_exact_allowlist_tool_observations(self):
        for name in ['browser_exec', 'browser_vault_fill', 'web_fake', 'terminal']:
            ev = events([LEAD]); ev[0]['name'] = name
            self.assertEqual(parse_events(ev)[1], [])
        ev = events([LEAD]); ev[0]['is_error'] = True
        self.assertEqual(parse_events(ev)[1], [])

    def test_search_snippet_alone_is_not_direct_profile_document(self):
        ev = events([LEAD]); ev[0]['name'] = 'web_search'
        parsed, observed = parse_events(ev)
        self.assertEqual(observed, OBS)  # exact successful text retained, no concatenation
        self.assertEqual(accepted_leads(parsed, observed, 1, set()), [])

    def test_direct_profile_document_name_not_unrelated_profile_link(self):
        lead = copy.deepcopy(LEAD)
        lead['evidence'][0]['url'] = SOURCE
        parsed, _ = parse_events(events([lead]))
        self.assertEqual(accepted_leads(parsed, [SOURCE + '\n' + lead['evidence'][0]['quote']], 1, set()), [])
        ev = events([LEAD]); ev[0]['output'] = PROFILE + '-different\n' + LEAD['evidence'][0]['quote']
        parsed, observed = parse_events(ev)
        self.assertEqual(accepted_leads(parsed, observed, 1, set()), [])

    def test_verbatim_observation_and_fact_support_not_combined_or_guessed(self):
        ev = events([LEAD]); ev[0]['output'] = PROFILE; ev.insert(1, {'type': 'tool_result', 'name': 'web_extract', 'output': LEAD['evidence'][0]['quote']})
        parsed, observed = parse_events(ev)
        self.assertEqual(accepted_leads(parsed, observed, 1, set()), [])
        lead = copy.deepcopy(LEAD); lead['current_role'] = 'Invented Partner'
        parsed, observed = parse_events(events([lead]))
        self.assertEqual(accepted_leads(parsed, observed, 1, set()), [])

    def test_requested_titles_require_observed_matching_role(self):
        parsed, observed = parse_events(events([LEAD]))
        self.assertEqual(accepted_leads(parsed, observed, 1, set(), ['Partner']), [])
        self.assertEqual(len(accepted_leads(parsed, observed, 1, set(), ['Engineer'])), 1)

    def test_explicit_german_decision_maker_variants_not_guessed_synonyms(self):
        titles = ['Inhaber', 'Partner', 'Geschäftsführer']
        for role in ['Geschäftsführer', 'GESCHÄFTSFÜHRERIN', 'geschäftsführender Gesellschafter',
                     'Geschäftsführende Partnerin', 'Mitinhaber', 'Inhaberin', 'Managing Partner', 'Partnerinnen']:
            with self.subTest(role=role): self.assertTrue(role_matches(role, titles))
        for role in [None, 'Rechtsanwalt', 'Jurist', 'Geschäftsleitung', 'Gründer', 'GF',
                     'Partnerschaftsberater', 'HR Business Partner', 'Partner Manager', 'Assistent des Geschäftsführers']:
            with self.subTest(role=role): self.assertFalse(role_matches(role, titles))
        self.assertTrue(role_matches('Assistant Engineer', ['Assistant Engineer']))

    def test_unknown_role_does_not_count_and_legal_scope_not_unrelated_sector(self):
        lead = copy.deepcopy(LEAD); lead['current_role'] = None
        parsed, observed = parse_events(events([lead]))
        self.assertEqual(len(accepted_leads(parsed, observed, 10, set())), 1)
        self.assertEqual(accepted_leads(parsed, observed, 10, set(), ['Partner']), [])
        # No hard-coded sector vocabulary: any keyword needs observed evidence marked 'sector'.
        no_sector = without_sector(LEAD)
        self.assertTrue(scope_matches(no_sector, []))
        for keywords in (['kanzlei'], ['industrial automation']):
            with self.subTest(keywords=keywords):
                self.assertFalse(scope_matches(no_sector, keywords))
                self.assertTrue(scope_matches(LEAD, keywords))

    def test_sector_evidence_must_be_observed_not_claimed(self):
        parsed, observed = parse_events(events([LEAD]))
        self.assertEqual(len(accepted_leads(parsed, observed, 1, set(), keywords=['synthetic sector'])), 1)
        invented = without_sector(LEAD)
        invented['evidence'].append({'url': SOURCE, 'quote': 'Synthetic invented sector claim.', 'supports': ['sector']})
        parsed, observed = parse_events(events([invented]))
        self.assertEqual(accepted_leads(parsed, observed, 1, set(), keywords=['synthetic sector']), [])

    def test_supplied_url_and_ids_not_rewritten_for_page_hint(self):
        url = 'https://www.linkedin.com/search/results/people/?keywords=A%20B&geoUrn=%5B%22urn%3Ali%3Ageo%3A012%22%5D&page=8'
        c = Criteria(search_url=url)
        data = pass_data(c, 2, 3)
        self.assertEqual(data['normal_search_page'], 9)
        self.assertIn('\nTARGET_URL=' + json.dumps(url), prompt(c, {PROFILE}, data))
        self.assertEqual(c.target_url(), url)


class DispatchBudgetTests(unittest.TestCase):
    def test_parallel_calls_cannot_exceed_process_local_budget(self):
        calls = []
        stub = SimpleNamespace(_select_tool_names=lambda: {'web_search'}, get_tool_definitions=lambda: [],
                               _execute_tool=lambda n, a: calls.append(n) or 'synthetic output')
        install_policy(stub, tool_budget=3)
        threads = [threading.Thread(target=stub._execute_tool, args=('web_search', {'query': 'synthetic'})) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(stub._execute_tool('web_search', {}))['error'], 'discovery_tool_budget_exhausted')

    def test_invalid_budget_fails_closed(self):
        stub = SimpleNamespace(_select_tool_names=lambda: set(), get_tool_definitions=lambda: [], _execute_tool=lambda *a: None)
        for value in ['0', '31', 'not-a-number']:
            with patch.dict(os.environ, {'DISCOVERY_TOOL_BUDGET': value}), self.assertRaises(RuntimeError): install_policy(stub)


class MultiPassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.executable = Path(self.tmp.name) / 'synthetic-hermes'
        self.executable.write_text('#!' + sys.executable + '\n' + Path(__file__).with_name('synthetic_multipass.py').read_text())
        self.executable.chmod(0o700)

    def run_fixture(self, mode='valid', limit=10, turns=30, seconds=15, output=2*1024*1024, cancelled=lambda: False, previous=None):
        c = Criteria(keywords=['synthetic'], titles=['Partner'], limit=limit,
                     budget=Budget.model_construct(seconds=seconds, max_turns=turns))
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': mode}):
            return HermesBackend(str(self.executable), output).run(c, previous or set(), cancelled)

    def test_ten_supported_synthetic_profiles_four_bounded_batches(self):
        result, obs = self.run_fixture()
        self.assertEqual(len(accepted_leads(result, obs, 10, set(), ['Partner'])), 10)
        stats = result._stats
        self.assertEqual([p['requested_count'] for p in stats['iterations']], [3, 3, 3, 1])
        self.assertEqual([p['normal_search_page'] for p in stats['iterations']], [1, 2, 3, 4])
        self.assertEqual((stats['iteration_count'], stats['charged_turns'], stats['tool_use_count']), (4, 30, 4))
        self.assertEqual(stats['stop_reason'], 'target_reached')

    def test_turn_budget_not_reset_each_pass(self):
        result, _ = self.run_fixture(turns=16)
        self.assertEqual(len(result.leads), 6)
        self.assertEqual(result._stats['charged_turns'], 16)
        self.assertEqual(result._stats['stop_reason'], 'turn_limit')

    def test_six_observed_generic_kanzlei_profiles_zero_qualified(self):
        c = Criteria(keywords=['kanzlei'], titles=['Inhaber', 'Partner', 'Geschäftsführer'],
                     limit=10, budget=Budget(seconds=15, max_turns=16))
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'unqualified'}):
            result, _ = HermesBackend(str(self.executable)).run(c, set(), lambda: False)
        self.assertEqual(result.leads, [])
        self.assertEqual(result.outcome, 'partial')
        self.assertEqual(result._stats['candidate_count'], 6)
        self.assertEqual(result._stats['normalized_website_count'], 1)
        self.assertEqual(result._stats['qualified_count'], 0)
        self.assertEqual(result._stats['rejected_unqualified_count'], 6)
        self.assertEqual(result._stats['rejected_out_of_scope_count'], 0)
        self.assertEqual(result._stats['rejected_count'], 6)

    def test_excess_tool_events_stop_and_charge_conservatively(self):
        with self.assertRaises(BackendError) as caught: self.run_fixture('too_many_tools', turns=2)
        self.assertEqual(caught.exception.code, 'turn_limit')

    def test_prior_profiles_excluded_even_when_batch_repeats(self):
        result, _ = self.run_fixture('duplicates', turns=30)
        self.assertEqual(len(result.leads), 3)
        self.assertEqual(result._stats['iteration_count'], 4)
        self.assertEqual(result._stats['rejected_count'], 9)
        self.assertEqual(result._stats['stop_reason'], 'turn_limit')

    def test_failure_later_keeps_earlier_supported_results_and_redacts(self):
        for mode, code in [('invalid', 'invalid_stream'), ('schema', 'invalid_result_schema'), ('exit', 'backend_exit'), ('flood', 'output_limit')]:
            with self.subTest(mode=mode):
                result, obs = self.run_fixture(mode, output=20000)
                self.assertEqual(len(accepted_leads(result, obs, 10, set(), ['Partner'])), 3)
                self.assertEqual(result.outcome, 'partial')
                self.assertEqual(result._stats['stop_reason'], code)
                self.assertNotIn('SECRET', json.dumps(result._stats))
                self.assertEqual(result._stats['iteration_count'], 2)

    def test_single_deadline_preserves_progress_on_later_hang(self):
        start = time.monotonic()
        result, _ = self.run_fixture('hang', seconds=1)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(result.leads), 3)
        self.assertEqual(result._stats['stop_reason'], 'timed_out')

    def test_cumulative_output_cap_not_per_pass(self):
        result, _ = self.run_fixture('padding', output=7000)
        self.assertEqual(len(result.leads), 3)
        self.assertEqual(result._stats['stop_reason'], 'output_limit')
        self.assertGreater(result._stats['output_bytes'], 7000)

    def test_blocked_later_stops_no_alternate_navigation(self):
        result, _ = self.run_fixture('blocked')
        self.assertEqual(len(result.leads), 3)
        self.assertEqual(result.outcome, 'blocked')
        self.assertEqual(result.blocked_reason, 'access_limit')
        self.assertEqual(result._stats['iteration_count'], 2)

    def test_cancellation_never_persists_partial_result(self):
        start = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            self.run_fixture('delay', cancelled=lambda: time.monotonic() - start > 0.2)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_empty_results_are_bounded(self):
        result, _ = self.run_fixture('empty', turns=3)
        self.assertEqual(result.leads, [])
        self.assertEqual(result._stats['iteration_count'], 1)
        self.assertEqual(result._stats['stop_reason'], 'turn_limit')

    def test_normal_pagination_stops_at_ten_and_preserves_exact_query(self):
        url = 'https://www.linkedin.com/search/results/people/?keywords=A%20B&geoUrn=%5B%220012%22%5D&page=10'
        c = Criteria(search_url=url, limit=10, budget=Budget(seconds=15, max_turns=30))
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'valid'}):
            result, _ = HermesBackend(str(self.executable)).run(c, set(), lambda: False)
        self.assertEqual(len(result.leads), 3)
        self.assertEqual(result._stats['iteration_count'], 1)
        self.assertEqual(result._stats['iterations'][0]['normal_search_page'], 10)
        self.assertEqual(result._stats['stop_reason'], 'search_exhausted')

    def test_api_later_schema_failure_persists_supported_partial_and_counts(self):
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'schema'}), TestClient(create_app(Path(self.tmp.name) / 'partial-data', backend=HermesBackend(str(self.executable)))) as client:
            response = client.post('/api/v1/discoveries', json={'keywords': ['synthetic'], 'titles': ['Partner'], 'limit': 10, 'budget': {'seconds': 15, 'max_turns': 30}})
            ident = response.json()['id']
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = client.get('/api/v1/discoveries/' + ident).json()
                if value['status'] not in {'queued', 'running'}: break
                time.sleep(0.01)
            self.assertEqual(value['status'], 'partial')
            self.assertEqual((value['returned_count'], value['missing_count']), (3, 7))
            self.assertEqual(value['result']['metadata']['stop_reason'], 'invalid_result_schema')
            self.assertEqual(value['result']['metadata']['iterations'][1]['error'], 'invalid_result_schema')
            self.assertNotIn('SECRET', json.dumps(value))
            self.assertEqual(client.get('/api/v1/discoveries/' + ident).json()['result'], value['result'])

    def test_api_unqualified_six_are_not_returned_or_persisted_as_seen(self):
        directory = Path(self.tmp.name) / 'unqualified-data'
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'unqualified'}), TestClient(create_app(directory, backend=HermesBackend(str(self.executable)))) as client:
            response = client.post('/api/v1/discoveries', json={'keywords': ['kanzlei'], 'titles': ['Inhaber', 'Partner', 'Geschäftsführer'], 'limit': 10, 'budget': {'seconds': 15, 'max_turns': 16}})
            ident = response.json()['id']
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = client.get('/api/v1/discoveries/' + ident).json()
                if value['status'] not in {'queued', 'running'}: break
                time.sleep(0.01)
            self.assertEqual(value['status'], 'partial')
            self.assertEqual((value['returned_count'], value['missing_count']), (0, 10))
            self.assertEqual(value['result']['leads'], [])
            self.assertEqual(value['result']['metadata']['qualified_count'], 0)
            self.assertEqual(value['result']['metadata']['rejected_unqualified_count'], 6)
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(directory / 'jobs.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM seen').fetchone()[0], 0)
            self.assertEqual(client.get('/api/v1/discoveries/' + ident).json()['result'], value['result'])

    def test_api_actual_subprocess_iterative_integration_and_openapi(self):
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'valid'}), TestClient(create_app(Path(self.tmp.name) / 'data', backend=HermesBackend(str(self.executable)))) as client:
            response = client.post('/api/v1/discoveries', json={'keywords': ['synthetic'], 'titles': ['Partner'], 'limit': 10, 'budget': {'seconds': 15, 'max_turns': 30}})
            self.assertEqual(response.status_code, 202)
            ident = response.json()['id']
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = client.get('/api/v1/discoveries/' + ident).json()
                if value['status'] not in {'queued', 'running'}: break
                time.sleep(0.01)
            self.assertEqual(value['status'], 'succeeded')
            self.assertEqual((value['returned_count'], value['missing_count']), (10, 0))
            meta = value['result']['metadata']
            self.assertEqual(meta['iteration_count'], 4)
            self.assertTrue(meta['human_review_required'])
            schema = client.get('/api/v1/openapi.json').json()
            self.assertIn('IterationMetadata', schema['components']['schemas'])
            # Exact target read back twice; persisted API response not just backend success.
            self.assertEqual(client.get('/api/v1/discoveries/' + ident).json()['result'], value['result'])


if __name__ == '__main__': unittest.main()
