"""eval_search.py: EXPLICIT SYNTHETIC fixtures only (FakeProviders from test_search); no network, no real people."""
import csv
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import eval_search as ev
import search_backend as sb
from test_search import CLARA, PEOPLE, FakeProviders

CASES = {'defaults': {'limit': 3, 'include_review': True, 'budget': {'seconds': 60, 'max_cost_usd': 0.3}},
         'cases': [{'name': 'kanzlei-synthetic', 'keywords': ['Rechtsanwalt'], 'titles': ['Partner', 'Inhaber'],
                    'locations': ['München'], '_note': 'ignored'},
                   {'name': 'second-synthetic', 'keywords': ['Rechtsanwalt'], 'locations': ['München']}]}
quiet = lambda *a, **k: None


def write_cases(tmp, spec=CASES):
    path = Path(tmp) / 'cases.json'
    path.write_text(json.dumps(spec), encoding='utf-8')
    return path


class EvalTests(unittest.TestCase):
    def test_shipped_cases_are_valid(self):
        cases = ev.load_cases()
        self.assertEqual(len(cases), 6)  # 3 sectors x 2 cities
        self.assertTrue(all(c.source == 'search' and not c.exclude_previous for _, c in cases))
        self.assertLessEqual(sum(c.budget.max_cost_usd for _, c in cases), 3.0)

    def test_overrides_and_unknown_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = ev.load_cases(write_cases(tmp), only=['second-synthetic'], limit=7, max_cost=0.05)
            self.assertEqual([(n, c.limit, c.budget.max_cost_usd, c.budget.seconds) for n, c in cases],
                             [('second-synthetic', 7, 0.05, 60)])
            with self.assertRaises(SystemExit): ev.load_cases(write_cases(tmp), only=['missing'])

    def test_run_writes_private_sheet_and_score_reports_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = sb.SearchBackend(lambda meter: FakeProviders(meter, people=[CLARA], **PEOPLE))
            run_dir = ev.run_cases(ev.load_cases(write_cases(tmp)), Path(tmp) / 'out', backend, log=quiet)
            for name in ('leads.jsonl', 'review.csv', 'summary.json'):
                self.assertTrue((run_dir / name).exists())
                if os.name == 'posix': self.assertEqual(stat.S_IMODE(os.stat(run_dir / name).st_mode), 0o600)
            if os.name == 'posix': self.assertEqual(stat.S_IMODE(os.stat(run_dir).st_mode), 0o700)
            summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual([(s['case'], s['status'], s['qualified']) for s in summary],
                             [('kanzlei-synthetic', 'ok', 3), ('second-synthetic', 'ok', 3)])
            self.assertEqual(summary[0]['qualified_by_strategy'], {'linkedin_search': 2, 'people_search': 1})
            self.assertEqual(summary[0]['cost_per_qualified_usd'], round(summary[0]['cost_usd'] / 3, 4))
            with open(run_dir / 'review.csv', encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f, delimiter=';'))
            self.assertEqual(tuple(rows[0]), ev.COLUMNS)
            first = [r for r in rows if r['case'] == 'kanzlei-synthetic']
            self.assertEqual([(r['name'], r['verification'], r['strategy'], r['counts']) for r in first],
                             [('Erika Musterfrau', 'verified', 'linkedin_search', 'ja'),
                              ('Clara Index', 'verified', 'people_search', 'ja'),
                              ('Max Beispiel', 'plausible', 'linkedin_search', 'ja'),
                              ('Otto Probe', 'review', 'linkedin_search', 'nein')])
            # A reviewer fills the sheet (Excel keeps ';' and the BOM): Max is wrong, Otto stays unchecked.
            marks = {'Clara Index': 'y', 'Erika Musterfrau': 'ja', 'Max Beispiel': 'n'}
            for r in rows:
                if r['name'] in marks: r.update({c: marks[r['name']] for c in ev.CHECKS})
            with open(run_dir / 'review.csv', 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, ev.COLUMNS, delimiter=';')
                w.writeheader()
                w.writerows(rows)
            result = ev.score(run_dir)
            self.assertEqual(result['counting_leads'], {'checked': 6, 'correct': 4, 'unchecked': 0, 'precision': 0.667})
            self.assertEqual(result['by_verification']['verified']['precision'], 1.0)
            self.assertEqual(result['by_verification']['plausible']['precision'], 0.0)
            self.assertEqual(result['by_verification']['review'], {'checked': 0, 'correct': 0, 'unchecked': 2, 'precision': None})
            self.assertEqual(result['by_strategy']['people_search']['precision'], 1.0)
            self.assertEqual((result['yield']['requested'], result['yield']['qualified']), (6, 6))
            self.assertTrue((run_dir / 'score.json').exists())

    def test_location_contradiction_counts_as_wrong(self):
        rows = [{'person_ok': 'y', 'role_ok': 'y', 'sector_ok': 'y', 'location_ok': 'n'},
                {'person_ok': 'y', 'role_ok': 'y', 'sector_ok': 'y', 'location_ok': ''},
                {'person_ok': 'y', 'role_ok': '?', 'sector_ok': 'y', 'location_ok': 'y'}]
        self.assertEqual(ev.precision(rows), {'checked': 2, 'correct': 1, 'unchecked': 1, 'precision': 0.5})

    def test_missing_configuration_stops_all_cases(self):
        def factory(meter): raise sb.SearchError('search_not_configured')
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = ev.run_cases(ev.load_cases(write_cases(tmp)), Path(tmp) / 'out', sb.SearchBackend(factory), log=quiet)
            summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary, [{'case': 'kanzlei-synthetic', 'run': 1, 'status': 'failed', 'error': 'search_not_configured'}])

    def test_repeat_runs_exclude_earlier_leads_and_use_the_memory(self):
        spec = {'defaults': {'limit': 1, 'expand_terms': False, 'budget': {'seconds': 60, 'max_cost_usd': 0.3}},
                'cases': [{'name': 'kanzlei-synthetic', 'keywords': ['Rechtsanwalt'], 'titles': ['Partner', 'Inhaber'],
                           'locations': ['München']}]}
        with tempfile.TemporaryDirectory() as tmp:
            backend = sb.SearchBackend(lambda meter: FakeProviders(meter))
            run_dir = ev.run_cases(ev.load_cases(write_cases(tmp, spec)), Path(tmp) / 'out', backend, log=quiet, repeat=2)
            summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual([(s['run'], s['qualified'], s['skipped_known']) for s in summary], [(1, 1, 0), (2, 1, 1)])
            leads = [json.loads(l) for l in (run_dir / 'leads.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([(l['run'], l['name']) for l in leads], [(1, 'Erika Musterfrau'), (2, 'Max Beispiel')])
            self.assertTrue((run_dir / 'memory' / 'search_memory.sqlite3').exists())

    def test_total_budget_skips_remaining_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = sb.SearchBackend(lambda meter: FakeProviders(meter, cost_micro=50_000))
            run_dir = ev.run_cases(ev.load_cases(write_cases(tmp)), Path(tmp) / 'out', backend, max_total_usd=0.01, log=quiet)
            summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual([s['status'] for s in summary], ['ok', 'skipped'])


if __name__ == '__main__':
    unittest.main()
