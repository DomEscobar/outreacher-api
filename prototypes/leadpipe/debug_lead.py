#!/usr/bin/env python3
"""Explain the variant C decision for single leads (reads leads JSONL on stdin, names as args)."""
import json
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

import leadpipe as lp

wanted = [a.casefold() for a in sys.argv[1:]]
for line in sys.stdin:
    lead = json.loads(line)
    if not any(w in lead['name'].casefold() for w in wanted): continue
    print('=' * 20, lead['name'], '|', lead.get('company'))
    name = ' '.join(t.capitalize() for t in lp.name_key(lead['name']))
    firm = lp.clean_company(lead.get('company'))
    for q in ([f'"{name}" {firm}'] if firm else []) + [f'"{name}" Rechtsanwalt München']:
        print('  query:', q)
        for r in lp.fc_search(q, 8):
            print(f"    related={lp.host_related(lead, r.get('url') or '')} {r.get('url')}")
    website, pages = lp.firm_pages(lead, 'München')
    print('  chosen:', website, [len(t) for _, t in pages])
    if pages:
        captured = {}
        real = lp.llm_json
        def spy(system, user):
            captured['o'] = real(system, user); return captured['o']
        with patch.object(lp, 'llm_json', spy), patch.object(lp, 'firm_pages', return_value=(website, pages)):
            r = lp.confirm(lead, 'München', ['Rechtsanwalt', 'Rechtsanwältin'])
        corpus = '\n\n'.join(t for _, t in pages)
        o = captured['o']
        print('  llm:', {k: o.get(k) for k in ('person_on_site', 'role_on_site', 'quote_role', 'firm_in_sector', 'quote_sector', 'firm_name', 'listed_as_non_partner')})
        print('  checks: quote_role verbatim', lp.verbatim(o.get('quote_role'), corpus), '| near', lp.near(lead['name'], o.get('quote_role') or '', corpus),
              '| sector quote verbatim', lp.verbatim(o.get('quote_sector'), corpus), '| name_in corpus', lp.name_in(lead['name'], corpus))
        print('  ->', r['tier'], r['reason'])
