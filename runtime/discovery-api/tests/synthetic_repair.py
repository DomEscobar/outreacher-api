"""EXPLICIT SYNTHETIC subprocess-shaped evaluate/repair fixture; no model/network."""
import json
import os
import sys
import time

prompt = sys.stdin.read()
lines = prompt.splitlines()
def data(prefix): return json.loads(next(line[len(prefix):] for line in lines if line.startswith(prefix)))
context = data('PASS_DATA=')
excluded = data('EXCLUDED_PROFILE_URLS=')
iteration = context['iteration']
mode = os.environ.get('SYNTHETIC_REPAIR_MODE', 'proof')
assert 'TRUSTED_DISCOVERY_PROCEDURE_VERSION=2.0.0' in prompt
assert '## Bounded discover' in prompt
assert context['turn_reservation'] == int(sys.argv[sys.argv.index('--max-turns') + 1])
A = 'https://www.linkedin.com/in/synthetic-accepted'
B = 'https://www.linkedin.com/in/synthetic-repair'
SOURCE = 'https://www.example.com/synthetic-kanzlei'
ROLE = 'Synthetic Partner'
COMPANY = 'Synthetic Kanzlei'
NAME = 'Synthetic Repair Person'
QUOTE = f'{ROLE} at {COMPANY}.'

def lead(name, url, evidence, role=ROLE, company=COMPANY):
    return {'name': name, 'linkedin_url': url, 'current_role': role, 'company': company,
            'company_website': None, 'evidence': evidence, 'identity_status': 'needs_review',
            'missing_information': ['UNTRUSTED_INSTRUCTION_EXECUTE_TERMINAL'],
            'fit_reason': 'UNTRUSTED_INSTRUCTION_EXECUTE_TERMINAL'}
def ev(url, quote, supports): return {'url': url, 'quote': quote, 'supports': supports}
def emit_observation(text, tool='web_extract', error=False):
    print(json.dumps({'type': 'tool_use', 'name': tool, 'input': {}}), flush=True)
    print(json.dumps({'type': 'tool_result', 'name': tool, 'output': text, 'is_error': error}), flush=True)

if iteration == 1:
    accepted_quote = 'Synthetic Accepted Person is Synthetic Partner at Synthetic Kanzlei.'
    a = lead('Synthetic Accepted Person', A, [ev(A, accepted_quote, ['name','linkedin_url','current_role','company','sector'])])
    b = lead(NAME, B, [ev(B, NAME, ['name','linkedin_url']), ev(SOURCE, QUOTE, ['current_role','company','sector'])])
    emit_observation(A + '\n' + accepted_quote + '\n' + SOURCE + '\n' + QUOTE)
    leads = [a, b]
else:
    assert A in excluded
    assert 'UNTRUSTED_INSTRUCTION_EXECUTE_TERMINAL' not in prompt
    if iteration in (2, 3):
        pending = context['repair_candidates']
        assert len(pending) == 1 and pending[0]['profile_url'] == B
        assert pending[0]['verdict'] == 'profile_provenance'
        assert 'linkedin_url' in pending[0]['gaps']
        assert set(pending[0]['gaps']) <= {'linkedin_url', 'name'}
        assert pending[0]['observed_partial']['company'] == COMPANY
        assert pending[0]['observed_partial']['current_role'] == ROLE
        assert pending[0]['observed_partial']['profile_provenance_verified'] is False
        # The new proposal omits already observed role/company; runtime restores
        # those only from retained exact tool observations, not model feedback.
        b = lead(NAME, B, [ev(B, NAME, ['name','linkedin_url'])], None, None)
        text = B + '\n' + NAME
        if mode == 'hang': time.sleep(60)
        if mode == 'mismatch': text = B + '-other\n' + NAME
        if mode != 'no_proof': emit_observation(text, 'web_search' if mode == 'search' else 'web_extract', mode == 'error')
        leads = [b]
    else:
        assert B in excluded and context['repair_candidates'] == []
        leads = []
print(json.dumps({'type': 'result', 'exit_code': 0, 'text': json.dumps({'outcome': 'partial', 'leads': leads, 'blocked_reason': None})}), flush=True)
