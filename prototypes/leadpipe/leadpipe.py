#!/usr/bin/env python3
"""Prototype lead pipelines (no browser agent, no LinkedIn login).

Variant B  linkedin-first : search engine (Firecrawl) for LinkedIn profiles -> LLM reads
                            title/snippet -> verbatim-quote check -> lead.
Variant A  firm-first     : search law-firm imprint pages -> scrape -> LLM extracts
                            decision makers with verbatim quotes -> find LinkedIn profile
                            via search (required) -> lead.

Every populated fact must appear verbatim in an observed source text (snippet or page).
A LinkedIn URL is mandatory for every lead. Output: JSON lines on stdout, stats on stderr.

Env: FIRECRAWL_API_KEY, OPENROUTER_API_KEY, optional LEADPIPE_MODEL.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from urllib.parse import urlsplit

FIRECRAWL = 'https://api.firecrawl.dev/v2'
OPENROUTER = 'https://openrouter.ai/api/v1/chat/completions'
MODEL = os.environ.get('LEADPIPE_MODEL', 'deepseek/deepseek-v4.1-flash')
DIRECTORIES = ('anwalt.de', 'anwalt24', '11880', 'gelbeseiten', 'dasoertliche', 'jameda', 'google.', 'linkedin.',
               'xing.', 'facebook.', 'instagram.', 'yelp.', 'kanzleiverzeichnis', 'juve.', 'wikipedia.', 'golocal',
               'cylex', 'werkenntdenbesten', 'provenexpert', 'brak.de', 'rak-', 'rechtsanwaltskammer', 'meinestadt',
               'branchenbuch', 'stadtbranchenbuch', 'trustlocal', 'advocado', 'anwaltauskunft', 'justico', '123recht')

stats = {'firecrawl_search': 0, 'firecrawl_scrape': 0, 'llm_calls': 0, 'llm_tokens': 0, 'errors': 0, 'rate_limited': 0, 'cache_hits': 0, 'cache_misses': 0, 'treg_cost_micro': 0}
lock = Lock()


def count(key, n=1):
    with lock: stats[key] += n


class Throttle:
    """Global minimum spacing between calls, shared by all worker threads."""
    def __init__(self, interval):
        self.interval, self.next, self.lock = interval, 0.0, Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        time.sleep(max(0.0, slot - now))


firecrawl_throttle = Throttle(float(os.environ.get('FIRECRAWL_INTERVAL', '0.8')))
CACHE_DIR = os.environ.get('LEADPIPE_CACHE')  # optional: directory for cached Firecrawl responses


def cached(kind, body, fetch):
    if not CACHE_DIR: return fetch()
    import hashlib
    path = os.path.join(CACHE_DIR, kind + '-' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24] + '.json')
    try:
        with open(path, encoding='utf-8') as f:
            count('cache_hits'); return json.load(f)
    except (OSError, ValueError):
        pass
    if os.environ.get('LEADPIPE_OFFLINE'):  # replay cached responses only; never spend credits
        count('cache_misses'); return None
    value = fetch()
    if value:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f: json.dump(value, f)
    return value


def post(url, body, headers, timeout=90, retries=5, throttle=None):
    data = json.dumps(body).encode()
    for attempt in range(retries):
        if throttle: throttle.wait()
        req = urllib.request.Request(url, data=data, method='POST',
                                     headers={'Content-Type': 'application/json', **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                if e.code == 429: count('rate_limited')
                retry_after = e.headers.get('Retry-After')
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt * 3)
                continue
            count('errors')
            with lock: stats.setdefault('http_errors', {}).setdefault(str(e.code), 0); stats['http_errors'][str(e.code)] += 1
            return None
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1: continue
            count('errors')
            return None


BACKEND = os.environ.get('LEADPIPE_BACKEND', 'firecrawl')  # firecrawl | treg
TREG = 'https://treg.to/call/'
treg_throttle = Throttle(float(os.environ.get('TREG_INTERVAL', '0.1')))


def treg_headers():
    token = os.environ['TREG_TOKEN']
    org = os.environ.get('TREG_ORG')
    if not org:  # identity tokens carry the team slug in their (unsigned-readable) payload
        import base64
        payload = token.split('.')[0]
        org = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('org')
    return {'X-Treg-Token': token, 'X-Treg-Org': org, 'X-Treg-Route-Max-Cost': os.environ.get('TREG_MAX_COST', '0.01')}


# crawl4ai's "Google" rows were mostly directories in tests; anyapi/dataforseo returned the real Google page.
TREG_EXCLUDE = {'treg.google.serp.organic': os.environ.get('TREG_SERP_EXCLUDE', 'crawl4ai')}


def treg_call(endpoint, body):
    headers = treg_headers()
    exclude = TREG_EXCLUDE.get(endpoint)
    if exclude: headers['X-Treg-Route-Exclude'] = exclude
    kind = 'treg-' + endpoint + ('-x-' + exclude.replace(',', '_') if exclude else '')
    r = cached(kind, body, lambda: post(TREG + endpoint, body, headers, timeout=120, throttle=treg_throttle))
    if r:
        count('treg_cost_micro', ((r.get('_treg') or {}).get('charged_micro') or 0))
    return (r or {}).get('output') or {}


def fc_search(query, limit=10, scrape=False):
    """Web search -> [{url, title, description, markdown?}] on the configured backend."""
    count('firecrawl_search')
    if BACKEND == 'treg':
        rows = treg_call('treg.google.serp.organic', {'q': query, 'country': 'de', 'language': 'de', 'limit': limit})
        results = [{'url': r.get('url') or r.get('link'), 'title': r.get('title', ''),
                    'description': r.get('snippet') or r.get('description') or ''} for r in rows.get('results') or []][:limit]
        if scrape:
            for r in results:
                if r['url'] and not linkedin_profile(r['url']) and not any(s in (urlsplit(r['url']).hostname or '') for s in SKIP_HOSTS):
                    r['markdown'] = fc_scrape(r['url'])
        return results
    body = {'query': query, 'limit': limit, 'country': 'DE', 'lang': 'de'}
    if scrape:  # one call returns results incl. page markdown (saves separate scrapes)
        count('firecrawl_scrape', limit)
        body['scrapeOptions'] = {'formats': ['markdown'], 'onlyMainContent': True}
    r = cached('search', body, lambda: post(FIRECRAWL + '/search', body,
               {'Authorization': 'Bearer ' + os.environ['FIRECRAWL_API_KEY']}, throttle=firecrawl_throttle))
    return (r or {}).get('data', {}).get('web', []) if r and r.get('success') else []


def fc_scrape(url):
    count('firecrawl_scrape')
    if BACKEND == 'treg':
        pages = treg_call('treg.web.extract', {'url': url}).get('pages') or []
        return (pages[0].get('markdown') or pages[0].get('content') or '') if pages else ''
    body = {'url': url, 'formats': ['markdown'], 'onlyMainContent': False}
    r = cached('scrape', body, lambda: post(FIRECRAWL + '/scrape', body,
               {'Authorization': 'Bearer ' + os.environ['FIRECRAWL_API_KEY']}, timeout=120, throttle=firecrawl_throttle))
    return ((r or {}).get('data') or {}).get('markdown') or ''


def llm_json(system, user):
    count('llm_calls')
    r = post(OPENROUTER, {'model': MODEL, 'temperature': 0, 'response_format': {'type': 'json_object'},
                          'reasoning': {'enabled': False},
                          'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]},
             {'Authorization': 'Bearer ' + os.environ['OPENROUTER_API_KEY']}, timeout=180)
    if not r: return {}
    count('llm_tokens', (r.get('usage') or {}).get('total_tokens', 0))
    text = r['choices'][0]['message']['content'] or ''
    match = re.search(r'\{.*\}', text, re.S)
    try: return json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError: return {}


def norm(s):
    s = unicodedata.normalize('NFKC', s or '').replace(' ', ' ')
    return re.sub(r'\s+', ' ', s).strip().casefold()


def verbatim(quote, source):
    return bool(quote) and len(quote) >= 4 and norm(quote) in norm(source)


def linkedin_profile(url):
    p = urlsplit(url or '')
    if not p.hostname or not p.hostname.endswith('linkedin.com'): return None
    m = re.match(r'^/in/([^/?#]+)', p.path)
    return f'https://www.linkedin.com/in/{m.group(1)}' if m else None


def name_key(name):
    # Drop titles so "Dr. Max Muster" matches "Max Muster" in a LinkedIn title.
    tokens = [t for t in re.split(r'[\s,.]+', norm(name)) if t and t not in {'dr', 'prof', 'll', 'm', 'rechtsanwalt', 'rechtsanwältin', 'ra', 'rain', 'mba', 'llm'}]
    return tokens


def name_in(name, text):
    tokens = name_key(name)
    t = norm(text)
    return len(tokens) >= 2 and all(re.search(r'(?<!\w)' + re.escape(tok) + r'(?!\w)', t) for tok in (tokens[0], tokens[-1]))


ROLE_SYSTEM = ('You extract professional facts as strict JSON. Use ONLY the supplied text. Copy quotes as exact '
               'substrings of the supplied text (character for character). Never translate, expand or invent. '
               'If unsure, set the value to null.')


# ---------------------------------------------------------------- Variant B: linkedin-first
def variant_b(city, roles, sector_terms, per_query, workers):
    queries = [f'site:linkedin.com/in {term} {role} {city}' for term in sector_terms for role in roles]
    with ThreadPoolExecutor(workers) as pool:
        results = [r for batch in pool.map(lambda q: fc_search(q, per_query), queries) for r in batch]
    profiles = {}
    for r in results:
        url = linkedin_profile(r.get('url'))
        if url and url not in profiles:
            profiles[url] = {'linkedin_url': url, 'source_text': f"{r.get('title', '')}\n{r.get('description', '')}"}
    items = list(profiles.values())
    leads = []

    def classify(chunk):
        user = ('Target: decision makers (owner/Inhaber, partner, Geschäftsführer, managing partner) of firms in the sector '
                f'described by: {sector_terms}, located in or near {city}. For EACH item return an object with keys: '
                'index, name, current_role, company, is_decision_maker (bool), in_sector (bool), in_city (bool or null), '
                'where in_sector means the COMPANY itself (current employer) is a firm of that sector; a lawyer who runs '
                'or works for a non-sector business (restaurant, publisher, software, seminars) is in_sector=false; '
                'quote_name, quote_role, quote_company, quote_sector, quote_city. Quotes must be exact substrings of that '
                "item's text. Employees, associates, counsel, of-counsel, trainees, judges, in-house lawyers: "
                'is_decision_maker=false. Return {"items": [...]}.\n\n' +
                '\n'.join(f'[{i}] {it["source_text"]}' for i, it in enumerate(chunk)))
        out = llm_json(ROLE_SYSTEM, user).get('items') or []
        accepted = []
        for o in out:
            try: it = chunk[int(o.get('index'))]
            except (TypeError, ValueError, IndexError): continue
            src = it['source_text']
            if not (o.get('is_decision_maker') and o.get('in_sector') and o.get('in_city') is not False): continue
            checks = {'name': o.get('quote_name'), 'current_role': o.get('quote_role'),
                      'company': o.get('quote_company'), 'sector': o.get('quote_sector')}
            if not all(verbatim(q, src) for q in checks.values()): continue
            if not (o.get('name') and name_in(o['name'], src)): continue
            accepted.append({'variant': 'B', 'name': o['name'], 'current_role': o.get('current_role'),
                             'company': o.get('company'), 'linkedin_url': it['linkedin_url'],
                             'evidence': [{'url': it['linkedin_url'], 'source': 'search_snippet', 'quote': q, 'supports': k}
                                          for k, q in checks.items()]})
        return accepted

    chunks = [items[i:i + 8] for i in range(0, len(items), 8)]
    with ThreadPoolExecutor(workers) as pool:
        for batch in pool.map(classify, chunks): leads.extend(batch)
    return leads, len(items)


# ---------------------------------------------------------------- Variant A: firm-first
def firm_sites(city, sector_terms, per_query, workers):
    queries = [f'{term} {city} Impressum' for term in sector_terms] + [f'{term} {city} Partner Team' for term in sector_terms]
    with ThreadPoolExecutor(workers) as pool:
        results = [r for batch in pool.map(lambda q: fc_search(q, per_query), queries) for r in batch]
    sites = {}
    for r in results:
        url = r.get('url') or ''
        host = (urlsplit(url).hostname or '').removeprefix('www.')
        if not host or any(d in host for d in DIRECTORIES): continue
        sites.setdefault(host, url)
    return sites


def variant_a(city, roles, sector_terms, per_query, workers, max_firms):
    sites = dict(list(firm_sites(city, sector_terms, per_query, workers).items())[:max_firms])

    def people_of(item):
        host, url = item
        page = fc_scrape(url)
        if 'impressum' not in url.lower() and len(page) < 200: return []
        if 'impressum' not in url.lower():
            imprint = fc_scrape(f'https://{host}/impressum')
            page = (page + '\n\n' + imprint) if imprint else page
        page = page[:30000]
        user = (f'Sector of interest: {sector_terms}. City: {city}. From this firm website text, list the firm decision '
                'makers (Inhaber/owner, Partner, Geschäftsführer, managing partner, vertretungsberechtigte Gesellschafter). '
                'Return {"in_sector": bool, "quote_sector": str, "company": str, "quote_company": str, "people": '
                '[{"name": str, "current_role": str, "quote_name_role": str}]}. quote_name_role must be ONE exact '
                'substring of the text containing both the name and the role (or a role heading directly followed by '
                'the name). No employees/associates. Text:\n\n' + page)
        o = llm_json(ROLE_SYSTEM, user)
        if not o.get('in_sector') or not verbatim(o.get('quote_sector'), page): return []
        company = o.get('company') if verbatim(o.get('quote_company'), page) and o.get('company') else None
        people = []
        for p in (o.get('people') or [])[:6]:
            q = p.get('quote_name_role')
            if p.get('name') and verbatim(q, page) and name_in(p['name'], q):
                people.append({'name': p['name'], 'current_role': p.get('current_role'), 'company': company,
                               'website': f'https://{host}', 'evidence': [
                                   {'url': url, 'source': 'firm_website', 'quote': o['quote_sector'], 'supports': 'sector'},
                                   {'url': url, 'source': 'firm_website', 'quote': q, 'supports': 'name,current_role'}] +
                                   ([{'url': url, 'source': 'firm_website', 'quote': o['quote_company'], 'supports': 'company'}] if company else [])})
        return people

    with ThreadPoolExecutor(workers) as pool:
        people = [p for batch in pool.map(people_of, sites.items()) for p in batch]

    def find_linkedin(person):
        firm = (person['company'] or '').split(' ')[0] if person['company'] else ''
        for query in (f'site:linkedin.com/in "{person["name"]}" {firm}'.strip(), f'site:linkedin.com/in "{person["name"]}" {city}'):
            for r in fc_search(query, 5):
                url = linkedin_profile(r.get('url'))
                text = f"{r.get('title', '')}\n{r.get('description', '')}"
                if url and name_in(person['name'], r.get('title', '')):
                    # Identity heuristic: same name in profile title plus firm or city in the snippet.
                    firm_hit = person['company'] and any(len(w) > 3 and norm(w) in norm(text) for w in re.split(r'[\s&,]+', person['company']))
                    city_hit = norm(city) in norm(text)
                    if firm_hit or city_hit:
                        return {**person, 'variant': 'A', 'linkedin_url': url,
                                'identity_match': 'name+firm' if firm_hit else 'name+city',
                                'evidence': person['evidence'] + [{'url': url, 'source': 'search_snippet', 'quote': r.get('title', ''), 'supports': 'linkedin_url'}]}
        return None

    with ThreadPoolExecutor(workers) as pool:
        leads = [l for l in pool.map(find_linkedin, people) if l]
    return leads, len(sites), len(people)


# ---------------------------------------------------------------- Variant C: B discovery + firm-website confirmation
SKIP_HOSTS = DIRECTORIES + ('twitter.', 'x.com', 'youtube.', 'amazon.', 'trustpilot.', 'kununu.', 'northdata.',
                            'rocketreach.', 'thalia.', 'schweitzer-online.', 'creditreform.', 'firmeneintrag.', 'devpro',
                            'lexology.', 'legal500.', 'chambers.', 'bestlawyers.', 'wer-zu-wem.', 'dnb.com', 'zoominfo.',
                            'companyhouse.', 'unternehmensregister.', 'bundesanzeiger.', 'handelsregister.', 'beck.de',
                            'buecher.', 'hugendubel.', 'ebay.', 'presseportal.', 'competitionline.', 'theorg.')
NON_FIRMS = {'selbstständig', 'selbständig', 'selbstaendig', 'freiberuflich', 'freelance', 'self-employed',
             'meiner kanzlei', 'eigene kanzlei', 'kanzlei', 'rechtsanwaltskanzlei', 'rechtsanwalt'}
GENERIC_FIRM_WORDS = {'rechtsanwälte', 'rechtsanwalt', 'rechtsanwaltsgesellschaft', 'kanzlei', 'partner', 'partners',
                      'partnerschaft', 'partnerschaftsgesellschaft', 'gmbh', 'mbb', 'partg', 'llp', 'legal', 'law',
                      'anwaltskanzlei', 'rechtsanwaltskanzlei', 'steuerberater', 'germany', 'deutschland', 'münchen'}
# Decision-maker role words, checked deterministically on the quoted website text (not the LLM's yes/no).
DM_TERMS = re.compile(r'partner|inhaber|geschäftsführ|gründer|founder|owner|gesellschafter|sozius|principal|managing director')
NON_PARTNER_TITLE = re.compile(r'counsel|associate|angestellt|referendar|wissenschaftliche|syndikus|trainee|paralegal')
NOT_DM = re.compile(r'associate partner|partner manager|business partner|kooperationspartner|ansprechpartner|'
                    r'vertriebspartner|partnerschaft\w*|partnerkanzlei\w*|netzwerkpartner|& partner\w*|und partner\w*')


def clean_company(company):
    c = (company or '').strip(' .,|')
    return None if not c or norm(c) in NON_FIRMS else c


def surname(name):
    tokens = name_key(name)
    return tokens[-1] if tokens else ''


def is_decision_maker_text(text):
    return bool(DM_TERMS.search(NOT_DM.sub(' ', norm(text))))


def near(name, quote, corpus, window=400):
    """Some occurrence of the quote sits close to some occurrence of the person's surname."""
    c, q, s = norm(corpus), norm(quote), surname(name)
    if not q or not s: return False
    names = [m.start() for m in re.finditer(r'(?<!\w)' + re.escape(s) + r'(?!\w)', c)]
    return any(abs(n - m.start()) <= window for m in re.finditer(re.escape(q), c) for n in names)


