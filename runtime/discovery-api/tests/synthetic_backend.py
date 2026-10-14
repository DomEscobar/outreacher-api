"""EXPLICIT SYNTHETIC fixture: never a model call or a real lead."""
import json
import os
import sys
import time

MODE = os.environ.get('SYNTHETIC_MODE', 'valid')
# Read and check actual query-file transport (nothing in argv can contain a prompt).
query = sys.stdin.read()
if 'CRITERIA_DATA=' not in query:
    sys.exit(9)
if MODE == 'hang':
    time.sleep(60)
if MODE == 'closed_hang':
    sys.stdout.close()
    sys.stderr.close()
    os.close(1)
    os.close(2)
    time.sleep(60)
if MODE == 'flood':
    sys.stdout.write('x' * 10000)
    sys.stdout.flush()
    time.sleep(60)
if MODE == 'stderr_flood':
    sys.stderr.write('x' * 10000)
    sys.stderr.flush()
    time.sleep(60)
if MODE == 'invalid':
    print('NOT JSON')
    sys.exit(0)
if MODE == 'exit':
    sys.exit(4)

PROFILE = 'https://www.linkedin.com/in/synthetic-fixture-person'
SOURCE = 'https://www.example.com/team'
QUOTE = f'Synthetic Fixture Person is Synthetic Engineer at Synthetic Company. Profile: {PROFILE}'
LEAD = {'name': 'Synthetic Fixture Person', 'current_role': 'Synthetic Engineer',
        'company': 'Synthetic Company', 'company_website': None, 'linkedin_url': PROFILE,
        'evidence': [{'url': PROFILE, 'quote': QUOTE, 'supports': ['name', 'current_role', 'company', 'linkedin_url', 'sector']}],
        'identity_status': 'verified', 'missing_information': [], 'fit_reason': 'Synthetic fixture only'}
print(json.dumps({'type': 'tool_result', 'name': 'web_extract', 'output': PROFILE + '\n' + QUOTE, 'is_error': False}))
print(json.dumps({'type': 'text', 'text': 'not the final result'}))
print(json.dumps({'type': 'result', 'exit_code': 0,
                  'text': json.dumps({'outcome': 'completed', 'leads': [LEAD]}),
                  'tokens': {'input': 0, 'output': 0, 'total': 0, 'cache_read': 0, 'cache_write': 0},
                  'duration_ms': 0, 'session_id': 'explicit-synthetic-fixture'}))
