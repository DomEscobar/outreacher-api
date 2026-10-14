#!/usr/bin/env python3
"""Independent quality check of leads (JSON lines on stdin) against NON-LinkedIn sources.

Per lead: web search "name" + firm (LinkedIn excluded) -> scrape top pages -> LLM judges current
role / law-firm / city with verbatim quotes -> quotes checked deterministically.
Verdicts: confirmed | partial | contradicted | not_found. Output: JSON lines + summary on stderr.
"""
import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import leadpipe as lp

SKIP_HOSTS = ('linkedin.', 'xing.', 'facebook.', 'instagram.', 'twitter.', 'x.com', 'youtube.')


def clean_name(name):
    return ' '.join(t for t in name.replace(',', ' ').split() if t.rstrip('.').casefold() not in {'dr', 'prof', 'll.m', 'llm', 'mba'})


def check(lead, city):
    name = clean_name(lead['name'])
    firm = lead.get('company') or ''
    pages = []
    for query in (f'"{name}" {firm}'.strip(), f'"{name}" Rechtsanwalt {city}'):
        for r in lp.fc_search(query, 6):
            host = (urlsplit(r.get('url') or '').hostname or '')
            if any(s in host for s in SKIP_HOSTS) or r['url'] in (p[0] for p in pages): continue
            pages.append((r['url'], f"{r.get('title', '')}\n{r.get('description', '')}"))
        if len(pages) >= 2: break
    if not pages:
        return {**lead, 'verdict': 'not_found', 'note': 'no non-LinkedIn source found'}
    texts = []
    for url, snippet in pages[:2]:
        body = lp.fc_scrape(url)[:15000]
        texts.append((url, snippet + '\n' + body))
    corpus = '\n\n'.join(f'### SOURCE {u}\n{t}' for u, t in texts)
    user = (f'Claim: {lead["name"]} is "{lead.get("current_role")}" at "{firm}", a law firm, in {city}.\n'
            'Check ONLY against the sources below. Return JSON: {"person_found": bool, "role_on_source": str|null, '
            '"quote_role": str|null, "is_decision_maker": bool|null, "firm_on_source": str|null, '
            '"firm_is_law_firm": bool|null, "quote_firm": str|null, "in_city": bool|null, "quote_city": str|null, '
            '"left_firm_or_contradiction": bool, "note": str}. is_decision_maker = owner/Inhaber, (equity or salaried) '
            'partner, Geschäftsführer or managing partner (not associate, counsel, of counsel, employee). quote_* must '
            'be exact substrings of the sources. Use null when the sources do not say.\n\n' + corpus)
    o = lp.llm_json(lp.ROLE_SYSTEM, user)
    ok = lambda q: lp.verbatim(q, corpus)
    role_ok = o.get('person_found') and o.get('is_decision_maker') and ok(o.get('quote_role')) and lp.name_in(name, corpus)
    firm_ok = o.get('firm_is_law_firm') and ok(o.get('quote_firm'))
    city_ok = o.get('in_city') and ok(o.get('quote_city'))
    if o.get('left_firm_or_contradiction') or o.get('is_decision_maker') is False or o.get('firm_is_law_firm') is False:
        verdict = 'contradicted'
    elif role_ok and firm_ok:
        verdict = 'confirmed'
    elif o.get('person_found') and lp.name_in(name, corpus):
        verdict = 'partial'
    else:
        verdict = 'not_found'
    return {'name': lead['name'], 'company': firm, 'claimed_role': lead.get('current_role'),
            'linkedin_url': lead['linkedin_url'], 'verdict': verdict, 'city_confirmed': bool(city_ok),
            'role_on_source': o.get('role_on_source') if ok(o.get('quote_role')) else None,
            'source': texts[0][0], 'note': (o.get('note') or '')[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--city', default='München')
    ap.add_argument('--workers', type=int, default=3)
    a = ap.parse_args()
    leads = [json.loads(l) for l in sys.stdin if l.strip()]
    t0 = time.monotonic()
    with ThreadPoolExecutor(a.workers) as pool:
        results = list(pool.map(lambda l: check(l, a.city), leads))
    for r in results: print(json.dumps(r, ensure_ascii=False))
    summary = Counter(r['verdict'] for r in results)
    print(json.dumps({'checked': len(results), **summary, 'city_confirmed': sum(r.get('city_confirmed', False) for r in results),
                      'seconds': round(time.monotonic() - t0, 1), **lp.stats}, ensure_ascii=False), file=sys.stderr)


if __name__ == '__main__':
    main()