def translit(s):
    # Domains spell umlauts out (Rödl -> roedl); norm() already turned ß into ss.
    return s.replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')


def dm_title_near_name(name, corpus, window=200):
    """Exact page text around the surname that contains a decision-maker title, else None."""
    s = surname(name)
    if not s: return None
    for m in re.finditer(r'(?<!\w)' + re.escape(s) + r'(?!\w)', corpus, re.IGNORECASE):
        snippet = corpus[max(0, m.start() - window):m.end() + window]
        if is_decision_maker_text(snippet) and not NON_PARTNER_TITLE.search(NOT_DM.sub(' ', norm(snippet))):
            return snippet.strip()
    return None


def firm_tokens(firm):
    # >= 3 chars keeps short brand names (PwC, TGM, RAK, ASR); compare in transliterated form.
    return {translit(t) for t in re.split(r'[^\w]+', norm(firm or '')) if len(t) >= 3 and t not in GENERIC_FIRM_WORDS}


def host_related(lead, url):
    """The domain belongs to the firm or the person (not a directory or news site)."""
    host = (urlsplit(url).hostname or '').removeprefix('www.').casefold()
    if not host or any(s in host for s in SKIP_HOSTS): return False
    sn, tokens = translit(surname(lead['name'])), firm_tokens(clean_company(lead.get('company')))
    initials = ''.join(t[0] for t in re.split(r'[^\w]+', norm(lead.get('company') or '')) if t and t not in GENERIC_FIRM_WORDS)
    return any(t in host for t in tokens) or bool(sn and sn in host) or (len(initials) >= 2 and initials in host)


