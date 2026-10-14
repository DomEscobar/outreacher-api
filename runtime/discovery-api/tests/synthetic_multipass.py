"""EXPLICIT SYNTHETIC subprocess fixture; no network, browser or model."""
import json
import os
import sys
import time

lines = sys.stdin.read().splitlines()
def data(prefix): return json.loads(next(line[len(prefix):] for line in lines if line.startswith(prefix)))
criteria = data('CRITERIA_DATA=')
context = data('PASS_DATA=')
excluded = data('EXCLUDED_PROFILE_URLS=')
iteration = context['iteration']
mode = os.environ.get('SYNTHETIC_MULTIPASS_MODE', 'valid')
assert int(os.environ['DISCOVERY_TOOL_BUDGET']) == int(sys.argv[sys.argv.index('--max-turns') + 1])
assert data('TARGET_URL=') == (criteria.get('search_url') or None) or not criteria.get('search_url')
if iteration > 1 and mode != 'empty':
    assert 'https://www.linkedin.com/in/synthetic-pass-1' in excluded
if mode == 'delay': time.sleep(0.12)
if iteration == 2:
    if mode == 'hang': time.sleep(60)
    if mode == 'invalid': print('SYNTHETIC RAW SECRET'); sys.exit(0)
    if mode == 'exit': sys.exit(7)
    if mode == 'schema':
        print(json.dumps({'type': 'result', 'exit_code': 0, 'text': 'SYNTHETIC RAW SECRET {}'}))
        sys.exit(0)
    if mode == 'flood':
        sys.stderr.write('SYNTHETIC SECRET' * 10000); sys.stderr.flush(); time.sleep(60)
if mode == 'padding':
    sys.stderr.write('x' * 2000); sys.stderr.flush()
leads = []
for index in range(context['batch_size']):
    number = index + 1 if mode == 'duplicates' else (iteration - 1) * 3 + index + 1
    name = f'Synthetic Fixture Person {number}'
    url = f'https://www.linkedin.com/in/synthetic-pass-{number}'
    role = 'Synthetic Rechtsanwalt' if mode == 'unqualified' else 'Synthetic Partner'
    company = 'Synthetic Kanzlei' if mode == 'unqualified' else 'Synthetic Company'
    quote = f'{name} is {role} at {company}.'
    leads.append({'name': name, 'current_role': role, 'company': company,
                  'company_website': None, 'linkedin_url': url, 'identity_status': 'needs_review',
                  'evidence': [{'url': url, 'quote': quote, 'supports': ['name', 'linkedin_url', 'current_role', 'company', 'sector']}],
                  'missing_information': [], 'fit_reason': 'Synthetic only'})
    if mode == 'unqualified' and number == 4:
        leads[-1]['company_website'] = 'http://localhost/invalid-synthetic-optional'
if mode == 'empty': leads = []
observed = '\n'.join(p['linkedin_url'] + '\n' + p['evidence'][0]['quote'] for p in leads)
print(json.dumps({'type': 'tool_use', 'name': 'web_extract', 'arguments': {}}), flush=True)
if mode == 'too_many_tools':
    for _ in range(31): print(json.dumps({'type': 'tool_use', 'name': 'web_extract', 'arguments': {}}), flush=True)
print(json.dumps({'type': 'tool_result', 'name': 'web_extract', 'output': observed}), flush=True)
blocked = mode == 'blocked' and iteration == 2
if blocked: leads = []
print(json.dumps({'type': 'result', 'exit_code': 0, 'text': json.dumps({
    'outcome': 'blocked' if blocked else 'completed', 'leads': leads,
    'blocked_reason': 'access_limit' if blocked else None})}), flush=True)
