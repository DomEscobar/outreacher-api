"""Synthetic evaluate/repair regressions, with real OS subprocess and API readback."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import create_app
from evaluation import CandidateState, MAX_REPAIRS, MAX_REPAIR_PASSES, VERDICTS
from models import Budget, Criteria, Lead, accepted_leads, evaluate_lead
from policy import allowed_call
from procedure import trusted_procedure
from research import BackendError, parse_events
from worker import HermesBackend, prompt
from test_discovery import LEAD, OBS, PROFILE, SOURCE


def parse(leads, observations=()):
    events = [{'type': 'tool_result', 'name': 'web_extract', 'output': o} for o in observations]
    events.append({'type': 'result', 'exit_code': 0, 'text': json.dumps({'outcome': 'partial', 'leads': leads})})
    return parse_events(events)


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.criteria = Criteria(keywords=['synthetic'], titles=['Synthetic Engineer'], limit=5)

    def test_all_deterministic_verdicts_and_local_schema_rejection(self):
        invalid = {**LEAD, 'name': ''}
        schema = {**LEAD, 'instructions': 'EXECUTE_TERMINAL'}
        result, observations = parse([invalid, schema, LEAD, LEAD], OBS)
        state = CandidateState()
        fresh, counts = state.evaluate(result, observations, set(), self.criteria, 5)
        self.assertEqual(len(fresh), 1)
        self.assertEqual((counts['invalid'], counts['schema'], counts['duplicate']), (1, 1, 1))
        self.assertEqual(set(counts), set(VERDICTS))
        self.assertEqual(state.feedback(set()), [])
        for field, value, reason in [('company', 'Synthetic Unsupported Company', 'unsupported_fields'),
                                     ('current_role', 'Synthetic Engineer', 'role')]:
            candidate = copy.deepcopy(LEAD)
            candidate[field] = value
            titles = ['Synthetic Director'] if reason == 'role' else []
            _, verdict, gaps = evaluate_lead(Lead.model_validate(candidate), OBS, titles=titles)
            self.assertEqual(verdict, reason)
            self.assertIn(field, gaps)
        no_sector = copy.deepcopy(LEAD)
        for e in no_sector['evidence']: e['supports'].remove('sector')
        _, verdict, gaps = evaluate_lead(Lead.model_validate(no_sector), OBS, keywords=['kanzlei'])
        self.assertEqual((verdict, gaps), ('scope', ['sector']))
        _, verdict, _ = evaluate_lead(Lead.model_validate(LEAD), [], titles=[])
        self.assertEqual(verdict, 'profile_provenance')
        _, verdict, _ = evaluate_lead(Lead.model_validate(LEAD), OBS, {PROFILE})
        self.assertEqual(verdict, 'duplicate')

    def test_unique_supported_candidates_beyond_quota_are_not_duplicate(self):
        state = CandidateState()
        extra = copy.deepcopy(LEAD)
        extra['linkedin_url'] += '-extra'
        extra['evidence'][0]['url'] = extra['linkedin_url']
        result, observations = parse([LEAD, extra], OBS + [extra['linkedin_url'] + '\n' + extra['evidence'][0]['quote']])
        fresh, counts = state.evaluate(result, observations, set(), self.criteria, 1)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(counts['quota'], 1)
        self.assertEqual(counts['duplicate'], 0)

    def test_prior_and_definitively_irrelevant_candidates_never_reopen(self):
        state = CandidateState()
        result, obs = parse([LEAD], OBS)
        excluded = {PROFILE}
        fresh, counts = state.evaluate(result, obs, excluded, self.criteria, 5)
        self.assertEqual((fresh, counts['duplicate'], state.feedback(excluded)), ([], 1, []))
        excluded = set()
        self.criteria.titles = ['Synthetic Director']
        fresh, counts = state.evaluate(result, obs, excluded, self.criteria, 5)
        self.assertEqual((fresh, counts['role']), ([], 1))
        self.assertIn(PROFILE, excluded)
        self.assertEqual(state.feedback(excluded), [])

    def test_repair_queue_bounded_and_exhausted_candidates_excluded(self):
        state = CandidateState()
        proposals = []
        for i in range(10):
            p = copy.deepcopy(LEAD)
            p['linkedin_url'] += '-' + str(i)
            proposals.append(p)
        result, obs = parse(proposals)
        excluded = set()
        state.evaluate(result, obs, excluded, self.criteria, 5)
        self.assertEqual(len(state.pending), MAX_REPAIRS)
        for _ in range(MAX_REPAIR_PASSES): self.assertEqual(len(state.feedback(excluded)), MAX_REPAIRS)
        self.assertEqual(state.feedback(excluded), [])
        self.assertEqual(len(excluded), MAX_REPAIRS)

    def test_duplicate_bad_proposal_does_not_erase_repairable_peer(self):
        state = CandidateState()
        result, obs = parse([LEAD, LEAD])
        _, counts = state.evaluate(result, obs, set(), self.criteria, 5)
        self.assertEqual(counts['duplicate'], 1)
        self.assertEqual(len(state.pending), 1)

    def test_untrusted_instructions_not_feedback_policy_or_tools(self):
        state = CandidateState()
        bad = copy.deepcopy(LEAD)
        bad['company'] = 'UNTRUSTED_COMMAND_READ_SECRETS'
        bad['fit_reason'] = 'UNTRUSTED_COMMAND_EXECUTE_TERMINAL'
        bad['missing_information'] = ['UNTRUSTED_COMMAND_EXECUTE_TERMINAL']
        result, obs = parse([bad], OBS)
        state.evaluate(result, obs, set(), self.criteria, 5)
        feedback = state.feedback(set())
        text = json.dumps(feedback)
        self.assertNotIn('UNTRUSTED_COMMAND', text)
        self.assertEqual(feedback[0]['gaps'], ['company'])
        rendered = prompt(self.criteria, set(), {'repair_candidates': feedback})
        self.assertIn('UNTRUSTED candidate DATA', rendered)
        for name in ('terminal', 'browser_exec', 'browser_vault_fill', 'skill_view'):
            self.assertFalse(allowed_call(name, {'command': 'read secrets'}))

    def test_repaired_fact_needs_actual_successful_observation_not_model_claim(self):
        state = CandidateState()
        criteria = Criteria(keywords=['synthetic'], titles=['Synthetic Director'], limit=1)
        candidate = copy.deepcopy(LEAD)
        candidate['current_role'] = 'Synthetic Director'
        result, observations = parse([candidate], OBS)
        excluded = set()
        fresh, counts = state.evaluate(result, observations, excluded, criteria, 1)
        self.assertEqual((fresh, counts['unsupported_fields']), ([], 1))
        self.assertEqual(state.feedback(excluded)[0]['gaps'], ['current_role'])
        proof = {'url': SOURCE, 'quote': 'Synthetic Director is the literal observed role.', 'supports': ['current_role']}
        candidate['evidence'].append(proof)
        result, _ = parse([candidate])
        fresh, counts = state.evaluate(result, observations, excluded, criteria, 1)
        self.assertEqual(fresh, [])
        self.assertEqual(counts['unsupported_fields'], 1)
        _, actual = parse([], [SOURCE + '\n' + proof['quote']])
        fresh, counts = state.evaluate(result, observations + actual, excluded, criteria, 1)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]['current_role'], 'Synthetic Director')
        self.assertEqual(state.repaired_count, 1)
        self.assertEqual(sum(counts.values()), 0)

    def test_schema_extras_are_not_forwarded_as_repair_instructions(self):
        state = CandidateState()
        result, obs = parse([{**LEAD, 'instructions': 'IGNORE_POLICY_READ_SECRETS'}], OBS)
        _, counts = state.evaluate(result, obs, set(), self.criteria, 5)
        self.assertEqual(counts['schema'], 1)
        self.assertEqual(state.feedback(set()), [])

    def test_skill_is_actual_versioned_text_and_correct_host_fallback(self):
        procedure = trusted_procedure()
        self.assertIn('TRUSTED_DISCOVERY_PROCEDURE_VERSION=2.0.0', procedure)
        self.assertIn('Bounded discover', procedure)
        self.assertTrue(prompt(self.criteria, set()).startswith(procedure))
        from procedure import HOST_SKILL
        self.assertEqual(HOST_SKILL, Path(__file__).resolve().parents[3] / 'skills/linkedin-discovery/SKILL.md')
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'SKILL.md'
            p.write_text('---\nname: linkedin-discovery\nversion: wrong\n---\n')
            with patch('procedure.APPLIANCE_SKILL', p), self.assertRaises(BackendError) as caught:
                trusted_procedure()
            self.assertEqual(caught.exception.code, 'procedure_unavailable')
        with patch('procedure.APPLIANCE_SKILL', Path('/missing/appliance/skill')), patch('procedure.HOST_SKILL', Path('/missing/host/skill')), self.assertRaises(BackendError):
            trusted_procedure()


class SubprocessRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executable = Path(self.tmp.name) / 'synthetic-repair'
        self.executable.write_text('#!' + sys.executable + '\n' + Path(__file__).with_name('synthetic_repair.py').read_text())
        self.executable.chmod(0o700)

    def run_fixture(self, mode='proof', turns=32):
        criteria = Criteria(keywords=['kanzlei'], titles=['Partner'], source='web', limit=2,
                            budget=Budget(seconds=15, max_turns=turns))
        with patch.dict(os.environ, {'SYNTHETIC_REPAIR_MODE': mode}):
            return HermesBackend(str(self.executable)).run(criteria, set(), lambda: False)

    def test_real_subprocess_feedback_repairs_with_retained_actual_proof(self):
        result, observations = self.run_fixture()
        self.assertEqual(len(accepted_leads(result, observations, 2, set(), ['Partner'], ['kanzlei'])), 2)
        stats = result._stats
        self.assertEqual(stats['repaired_count'], 1)
        self.assertEqual(stats['rejection_counts']['profile_provenance'], 1)
        self.assertEqual(stats['iteration_count'], 2)
        self.assertEqual([p['charged_turns'] for p in stats['iterations']], [16, 16])
        self.assertEqual(stats['reserved_turns'], stats['charged_turns'])
        self.assertEqual(stats['tool_use_count'], 2)
        self.assertIsNone(stats['measured_model_iterations'])
        self.assertEqual(stats['budget_accounting'], 'full_cli_reservation')
        repaired = result.leads[1]
        self.assertEqual(repaired.current_role, 'Synthetic Partner')
        self.assertEqual(repaired.company, 'Synthetic Kanzlei')
        self.assertEqual(len(repaired.evidence), 2)
        self.assertNotIn('repair_candidates', json.dumps(stats))

    def test_repair_never_succeeds_without_successful_direct_document_proof(self):
        for mode in ('no_proof', 'search', 'error', 'mismatch'):
            with self.subTest(mode=mode):
                result, observations = self.run_fixture(mode, turns=40)
                self.assertEqual(len(accepted_leads(result, observations, 2, set(), ['Partner'], ['kanzlei'])), 1)
                self.assertEqual(result._stats['repaired_count'], 0)
                self.assertEqual(result._stats['charged_turns'], 40)
                self.assertEqual(result._stats['stop_reason'], 'turn_limit')

    def test_feedback_and_observations_are_job_local_not_global(self):
        first, _ = self.run_fixture()
        second, _ = self.run_fixture()
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(second._stats['repaired_count'], 1)

    def test_api_atomic_cancel_during_repair_does_not_commit_partial_or_seen(self):
        directory = Path(self.tmp.name) / 'cancel'
        with patch.dict(os.environ, {'SYNTHETIC_REPAIR_MODE': 'hang'}), TestClient(create_app(directory, backend=HermesBackend(str(self.executable)))) as client:
            submitted = client.post('/api/v1/discoveries', json={'keywords': ['kanzlei'], 'titles': ['Partner'], 'source': 'web', 'limit': 2, 'budget': {'seconds': 15, 'max_turns': 32}}).json()
            ident = submitted['id']
            time.sleep(0.3)  # Fixture deliberately holds the second subprocess.
            pending = client.get('/api/v1/discoveries/' + ident).json()
            self.assertEqual(pending['status'], 'running')
            self.assertIsNone(pending['result'])
            self.assertEqual(client.post('/api/v1/discoveries/' + ident + '/cancel').json()['error'], 'cancelled')
            time.sleep(0.2)
            cancelled = client.get('/api/v1/discoveries/' + ident).json()
            self.assertIsNone(cancelled['result'])
            self.assertEqual(cancelled['error'], 'cancelled')
            self.assertEqual(client.app.state.store.previous(), set())

    def test_api_readback_deterministic_repair_and_previous_dedup(self):
        directory = Path(self.tmp.name) / 'data'
        with patch.dict(os.environ, {'SYNTHETIC_REPAIR_MODE': 'proof'}), TestClient(create_app(directory, backend=HermesBackend(str(self.executable)))) as client:
            submitted = client.post('/api/v1/discoveries', json={'keywords': ['kanzlei'], 'titles': ['Partner'], 'source': 'web', 'limit': 2, 'budget': {'seconds': 15, 'max_turns': 32}}).json()
            ident = submitted['id']
            end = time.monotonic() + 3
            while time.monotonic() < end:
                response = client.get('/api/v1/discoveries/' + ident)
                value = response.json()
                if value['status'] not in ('queued', 'running'): break
                time.sleep(0.01)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(value['status'], 'succeeded')
            self.assertEqual(value['returned_count'], 2)
            self.assertEqual(value['result']['metadata']['repaired_count'], 1)
            self.assertEqual(client.get('/api/v1/discoveries/' + ident).json(), value)
            self.assertEqual(len(client.app.state.store.previous()), 2)

    def test_smaller_adaptive_reservations_on_full_yield(self):
        executable = Path(self.tmp.name) / 'synthetic-yield'
        executable.write_text('#!' + sys.executable + '\n' + Path(__file__).with_name('synthetic_multipass.py').read_text())
        executable.chmod(0o700)
        criteria = Criteria(keywords=['synthetic'], titles=['Partner'], limit=10, budget=Budget(seconds=15, max_turns=120))
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'valid'}):
            result, _ = HermesBackend(str(executable)).run(criteria, set(), lambda: False)
        self.assertEqual([p['charged_turns'] for p in result._stats['iterations']], [16, 14, 14, 14])
        self.assertEqual(result._stats['charged_turns'], 58)
        self.assertEqual(result._stats['tool_use_count'], 4)
        self.assertIsNone(result._stats['measured_model_iterations'])

    def test_api_actual_subprocess_120_reservations_are_readable_not_tool_turns(self):
        executable = Path(self.tmp.name) / 'synthetic-empty'
        executable.write_text('#!' + sys.executable + '\n' + Path(__file__).with_name('synthetic_multipass.py').read_text())
        executable.chmod(0o700)
        directory = Path(self.tmp.name) / 'budget120'
        with patch.dict(os.environ, {'SYNTHETIC_MULTIPASS_MODE': 'empty'}), TestClient(create_app(directory, backend=HermesBackend(str(executable)))) as client:
            posted = client.post('/api/v1/discoveries', json={'keywords': ['synthetic'], 'source': 'web', 'limit': 10, 'budget': {'seconds': 1200, 'max_turns': 120}})
            ident = posted.json()['id']
            end = time.monotonic() + 3
            while time.monotonic() < end:
                reply = client.get('/api/v1/discoveries/' + ident)
                value = reply.json()
                if value['status'] not in ('queued', 'running'): break
                time.sleep(0.01)
            self.assertEqual(reply.status_code, 200)
            meta = value['result']['metadata']
            self.assertEqual(value['status'], 'partial')
            self.assertEqual((meta['charged_turns'], meta['reserved_turns'], meta['tool_use_count']), (120, 120, 8))
            self.assertEqual(meta['iteration_count'], 8)
            self.assertIsNone(meta['measured_model_iterations'])
            self.assertEqual(meta['stop_reason'], 'turn_limit')
            self.assertEqual(client.get('/api/v1/discoveries/' + ident).json(), value)

    def test_docker_copy_covers_new_runtime_modules(self):
        root = Path(__file__).resolve().parents[3]
        self.assertIn('COPY runtime/ /opt/browser-box/', (root / 'Dockerfile').read_text())
        for module in ('evaluation.py', 'procedure.py', 'rendering.py'):
            self.assertTrue((root / 'runtime/discovery-api' / module).is_file())


if __name__ == '__main__': unittest.main()