def site_score(lead, url, markdown):
    """Prefer the firm's own page about this person; < 3 = not usable."""
    if not markdown or not name_in(lead['name'], markdown): return 0  # the person must actually be on the page
    if not host_related(lead, url): return 1  # third-party page: never used as the firm website
    return 3 + (2 if surname(lead['name']) and translit(surname(lead['name'])) in translit(norm(urlsplit(url).path)) else 0)


def firm_pages(lead, city):
    """Find the firm's own page about this person, else the firm's imprint naming them.
    Only pages on firm-related domains are fetched. Returns (website, [(url, text)])."""
    name = ' '.join(t.capitalize() for t in name_key(lead['name']))
    firm = clean_company(lead.get('company'))
    queries = ([f'"{name}" {firm}'] if firm else []) + [f'"{name}" Rechtsanwalt {city}'] + ([firm + ' ' + city] if firm else [])
    seen, related = set(), []
    for q in queries:
        for r in fc_search(q, 8):
            url = r.get('url') or ''
            if url and url not in seen and host_related(lead, url):
                seen.add(url); related.append(url)
        if related: break
    sn = translit(surname(lead['name']))
    related.sort(key=lambda u: sn not in translit(norm(urlsplit(u).path)))  # the person's own page first
    for url in related[:3]:
        host = (urlsplit(url).hostname or '').removeprefix('www.')
        md = fc_scrape(url)
        imprint = '' if 'impressum' in url.lower() else fc_scrape(f'https://{host}/impressum')[:10000]
        if site_score(lead, url, md) >= 3:
            i = norm(md).find(surname(lead['name']))
            pages = [(url, md[max(0, i - 4000):i + 8000] if i > 4000 else md[:12000])]
            if imprint: pages.append((f'https://{host}/impressum', imprint))
            return f'https://{host}', pages
        if imprint and name_in(lead['name'], imprint):
            return f'https://{host}', [(f'https://{host}/impressum', imprint)] + ([(url, md[:6000])] if md else [])
    return None, []


