#!/usr/bin/env python3
"""Fixed evaluation set for source="search": real runs, a review sheet for a human, precision per tier.

  python3 eval_search.py plan  [--case NAME ...] [--limit N] [--max-cost USD]
  python3 eval_search.py run   [--out DIR] [--case NAME ...] [--limit N] [--max-cost USD] [--max-total-usd USD]
                               [--repeat N]
  python3 eval_search.py score RUN_DIR

`--repeat N` runs every case N times in a row with a search memory in RUN_DIR (search_memory.py) and
each repeat excluding the leads delivered by the earlier ones, as repeat API searches do: it measures
how much a repeat search still finds and what the memory saves.

`plan` only prints the cases and the maximum spend. `run` makes PAID provider calls (treg + OpenRouter,
each case capped by its budget.max_cost_usd) and writes leads.jsonl, review.csv and summary.json into a
new RUN_DIR. Fill the check columns of review.csv (y / n, empty or ? = not checked), then `score` reports
precision per verification tier and per discovery strategy, yield and cost per qualified lead.

Lead rows are personal data: they go to a private directory (0700/0600, default in the data volume),
never into the source tree, Brain or a public place. Runs bypass the job store and the shared registry:
nothing is recorded as delivered and earlier deliveries are not excluded, so runs stay comparable.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from pathlib import Path

from models import Criteria
from search_backend import SearchBackend, SearchError
from search_memory import SearchMemory

CASES = Path(__file__).with_name('eval_cases.json')
DEFAULT_OUT = Path(os.environ.get('DISCOVERY_EVAL_DIR', '/opt/data/discovery-api/eval'))
CHECKS = ('person_ok', 'role_ok', 'sector_ok', 'location_ok')
COLUMNS = ('case', 'run', 'rank', 'counts', 'verification', 'identity_status', 'strategy', 'name', 'current_role', 'company',
           'company_website', 'linkedin_url', 'evidence_urls') + CHECKS + ('notes',)
STATS = ('elapsed_seconds', 'stop_reason', 'verification_counts', 'qualified_by_strategy', 'provider_errors', 'candidate_count',
         'queries', 'people_queries', 'stale_rejected', 'provider_calls', 'llm_calls', 'regions', 'expanded_facets',
         'skipped_known', 'demoted_queries', 'excluded_known_company', 'call_seconds')
YES, NO = {'y', 'j', 'ja', 'yes', '1', 'x'}, {'n', 'nein', 'no', '0'}


def load_cases(path=CASES, only=(), limit=None, max_cost=None):
    """[(name, Criteria)] from the cases file; defaults first, CLI overrides last."""
    spec = json.loads(Path(path).read_text(encoding='utf-8'))
    defaults = {k: v for k, v in spec.get('defaults', {}).items() if not k.startswith('_')}
    cases = []
    for case in spec['cases']:
        name = case['name']
        if only and name not in only: continue
        fields = {**defaults, **{k: v for k, v in case.items() if k != 'name' and not k.startswith('_')}}
        fields.update(source='search', exclude_previous=False)
        if limit: fields['limit'] = limit
        if max_cost: fields['budget'] = {**fields.get('budget', {}), 'max_cost_usd': max_cost}
        cases.append((name, Criteria.model_validate(fields)))
    if only and len(cases) != len(set(only)):
        raise SystemExit('unknown case: ' + ', '.join(sorted(set(only) - {n for n, _ in cases})))
    return cases


def strategy_of(lead):
    """Where the lead was found, read from its evidence: people index, Google snippet or firm imprint first."""
    sources = [(e.get('source'), set(e.get('supports') or ())) for e in lead.get('evidence') or []]
    if any(s == 'people_index' for s, _ in sources): return 'people_search'
    if any(s == 'search_snippet' and 'current_role' in sup for s, sup in sources): return 'linkedin_search'
    return 'firm_websites'


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f: f.write(text)
    os.chmod(path, 0o600)


def run_cases(cases, out_root, backend=None, max_total_usd=None, log=print, repeat=1):
    """Run every case `repeat` times; returns the new run directory. A missing configuration stops all cases."""
    backend = backend or SearchBackend()
    run_dir = private_dir(Path(out_root) / time.strftime('%Y%m%d-%H%M%S'))
    memory = SearchMemory(private_dir(run_dir / 'memory')) if repeat > 1 else None
    leads, summary, spent, stop = [], [], 0.0, False
    for name, criteria in cases:
        delivered = set()  # earlier repeats' leads, excluded like an API search's previous deliveries
        for run in range(1, repeat + 1):
            if stop: break
            if max_total_usd is not None and spent >= max_total_usd:
                summary.append({'case': name, 'run': run, 'status': 'skipped', 'error': 'max_total_usd reached'})
                continue
            log(f'{name} run {run}: running (limit {criteria.limit}, max ${criteria.budget.max_cost_usd})', file=sys.stderr)
            try:
                qualified, review, stats = backend.run(criteria, set(delivered), lambda: False, None,
                                                       memory.view(criteria) if memory else None)
            except SearchError as exc:
                summary.append({'case': name, 'run': run, 'status': 'failed', 'error': exc.code,
                                **({'provider_errors': exc.provider_errors} if exc.provider_errors else {})})
                stop = exc.code == 'search_not_configured'
                continue
            spent += stats['cost_usd']
            delivered |= {l['linkedin_url'] for l in qualified}
            for rank, lead in enumerate(qualified + review, 1):
                leads.append({'case': name, 'run': run, 'rank': rank, 'counts': rank <= len(qualified),
                              'strategy': strategy_of(lead), **lead})
            summary.append({'case': name, 'run': run, 'status': 'ok', 'requested': criteria.limit,
                            'qualified': len(qualified), 'review_returned': len(review), 'cost_usd': stats['cost_usd'],
                            'cost_per_qualified_usd': round(stats['cost_usd'] / len(qualified), 4) if qualified else None,
                            **{k: stats.get(k) for k in STATS}})
    write_private(run_dir / 'leads.jsonl', ''.join(json.dumps(l, ensure_ascii=False) + '\n' for l in leads))
    write_private(run_dir / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=1) + '\n')
    sheet = io.StringIO()
    writer = csv.DictWriter(sheet, COLUMNS, delimiter=';', extrasaction='ignore')
    writer.writeheader()
    for lead in leads:
        writer.writerow({**lead, 'counts': 'ja' if lead['counts'] else 'nein',
                         'evidence_urls': ' '.join(dict.fromkeys(e['url'] for e in lead['evidence']))})
    write_private(run_dir / 'review.csv', '﻿' + sheet.getvalue())  # BOM: umlauts survive Excel
    return run_dir


def label(value):
    v = (value or '').strip().casefold()
    return True if v in YES else False if v in NO else None


def precision(rows):
    """Correct = person, role and sector confirmed and the place not contradicted; unchecked rows are left out."""
    verdicts = []
    for r in rows:
        checks = {c: label(r.get(c)) for c in CHECKS}
        if any(v is False for v in checks.values()): verdicts.append(False)
        elif all(checks[c] for c in CHECKS[:3]): verdicts.append(True)
    return {'checked': len(verdicts), 'correct': sum(verdicts), 'unchecked': len(rows) - len(verdicts),
            'precision': round(sum(verdicts) / len(verdicts), 3) if verdicts else None}


def score(run_dir):
    run_dir = Path(run_dir)
    with open(run_dir / 'review.csv', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f, delimiter=';'))
    summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
    ok = [s for s in summary if s.get('status') == 'ok']
    group = lambda key: {v: precision([r for r in rows if r[key] == v]) for v in dict.fromkeys(r[key] for r in rows)}
    qualified, cost = sum(s['qualified'] for s in ok), sum(s['cost_usd'] for s in ok)
    result = {'counting_leads': precision([r for r in rows if r['counts'] == 'ja']),
              'by_verification': group('verification'), 'by_strategy': group('strategy'), 'by_case': group('case'),
              'yield': {'cases': len(ok), 'failed_cases': [s['case'] for s in summary if s.get('status') != 'ok'],
                        'requested': sum(s['requested'] for s in ok), 'qualified': qualified,
                        'cost_usd': round(cost, 4), 'cost_per_qualified_usd': round(cost / qualified, 4) if qualified else None,
                        'seconds': round(sum(s.get('elapsed_seconds') or 0 for s in ok), 1)}}
    write_private(run_dir / 'score.json', json.dumps(result, ensure_ascii=False, indent=1) + '\n')
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('plan', 'run'):
        p = sub.add_parser(name)
        p.add_argument('--cases', type=Path, default=CASES)
        p.add_argument('--case', action='append', default=[], help='only this case (repeatable)')
        p.add_argument('--limit', type=int, help='override every case limit')
        p.add_argument('--max-cost', type=float, help='override every case budget.max_cost_usd')
        if name == 'run':
            p.add_argument('--out', type=Path, default=DEFAULT_OUT)
            p.add_argument('--max-total-usd', type=float, help='skip remaining cases once this much is spent')
            p.add_argument('--repeat', type=int, default=1, help='run every case N times with a search memory')
    sub.add_parser('score').add_argument('run_dir', type=Path)
    a = ap.parse_args(argv)
    if a.command == 'score':
        print(json.dumps(score(a.run_dir), ensure_ascii=False, indent=1))
        return 0
    cases = load_cases(a.cases, a.case, a.limit, a.max_cost)
    if a.command == 'plan':
        for name, c in cases:
            print(f'{name}: {c.keywords} {c.titles} {c.locations} limit={c.limit} max_cost=${c.budget.max_cost_usd}')
        print(f'maximum spend: ${sum(c.budget.max_cost_usd for _, c in cases):.2f} (each case stops at its cap)')
        return 0
    run_dir = run_cases(cases, a.out, max_total_usd=a.max_total_usd, log=print, repeat=a.repeat)
    print(json.dumps(json.loads((run_dir / 'summary.json').read_text(encoding='utf-8')), ensure_ascii=False, indent=1))
    print(f'review sheet: {run_dir / "review.csv"}  (fill {", ".join(CHECKS)}, then: score {run_dir})', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