def confirm(lead, city, sector_terms):
    website, pages = firm_pages(lead, city)
    base = {k: lead[k] for k in ('name', 'current_role', 'linkedin_url')}
    base['company'] = clean_company(lead.get('company'))
    base['evidence'] = lead['evidence']
    if not pages:
        return {**base, 'company_website': None, 'tier': 'review', 'reason': 'no firm page mentioning the person found'}
    corpus = '\n\n'.join(f'### SOURCE {u}\n{t}' for u, t in pages)
    user = (f'Person: {lead["name"]}. LinkedIn says: "{lead.get("current_role")}" at "{lead.get("company")}". '
            f'Sector: {sector_terms}. City: {city}. Using ONLY the firm website text below, return JSON: '
            '{"person_on_site": bool, "role_on_site": str|null, "quote_role": str|null, '
            '"listed_as_non_partner": bool, "firm_name": str|null, "quote_firm": str|null, "firm_in_sector": bool|null, '
            '"quote_sector": str|null, "in_city": bool|null, "quote_city": str|null, "solo_practice": bool|null, '
            '"quote_solo": str|null, "note": str}. role_on_site: the job title the site gives THIS person (e.g. Partner, '
            'Inhaber, Geschäftsführer, Associate, Rechtsanwalt). quote_role: the exact short text with that title next to '
            "the person's name. listed_as_non_partner: the site lists this person as associate, angestellter Rechtsanwalt, "
            'counsel or employee. firm_name: legal firm name, preferably from the imprint. solo_practice: the firm is '
            'clearly this one lawyer (e.g. imprint names only them). quote_* are exact substrings of the text.\n\n' + corpus)
    o = llm_json(ROLE_SYSTEM, user)
    ok = lambda q: verbatim(q, corpus)
    sector_on_page = any(re.search(r'(?<!\w)' + re.escape(norm(term)), norm(corpus)) for term in sector_terms if term)
    # Sector is already proven verbatim in the LinkedIn snippet (variant B); the website must not contradict it.
    lead_sector = any(e.get('supports') == 'sector' for e in lead.get('evidence', []))
    firm_ok = o.get('firm_in_sector') is not False and (bool(o.get('firm_in_sector')) and ok(o.get('quote_sector')) or sector_on_page or lead_sector)
    firm_name = o.get('firm_name') if o.get('firm_name') and ok(o.get('quote_firm')) and verbatim(o['firm_name'], o['quote_firm']) else None
    on_site = name_in(lead['name'], corpus)
    role_quoted = ok(o.get('quote_role')) and near(lead['name'], o['quote_role'], corpus)
    role_ok = role_quoted and is_decision_maker_text(o['quote_role'])
    near_title = None
    if not role_ok and is_decision_maker_text(o.get('role_on_site') or ''):
        # LLM read a decision-maker title but quoted the wrong line: accept only if the page itself
        # has a decision-maker term right next to the name (two independent signals).
        near_title = dm_title_near_name(lead['name'], corpus)
        role_ok = near_title is not None
    namesake = bool(firm_name) and surname(lead['name']) in norm(firm_name)
    solo = bool(o.get('solo_practice')) and ok(o.get('quote_solo'))
    evidence = list(lead['evidence'])
    for key, sup in (('quote_role', 'current_role'), ('quote_firm', 'company'), ('quote_sector', 'sector'), ('quote_city', 'city')):
        if ok(o.get(key)): evidence.append({'url': website, 'source': 'firm_website', 'quote': o[key], 'supports': sup})
    if near_title: evidence.append({'url': website, 'source': 'firm_website', 'quote': near_title, 'supports': 'current_role'})
    result = {**base, 'company': firm_name or base['company'], 'company_website': website,
              'role_on_site': o.get('role_on_site') if (role_quoted or near_title) else None,
              'city_confirmed': bool(o.get('in_city')) and ok(o.get('quote_city')), 'evidence': evidence}
    non_partner = role_quoted and bool(NON_PARTNER_TITLE.search(NOT_DM.sub(' ', norm(o['quote_role'])))) and not role_ok
    if (o.get('listed_as_non_partner') and not role_ok) or non_partner or o.get('firm_in_sector') is False:
        return {**result, 'tier': 'review', 'reason': 'website contradicts LinkedIn (role or sector)'}
    if firm_ok and on_site and role_ok:
        return {**result, 'tier': 'verified', 'reason': 'decision-maker title quoted next to the name on firm website'}
    if firm_ok and on_site and (namesake or solo):
        return {**result, 'tier': 'plausible', 'reason': 'namesake or solo law firm; decision-maker title not on site'}
    return {**result, 'tier': 'review', 'reason': 'decision-maker title not confirmed on firm website'}


def variant_c(city, roles, sector_terms, per_query, workers):
    found, candidates = variant_b(city, roles, sector_terms, per_query, workers)
    unique = list({l['linkedin_url']: l for l in found}.values())
    with ThreadPoolExecutor(workers) as pool:
        leads = list(pool.map(lambda l: confirm(l, city, sector_terms), unique))
    return leads, candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('variant', choices=['A', 'B', 'C'])
    ap.add_argument('--city', default='München')
    ap.add_argument('--roles', default='Partner,Inhaber,Geschäftsführer')
    ap.add_argument('--sector', default='Rechtsanwalt,Rechtsanwältin', help='comma-separated sector search terms')
    ap.add_argument('--per-query', type=int, default=20)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--max-firms', type=int, default=15)
    a = ap.parse_args()
    roles, sector = a.roles.split(','), a.sector.split(',')
    t0 = time.monotonic()
    if a.variant == 'B':
        leads, candidates = variant_b(a.city, roles, sector, a.per_query, a.workers)
        extra = {'candidates': candidates}
    elif a.variant == 'C':
        leads, candidates = variant_c(a.city, roles, sector, a.per_query, a.workers)
        extra = {'candidates': candidates, 'tiers': {t: sum(l['tier'] == t for l in leads) for t in ('verified', 'plausible', 'review')}}
    else:
        leads, firms, people = variant_a(a.city, roles, sector, a.per_query, a.workers, a.max_firms)
        extra = {'firms': firms, 'people_on_websites': people}
    seen = set()
    for lead in leads:
        if lead['linkedin_url'] in seen: continue
        seen.add(lead['linkedin_url'])
        print(json.dumps(lead, ensure_ascii=False))
    print(json.dumps({'variant': a.variant, 'city': a.city, 'leads': len(seen), 'seconds': round(time.monotonic() - t0, 1),
                      **extra, **stats}, ensure_ascii=False), file=sys.stderr)


if __name__ == '__main__':
    main()
