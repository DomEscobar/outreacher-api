"""Search-engine discovery backend for source="search": no browser agent, no LinkedIn login.

Pipeline per job (all provider calls through treg.to, one token, pay per call):
 1. discover : Google results for  site:linkedin.com/in <keyword> <title> <location>
 2. classify : an LLM reads each result title/snippet; name, role, firm and sector quotes are
               checked here to be literal substrings of that snippet
 3. confirm  : the firm's own page (or imprint) is fetched; a decision-maker title next to the
               person's name makes the lead "verified", a one-person/namesake firm "plausible",
               role+firm proven only by the LinkedIn snippet (website silent) "linkedin",
               a website contradicting role or sector "review"
 4. firms    : if the target is still short, firm imprints in the location are mined for decision
               makers and their LinkedIn profiles are looked up (same confirmation)

Facts are accepted only as verbatim text of fetched sources (search snippets, firm pages). The
LinkedIn association rests on the search index snippet (name in the profile title), not on a
logged-in profile view. Secrets come from the environment or HERMES_HOME/.env and are never
logged, persisted in results or returned.
"""
import base64
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

from models import Lead, profile_key, role_matches

TREG = 'https://treg.to/call/'
OPENROUTER = 'https://openrouter.ai/api/v1/chat/completions'
DEFAULT_MODEL = 'deepseek/deepseek-v4.1-flash'
DEFAULT_TITLES = ('Inhaber', 'Geschäftsführer', 'Partner', 'CEO', 'Gründer')  # search terms when no titles are given
FEMININE = {'partner': 'Partnerin', 'inhaber': 'Inhaberin', 'geschäftsführer': 'Geschäftsführerin',
            'gründer': 'Gründerin', 'gesellschafter': 'Gesellschafterin'}
WORKERS = 6
MAX_QUERIES = 80
TIER_ORDER = {'verified': 0, 'plausible': 1, 'linkedin': 2, 'review': 3}
MAX_EXPANSIONS = 4
MAX_FIRMS = 40
# crawl4ai's "Google" rows were mostly directories in tests; anyapi/dataforseo return the real page.
SERP_EXCLUDE = os.environ.get('DISCOVERY_SEARCH_SERP_EXCLUDE', 'crawl4ai')

SKIP_HOSTS = ('linkedin.', 'xing.', 'facebook.', 'instagram.', 'twitter.', 'x.com', 'youtube.', 'google.',
              'wikipedia.', 'anwalt.de', 'anwalt24', '11880', 'gelbeseiten', 'dasoertliche', 'dastelefonbuch',
              'jameda', 'yelp.', 'kanzleiverzeichnis', 'juve.', 'golocal', 'cylex', 'werkenntdenbesten',
              'provenexpert', 'brak.de', 'rak-', 'rechtsanwaltskammer', 'meinestadt', 'branchenbuch',
              'trustlocal', 'advocado', 'anwaltauskunft', 'justico', '123recht', 'rechtecheck', 'advopedia',
              'anwalt-suchservice', 'rechtsanwalt.com', 'rechtsanwalt.net', 'lokaler-anwalt', 'amazon.',
              'trustpilot.', 'kununu.', 'northdata.', 'rocketreach.', 'thalia.', 'schweitzer-online.',
              'creditreform.', 'firmeneintrag.', 'lexology.', 'legal500.', 'chambers.', 'bestlawyers.',
              'wer-zu-wem.', 'dnb.com', 'zoominfo.', 'companyhouse.', 'unternehmensregister.', 'bundesanzeiger.',
              'handelsregister.', 'beck.de', 'buecher.', 'hugendubel.', 'ebay.', 'presseportal.', 'theorg.',
              'tracxn.', 'mapquest.', 'infobel.', 'focus.de', 'haufe.de', 'iclg.', 'resolvio.', 'crunchbase.',
              'glassdoor.', 'indeed.', 'stepstone.', 'firmenwissen.', 'wlw.de', 'kompass.', 'europages.', 'yumpu.',
              'issuu.', 'slideshare.', 'github.', 'bloomberg.', 'owler.', 'apollo.io', 'lusha.', 'signalhire.')
# Placeholders people write instead of an employer.
NON_FIRMS = {'selbstständig', 'selbständig', 'selbstaendig', 'freiberuflich', 'freiberufler', 'freelance', 'freelancer',
             'self-employed', 'self employed', 'eigenes unternehmen', 'eigene firma', 'meiner kanzlei', 'eigene kanzlei',
             'eigene praxis', 'stealth', 'stealth mode', 'confidential', 'kanzlei', 'praxis', 'agentur', 'firma', 'unternehmen'}
# Words that say nothing about WHICH company (legal forms, filler, generic business words, any sector);
# the request's own keywords are added per job. Used to match firm names to domains.
GENERIC_FIRM_WORDS = {'gmbh', 'mbh', 'ag', 'kg', 'co', 'gbr', 'ohg', 'ug', 'se', 'ev', 'partg', 'mbb', 'llp', 'ltd', 'inc',
                      'llc', 'plc', 'corp', 'gesellschaft', 'partner', 'partners', 'partnerschaft', 'partnerschaftsgesellschaft',
                      'group', 'gruppe', 'holding', 'germany', 'deutschland', 'international', 'und', 'and', 'von', 'der',
                      'die', 'das', 'the', 'kanzlei', 'praxis', 'agentur', 'studio', 'büro', 'buero', 'beratung',
                      'consulting', 'solutions', 'services', 'service', 'systems', 'technologies', 'digital', 'media',
                      'legal', 'law', 'rechtsanwälte', 'rechtsanwalt', 'rechtsanwaltsgesellschaft', 'steuerberatung',
                      'steuerberater', 'steuerberatungsgesellschaft', 'wirtschaftsprüfung'}
# Default target when the request names no titles: decision makers of any kind of company.
DM_TERMS = re.compile(r'partner|inhaber|geschäftsführ|gründer|founder|owner|gesellschafter|sozius|principal|'
                      r'managing director|(?<!\w)(?:ceo|cto|coo|cfo|cmo)(?!\w)|chief|vorstand|geschäftsleit|präsident|president|'
                      r'head of|director|direktor|leiter')
# "Partner" inside firm names and partner-as-relationship roles; "Zahnärztin & Partnerin" stays a role.
FIRM_PARTNER = re.compile(r'(?:(?<!\w)(?:bei|at|von|der|die|des)(?!\w)|@)\s+[^|,;·\n]*?(?:&|und|\+)\s*partner\w*|'
                          r'(?:&|und|\+)\s*partner\w*\s+(?:mbb|partg\w*|gmbh|ag|kg|gbr|ohg|llp|ltd|inc)(?!\w)|'
                          r'partnerschaft\w*|partnerkanzlei\w*')
NOT_DM = re.compile(r'associate partner|partner manager|business partner|channel partner|partner success|'
                    r'kooperationspartner|ansprechpartner|vertriebspartner|netzwerkpartner|assistent\w* (?:der|des|to)')
NON_PARTNER_TITLE = re.compile(r'counsel|associate|angestellt|referendar|wissenschaftliche|syndikus|trainee|paralegal|'
                               r'werkstudent|praktikant|intern(?!\w)|junior|assistenz|assistant')
EXTRACT_SYSTEM = ('You extract professional facts as strict JSON. Use ONLY the supplied text. Copy quotes as exact '
                  'substrings of the supplied text (character for character). Never translate, expand or invent. '
                  'If unsure, set the value to null.')


class SearchError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Stop(Exception):
    """Cooperative stop: cancelled, deadline or cost cap. Accepted leads are kept."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------- configuration

def env_value(key, home=None):
    if os.environ.get(key): return os.environ[key]
    path = os.path.join(home or os.environ.get('HERMES_HOME', '/opt/data'), '.env')
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                name, sep, value = line.strip().partition('=')
                if sep and name.strip() == key:
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def openrouter_key(home=None):
    """OPENROUTER_API_KEY from env/.env (hermes setup model), else Hermes' credential pool (hermes auth add)."""
    key = env_value('OPENROUTER_API_KEY', home)
    if key: return key
    try:
        with open(os.path.join(home or os.environ.get('HERMES_HOME', '/opt/data'), 'auth.json'), encoding='utf-8') as f:
            pool = (json.load(f).get('credential_pool') or {}).get('openrouter')
    except (OSError, ValueError, AttributeError):
        return None
    def find(o):
        if isinstance(o, str): return o if o.startswith('sk-or-') else None
        values = o.values() if isinstance(o, dict) else o if isinstance(o, list) else []
        return next((k for k in map(find, values) if k), None)
    return find(pool or {})


def save_env_value(key, value, home=None):
    """Write KEY=value into HERMES_HOME/.env (mode 0600), replacing an existing entry."""
    path = os.path.join(home or os.environ.get('HERMES_HOME', '/opt/data'), '.env')
    try:
        with open(path, encoding='utf-8') as f: lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = [l for l in lines if l.partition('=')[0].strip() != key] + [f'{key}={value}']
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write('\n'.join(lines) + '\n')
    os.chmod(path, 0o600)


def treg_org(token):
    if not token: return None
    if os.environ.get('TREG_ORG'): return os.environ['TREG_ORG']
    try:  # identity tokens carry the team slug in their readable payload
        payload = token.split('.')[0]
        return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('org')
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------- text helpers

def norm(s):
    s = unicodedata.normalize('NFKC', s or '').replace(' ', ' ')
    return re.sub(r'\s+', ' ', s).strip().casefold()


def translit(s):
    return s.replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')  # norm() already made ß -> ss


def verbatim(quote, source):
    return bool(quote) and len(quote) >= 4 and norm(quote) in norm(source)


def sanitize(value, limit=160):
    value = re.sub(r'[\x00-\x1f\x7f]+', ' ', value or '')
    value = re.sub(r'\s+', ' ', value).strip(' |,;-')
    return value[:limit].rstrip() or None


# Academic/professional prefixes and suffixes that are not part of a person's name.
TITLE_WORDS = {'dr', 'prof', 'll', 'm', 'med', 'dent', 'jur', 'rer', 'nat', 'dipl', 'ing', 'mag', 'msc', 'bsc', 'ba', 'ma',
               'mba', 'llm', 'phd', 'rechtsanwalt', 'rechtsanwältin', 'ra', 'rain', 'stb', 'wp'}


def name_key(name):
    return [t for t in re.split(r'[\s,.]+', norm(name)) if t and t not in TITLE_WORDS]


def name_in(name, text):
    tokens, t = name_key(name), norm(text)
    return len(tokens) >= 2 and all(re.search(r'(?<!\w)' + re.escape(tok) + r'(?!\w)', t) for tok in (tokens[0], tokens[-1]))


def surname(name):
    tokens = name_key(name)
    return tokens[-1] if tokens else ''


def linkedin_profile(url):
    p = urlsplit(url or '')
    if not p.hostname or not p.hostname.endswith('linkedin.com'): return None
    m = re.match(r'^/in/([^/?#]+)', p.path)
    return f'https://www.linkedin.com/in/{m.group(1)}' if m else None


def clean_company(company):
    c = sanitize(company)
    return None if not c or norm(c) in NON_FIRMS else c


def firm_tokens(firm, extra=()):
    generic = GENERIC_FIRM_WORDS | set(extra)
    return {translit(t) for t in re.split(r'[^\w]+', norm(firm or '')) if len(t) >= 3 and t not in generic}


def role_text(text):
    """Role text without firm names ("bei Muster & Partner mbB") and partner-as-relationship titles."""
    return NOT_DM.sub(' ', FIRM_PARTNER.sub(' ', norm(text)))


def is_decision_maker_text(text):
    return bool(DM_TERMS.search(role_text(text)))


def is_non_partner_text(text):
    return bool(NON_PARTNER_TITLE.search(role_text(text)))


def near(name, quote, corpus, window=400):
    c, q, s = norm(corpus), norm(quote), surname(name)
    if not q or not s: return False
    names = [m.start() for m in re.finditer(r'(?<!\w)' + re.escape(s) + r'(?!\w)', c)]
    return any(abs(n - m.start()) <= window for m in re.finditer(re.escape(q), c) for n in names)


def dm_title_near_name(name, corpus, matches=None, window=200):
    """Exact page text around the surname that contains a target title (`matches`), else None."""
    s, matches = surname(name), matches or is_decision_maker_text
    if not s: return None
    for m in re.finditer(r'(?<!\w)' + re.escape(s) + r'(?!\w)', corpus, re.IGNORECASE):
        snippet = corpus[max(0, m.start() - window):m.end() + window].strip()
        if matches(snippet) and not is_non_partner_text(snippet):
            return snippet
    return None


def source_quote(quote, text, min_len=8, max_len=1200, anchor=None):
    """The exact source line(s) containing `quote` as whole words (case/whitespace tolerant), >= min_len
    chars. With `anchor` (a surname) the occurrence closest to that name is used, not the first one."""
    words, text = (quote or '').split(), text or ''
    if not words: return None
    matches = list(re.finditer(r'(?<!\w)' + r'\s+'.join(map(re.escape, words)) + r'(?!\w)', text, re.IGNORECASE))
    if not matches: return None
    m = matches[0]
    if anchor:
        names = [n.start() for n in re.finditer(r'(?<!\w)' + re.escape(anchor) + r'(?!\w)', text, re.IGNORECASE)]
        if names: m = min(matches, key=lambda x: min(abs(x.start() - n) for n in names))
    start = text.rfind('\n', 0, m.start()) + 1
    end = text.find('\n', m.end())
    end = len(text) if end < 0 else end
    while len(text[start:end].strip()) < min_len and (start > 0 or end < len(text)):
        if start > 0:  # widen upwards first: the name usually sits above a short title line
            start = text.rfind('\n', 0, start - 1) + 1
        else:
            nxt = text.find('\n', end + 1)
            end = len(text) if nxt < 0 else nxt
    snippet = text[start:end].strip()
    if len(snippet) > max_len:
        mid = m.start() - start
        snippet = snippet[max(0, mid - max_len // 2):max(0, mid - max_len // 2) + max_len].strip()
    return snippet if len(snippet) >= min_len else None


# ---------------------------------------------------------------------------- provider clients

class Meter:
    """Job-wide cost, deadline and cancellation guard shared by all worker threads."""
    def __init__(self, max_cost_usd, deadline, cancelled):
        self.limit_micro, self.deadline, self.cancelled = int(max_cost_usd * 1_000_000), deadline, cancelled
        self.lock, self.cost_micro = Lock(), 0
        self.counts = {'provider_calls': 0, 'llm_calls': 0}

    def charge(self, micro, kind):
        with self.lock:
            self.cost_micro += int(micro or 0)
            self.counts[kind] += 1

    def check(self):
        if self.cancelled(): raise Stop('cancelled')
        if time.monotonic() >= self.deadline: raise Stop('timed_out')
        if self.cost_micro >= self.limit_micro: raise Stop('budget_exhausted')


def http_post(url, body, headers, timeout=90, retries=4):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method='POST',
                                     headers={'Content-Type': 'application/json; charset=utf-8', **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                retry = e.headers.get('Retry-After')
                time.sleep(min(30.0, float(retry)) if retry and retry.replace('.', '', 1).isdigit() else 2 ** attempt * 2)
                continue
            if e.code in (401, 403): raise SearchError('search_not_configured') from None
            return None
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt < retries - 1: continue
            return None


class Providers:
    """treg (SERP + page extraction) and OpenRouter (LLM). Replaceable in tests."""
    def __init__(self, meter, treg_token, openrouter_key, model=None):
        org = treg_org(treg_token)
        if not treg_token or not org or not openrouter_key: raise SearchError('search_not_configured')
        self.meter, self.model = meter, model or DEFAULT_MODEL
        self.treg_headers = {'X-Treg-Token': treg_token, 'X-Treg-Org': org, 'X-Treg-Route-Max-Cost': '0.01'}
        self.llm_headers = {'Authorization': 'Bearer ' + openrouter_key}

    def _treg(self, endpoint, body, headers=None):
        self.meter.check()
        r = http_post(TREG + endpoint, body, {**self.treg_headers, **(headers or {})}, timeout=120)
        self.meter.charge(((r or {}).get('_treg') or {}).get('charged_micro'), 'provider_calls')
        return (r or {}).get('output') or {}

    def serp(self, query, limit=10):
        out = self._treg('treg.google.serp.organic', {'q': query, 'country': 'de', 'language': 'de', 'limit': limit},
                         {'X-Treg-Route-Exclude': SERP_EXCLUDE} if SERP_EXCLUDE else None)
        return [{'url': r.get('url') or r.get('link') or '', 'title': r.get('title') or '',
                 'description': r.get('snippet') or r.get('description') or ''} for r in out.get('results') or []][:limit]

    def extract(self, url):
        pages = self._treg('treg.web.extract', {'url': url}).get('pages') or []
        return (pages[0].get('markdown') or pages[0].get('content') or '') if pages else ''

    def llm(self, user):
        self.meter.check()
        r = http_post(OPENROUTER, {'model': self.model, 'temperature': 0, 'response_format': {'type': 'json_object'},
                                   'reasoning': {'enabled': False}, 'usage': {'include': True},
                                   'messages': [{'role': 'system', 'content': EXTRACT_SYSTEM}, {'role': 'user', 'content': user}]},
                      self.llm_headers, timeout=180)
        self.meter.charge(float(((r or {}).get('usage') or {}).get('cost') or 0) * 1_000_000, 'llm_calls')
        try:
            text = r['choices'][0]['message']['content'] or ''
            match = re.search(r'\{.*\}', text, re.S)
            return json.loads(match.group(0)) if match else {}
        except (TypeError, KeyError, IndexError, ValueError):
            return {}


# ---------------------------------------------------------------------------- pipeline

class SearchPipeline:
    def __init__(self, criteria, providers, meter, previous=(), blocked=None):
        self.c, self.p, self.meter = criteria, providers, meter
        blocked = blocked or {}
        self.blocked_domains, self.blocked_names = set(blocked.get('domains', ())), set(blocked.get('names', ()))
        self.company_counts = {}
        self.sector = list(criteria.keywords)
        self.locations = list(criteria.locations) or ['']
        self.default_titles = not criteria.titles
        titles = list(criteria.titles) or list(DEFAULT_TITLES)
        self.title_terms = list(dict.fromkeys(titles + [FEMININE[t.casefold()] for t in titles if t.casefold() in FEMININE]))
        self.titles = titles
        self.sector_words = {translit(t) for kw in self.sector for t in re.split(r'[^\w]+', norm(kw)) if t}
        self.seen = {profile_key(u) for u in previous}
        self.lock = Lock()
        self.min_rank = TIER_ORDER[criteria.min_verification]
        self.stats = {'candidate_count': 0, 'queries': 0, 'firms_checked': 0, 'expanded_keywords': [], 'expanded_titles': [],
                      'excluded_by_registry': 0, 'excluded_same_company': 0,
                      'verification_counts': {t: 0 for t in TIER_ORDER}, 'strategies': []}

    # -- helpers
    def pool(self, fn, items):
        with ThreadPoolExecutor(WORKERS) as ex:
            return list(ex.map(fn, items))

    def role_ok(self, text):
        # Explicit titles are the target (e.g. "Head of Sales"); without them: decision makers of any kind.
        if self.default_titles: return is_decision_maker_text(text)
        return role_matches(role_text(text), self.titles)

    def target_roles(self):
        return (f'people whose CURRENT role matches one of these titles (or an obvious variant): {self.titles}'
                if not self.default_titles else
                'decision makers (owner, partner, managing director/Geschäftsführer, founder, CEO, board member)')

    def tokens(self, firm):
        return firm_tokens(firm, self.sector_words)

    def host_related(self, lead, url):
        host = (urlsplit(url).hostname or '').removeprefix('www.').casefold()
        if not host or any(s in host for s in SKIP_HOSTS): return False
        sn, tokens = translit(surname(lead['name'])), self.tokens(clean_company(lead.get('company')))
        initials = ''.join(t[0] for t in re.split(r'[^\w]+', norm(lead.get('company') or ''))
                           if t and t not in GENERIC_FIRM_WORDS and t not in self.sector_words)
        return any(t in host for t in tokens) or bool(sn and sn in host) or (len(initials) >= 2 and initials in host)

    # -- 1+2: LinkedIn profiles from search results
    def expand(self):
        """LLM-proposed equivalent wordings: related sector terms and SAME-role titles (other language/wording).
        Expanded titles are also accepted by the role check, so they must be true equivalents."""
        o = self.p.llm(
            f'Suggest LinkedIn search terms. Sector: {self.sector}. ' +
            (f'Target titles: {self.titles}. ' if not self.default_titles else '') +
            f'Return {{"keywords": [...], "titles": [...]}} with at most {MAX_EXPANSIONS} each. keywords: other words '
            'people in this sector use in their LinkedIn headline or company name (German and English, synonyms, '
            'sub-sectors; no places, no job titles). titles: ONLY exact equivalents of the target titles in other '
            'wording or language (e.g. "Head of Sales" -> "Vertriebsleiter", "Sales Director"); no broader or '
            'different roles; empty list if no target titles were given.')
        clean = lambda values, known: [v for v in dict.fromkeys(sanitize(x, 60) for x in (values or []) if isinstance(x, str))
                                       if v and norm(v) not in {norm(k) for k in known}][:MAX_EXPANSIONS]
        keywords = clean(o.get('keywords'), self.sector)
        titles = clean(o.get('titles'), self.titles) if not self.default_titles else []
        self.stats['expanded_keywords'], self.stats['expanded_titles'] = keywords, titles
        return keywords, titles

    def discover(self):
        extra_keywords, extra_titles = self.expand() if self.c.expand_terms else ([], [])
        self.titles = self.titles + extra_titles  # equivalents count for the role check, too
        base_titles, more_titles = self.title_terms, extra_titles
        # Originals for every location first, then expansions, so a query cap never drops a location.
        queries = list(dict.fromkeys(
            [f'site:linkedin.com/in {kw} {t} {loc}'.strip() for loc in self.locations for kw in self.sector for t in base_titles] +
            [f'site:linkedin.com/in {kw} {t} {loc}'.strip() for loc in self.locations for kw in self.sector for t in more_titles] +
            [f'site:linkedin.com/in {kw} {t} {loc}'.strip() for loc in self.locations for kw in extra_keywords
             for t in base_titles + more_titles]))[:MAX_QUERIES]
        self.stats['queries'] += len(queries)
        rows = [r for batch in self.pool(lambda q: self.p.serp(q, 20), queries) for r in batch]
        profiles = {}
        for r in rows:
            url = linkedin_profile(r['url'])
            if url and profile_key(url) not in self.seen and url not in profiles:
                profiles[url] = {'linkedin_url': url, 'text': f"{r['title']}\n{r['description']}"}
        items = list(profiles.values())
        self.stats['candidate_count'] += len(items)
        chunks = [items[i:i + 8] for i in range(0, len(items), 8)]
        return [lead for batch in self.pool(self.classify, chunks) for lead in batch]

    def classify(self, chunk):
        locs = [l for l in self.locations if l]
        user = (f'Target: {self.target_roles()} at companies in the sector described by {self.sector}' +
                (f', located in or near one of {locs}' if locs else '') + '. '
                'For EACH item return an object with keys: index, name, current_role, company, is_decision_maker (bool), '
                'in_sector (bool), in_location (bool or null), quote_name, quote_role, quote_company, quote_sector. '
                'is_decision_maker means: the current role is one of the target roles. in_sector means the COMPANY itself '
                '(current employer) belongs to that sector; a person with a matching qualification who works for a business '
                "of another kind is in_sector=false. Quotes must be exact substrings of that item's text. "
                'Return {"items": [...]}.\n\n' +
                '\n'.join(f'[{i}] {it["text"]}' for i, it in enumerate(chunk)))
        leads = []
        for o in self.p.llm(user).get('items') or []:
            try: it = chunk[int(o.get('index'))]
            except (TypeError, ValueError, IndexError): continue
            src = it['text']
            if not (o.get('is_decision_maker') and o.get('in_sector') and o.get('in_location') is not False): continue
            quotes = {'name': o.get('quote_name'), 'current_role': o.get('quote_role'),
                      'company': o.get('quote_company'), 'sector': o.get('quote_sector')}
            if not all(verbatim(q, src) for q in quotes.values()): continue
            name = sanitize(o.get('name'))
            if not (name and name_in(name, src) and self.role_ok(o.get('quote_role') or '')): continue
            evidence = [{'url': it['linkedin_url'], 'quote': src.strip()[:1200], 'source': 'search_snippet',
                         'supports': ['name', 'linkedin_url', 'current_role', 'company', 'sector']}]
            leads.append({'name': name, 'current_role': sanitize(o.get('current_role')), 'company': sanitize(o.get('company')),
                          'linkedin_url': it['linkedin_url'], 'linkedin_text': src, 'evidence': evidence})
        return leads

    # -- 3: confirmation on the firm's own website
    def firm_pages(self, lead):
        name = ' '.join(t.capitalize() for t in name_key(lead['name']))
        firm = clean_company(lead.get('company'))
        loc = next((l for l in self.locations if l), '')
        queries = ([f'"{name}" {firm}'] if firm else []) + [f'"{name}" {self.sector[0]} {loc}'.strip()] + ([f'{firm} {loc}'.strip()] if firm else [])
        related, seen = [], set()
        for q in queries:
            for r in self.p.serp(q, 8):
                if r['url'] and r['url'] not in seen and self.host_related(lead, r['url']):
                    seen.add(r['url']); related.append(r['url'])
            if related: break
        sn = translit(surname(lead['name']))
        related.sort(key=lambda u: sn not in translit(norm(urlsplit(u).path)))  # the person's own page first
        for url in related[:3]:
            if not url.startswith('https://'): continue
            host = (urlsplit(url).hostname or '').removeprefix('www.')
            md = self.p.extract(url)
            imprint = '' if 'impressum' in url.lower() else self.p.extract(f'https://{host}/impressum')[:10000]
            if md and name_in(lead['name'], md):
                i = norm(md).find(surname(lead['name']))
                pages = [(url, md[max(0, i - 4000):i + 8000] if i > 4000 else md[:12000])]
                return f'https://{host}', pages + ([(f'https://{host}/impressum', imprint)] if imprint else [])
            if imprint and name_in(lead['name'], imprint):
                return f'https://{host}', [(f'https://{host}/impressum', imprint)] + ([(url, md[:6000])] if md else [])
        return None, []

    def confirm(self, lead, website=None, pages=None):
        if pages is None: website, pages = self.firm_pages(lead)
        if not pages:
            return self.finish(lead, *self.snippet_only(lead, 'no company page mentioning the person found'))
        corpus = '\n\n'.join(t for _, t in pages)
        user = (f'Person: {lead["name"]}. Claimed: "{lead.get("current_role")}" at "{lead.get("company")}". '
                f'Sector: {self.sector}. Target: {self.target_roles()}. Using ONLY the company website text below, '
                'return JSON: {"person_on_site": bool, "role_on_site": str|null, "quote_role": str|null, '
                '"listed_in_other_role": bool, "firm_name": str|null, "quote_firm": str|null, "firm_in_sector": bool|null, '
                '"quote_sector": str|null, "solo_practice": bool|null, "quote_solo": str|null}. role_on_site: the job title '
                "the site gives THIS person. quote_role: the exact short text with that title next to the person's name. "
                'listed_in_other_role: the site lists this person in a role that is clearly NOT a target role. firm_name: '
                'legal company name, preferably from the imprint. solo_practice: the company is clearly this one person. '
                'quote_* are exact substrings of the text.\n\n' +
                '\n\n'.join(f'### SOURCE {u}\n{t}' for u, t in pages))
        o = self.p.llm(user)
        ok = lambda q: verbatim(q, corpus)
        sector_on_page = any(re.search(r'(?<!\w)' + re.escape(norm(t)), norm(corpus)) for t in self.sector)
        lead_sector = any('sector' in e['supports'] for e in lead['evidence'])
        firm_ok = o.get('firm_in_sector') is not False and (bool(o.get('firm_in_sector')) and ok(o.get('quote_sector')) or sector_on_page or lead_sector)
        firm_name = sanitize(o.get('firm_name')) if o.get('firm_name') and ok(o.get('quote_firm')) and verbatim(o['firm_name'], o['quote_firm']) else None
        on_site = name_in(lead['name'], corpus)
        role_quoted = ok(o.get('quote_role')) and near(lead['name'], o['quote_role'], corpus)
        role_ok = role_quoted and self.role_ok(o['quote_role'])
        near_title = None
        if not role_ok and self.role_ok(o.get('role_on_site') or ''):
            near_title = dm_title_near_name(lead['name'], corpus, self.role_ok)  # second, deterministic signal
            role_ok = near_title is not None
        non_partner = self.default_titles and role_quoted and is_non_partner_text(o['quote_role']) and not role_ok
        namesake = bool(firm_name) and surname(lead['name']) in norm(firm_name)
        solo = bool(o.get('solo_practice')) and ok(o.get('quote_solo'))
        evidence = list(lead['evidence'])
        page_of = lambda q: next((u for u, t in pages if verbatim(q, t)), website)
        for key, supports in (('quote_role', ['name', 'current_role']), ('quote_firm', ['company']), ('quote_sector', ['sector'])):
            if ok(o.get(key)):
                url = page_of(o[key])
                quote = source_quote(o[key], dict(pages).get(url, corpus),
                                     anchor=surname(lead['name']) if key == 'quote_role' else None)
                if quote: evidence.append({'url': url, 'quote': quote, 'source': 'firm_website', 'supports': supports})
        if near_title:
            evidence.append({'url': page_of(near_title), 'quote': near_title[:1200], 'source': 'firm_website', 'supports': ['current_role']})
        lead = {**lead, 'company': firm_name or clean_company(lead.get('company')), 'company_website': website,
                'role_on_site': sanitize(o.get('role_on_site')) if (role_quoted or near_title) else None, 'evidence': evidence}
        if (o.get('listed_in_other_role') and not role_ok) or non_partner or o.get('firm_in_sector') is False:
            return self.finish(lead, 'review', 'company website contradicts the role or sector')
        if firm_ok and on_site and role_ok:
            return self.finish(lead, 'verified', 'target title next to the name on the company website')
        if firm_ok and on_site and (namesake or solo):
            return self.finish(lead, 'plausible', 'one-person or namesake company; title not stated on the website')
        return self.finish(lead, *self.snippet_only(lead, 'target title not confirmed on the company website'))

    @staticmethod
    def snippet_only(lead, reason):
        """Website silent (not contradicting): the LinkedIn snippet alone proved role and firm -> tier "linkedin"."""
        proven = any(e.get('source') == 'search_snippet' and {'current_role', 'company'} <= set(e['supports'])
                     for e in lead['evidence'])
        return ('linkedin', 'role and company only in the LinkedIn search snippet; ' + reason) if proven else ('review', reason)

    def finish(self, lead, tier, reason):
        firm = clean_company(lead.get('company'))
        snippet_firm = bool(self.tokens(firm) & set(re.split(r'[^\w]+', translit(norm(lead.get('linkedin_text') or '')))))
        website = lead.get('company_website')
        data = {'name': lead['name'], 'current_role': lead.get('current_role') or lead.get('role_on_site'),
                'company': firm, 'company_website': website if website and website.startswith('https://') else None,
                'linkedin_url': lead['linkedin_url'], 'evidence': [{**e, 'quote': e['quote'][:1200]} for e in lead['evidence']
                                                                      if len(e.get('quote') or '') >= 8][:10],
                'identity_status': 'verified' if tier == 'verified' and snippet_firm else 'needs_review',
                'fit_reason': None, 'verification': tier, 'verification_reason': reason}
        data['missing_information'] = [k for k in ('current_role', 'company', 'company_website') if not data[k]]
        try:
            return Lead.model_validate(data).model_dump()
        except ValueError:
            return None

    # -- 4: company-first supplement (German imprints name the legally responsible people)
    def firm_first(self, need):
        self.stats['strategies'].append('firm_websites')
        queries = [f'{kw} {loc} {suffix}'.strip() for loc in self.locations for kw in self.sector for suffix in ('Impressum', 'Team')]
        hosts = {}
        for batch in self.pool(lambda q: self.p.serp(q, 10), queries[:MAX_QUERIES]):
            for r in batch:
                host = (urlsplit(r['url']).hostname or '').removeprefix('www.')
                if host and r['url'].startswith('https://') and not any(s in host for s in SKIP_HOSTS):
                    hosts.setdefault(host, r['url'])
        found = []
        for chunk_start in range(0, min(len(hosts), MAX_FIRMS), WORKERS):
            self.meter.check()
            items = list(hosts.items())[chunk_start:chunk_start + WORKERS]
            self.stats['firms_checked'] += len(items)
            for batch in self.pool(self.people_of_firm, items):
                found.extend(l for l in batch if l)
            if sum(l['verification'] in ('verified', 'plausible') for l in found) >= need: break
        return found

    def people_of_firm(self, item):
        host, url = item
        imprint = self.p.extract(url if 'impressum' in url.lower() else f'https://{host}/impressum')[:15000]
        if len(imprint) < 100: return []
        user = (f'Sector of interest: {self.sector}. Target: {self.target_roles()}. From this company imprint/team text, '
                'return {"in_sector": bool, "quote_sector": str, "company": str, "quote_company": str, "people": '
                '[{"name": str, "current_role": str, "quote_name_role": str}]}. people: only persons in a target role '
                '(imprints usually name owners, managing directors, board members or representing partners). '
                'quote_name_role is ONE exact substring containing the name and the role. Text:\n\n' + imprint)
        o = self.p.llm(user)
        if not o.get('in_sector') or not verbatim(o.get('quote_sector'), imprint): return []
        company = sanitize(o.get('company')) if o.get('company') and verbatim(o.get('quote_company'), imprint) else None
        leads = []
        for person in (o.get('people') or [])[:4]:
            name, q = sanitize(person.get('name')), person.get('quote_name_role')
            if not (name and verbatim(q, imprint) and name_in(name, q) and self.role_ok(q)): continue
            profile = self.find_linkedin(name, company)
            if not profile: continue
            lead = {'name': name, 'current_role': sanitize(person.get('current_role')), 'company': company,
                    'linkedin_url': profile['url'], 'linkedin_text': profile['text'],
                    'evidence': [{'url': profile['url'], 'quote': profile['text'][:1200], 'source': 'search_snippet',
                                  'supports': ['name', 'linkedin_url']}]}
            leads.append(self.confirm(lead, f'https://{host}', [(f'https://{host}/impressum', imprint)]))
        return [l for l in leads if l]

    def find_linkedin(self, name, company):
        loc = next((l for l in self.locations if l), '')
        firm_word = next(iter(sorted(self.tokens(company), key=len, reverse=True)), '') if company else ''
        for query in (f'site:linkedin.com/in "{name}" {firm_word}'.strip(), f'site:linkedin.com/in "{name}" {loc}'.strip()):
            for r in self.p.serp(query, 5):
                url = linkedin_profile(r['url'])
                text = f"{r['title']}\n{r['description']}"
                if not url or profile_key(url) in self.seen or not name_in(name, r['title']): continue
                firm_hit = firm_word and firm_word in translit(norm(text))
                if firm_hit or (loc and norm(loc) in norm(text)):
                    return {'url': url, 'text': text}
        return None

    # -- orchestration
    def run(self):
        need = self.c.limit
        leads, stop = [], None
        try:
            self.stats['strategies'].append('linkedin_search')
            candidates = self.discover()
            for i in range(0, len(candidates), WORKERS * 2):
                self.meter.check()
                batch = [l for l in candidates[i:i + WORKERS * 2] if not self.company_blocked(None, l.get('company'))
                         and self.claim(l['linkedin_url'])]
                leads.extend(l for l in self.pool(self.confirm, batch) if self.accept(l))
                if self.qualified(leads) >= need: break
            if self.qualified(leads) < need:
                for lead in self.firm_first(need - self.qualified(leads)):
                    if self.claim(lead['linkedin_url']) and self.accept(lead): leads.append(lead)
        except Stop as s:
            stop = s.reason
        for lead in leads: self.stats['verification_counts'][lead['verification']] += 1
        leads.sort(key=lambda l: TIER_ORDER[l['verification']])
        qualified = [l for l in leads if TIER_ORDER[l['verification']] <= self.min_rank][:need]
        review = ([l for l in leads if TIER_ORDER[l['verification']] > self.min_rank] if self.c.include_review else [])
        stop = stop or ('target_reached' if len(qualified) >= need else 'search_exhausted')
        return qualified, review[:max(0, 100 - len(qualified))], stop

    def company_blocked(self, website, name):
        from registry import company_domain, company_name_key
        domain, name_key = company_domain(website), company_name_key(name)
        blocked = (domain and domain in self.blocked_domains) or (name_key and name_key in self.blocked_names)
        if blocked:
            with self.lock: self.stats['excluded_by_registry'] += 1
        return bool(blocked)

    def accept(self, lead):
        """Registry-blocked companies never; at most max_per_company counting leads per company and run."""
        if not lead or self.company_blocked(lead.get('company_website'), lead.get('company')): return False
        if TIER_ORDER[lead['verification']] > self.min_rank or not self.c.max_per_company: return True
        from registry import company_domain, company_name_key
        key = company_domain(lead.get('company_website')) or company_name_key(lead.get('company'))
        if not key: return True
        with self.lock:
            if self.company_counts.get(key, 0) >= self.c.max_per_company:
                self.stats['excluded_same_company'] += 1
                return False
            self.company_counts[key] = self.company_counts.get(key, 0) + 1
        return True

    def claim(self, url):
        key = profile_key(url)
        with self.lock:
            if key in self.seen: return False
            self.seen.add(key)
            return True

    def qualified(self, leads):
        return sum(TIER_ORDER[l['verification']] <= self.min_rank for l in leads)


class SearchBackend:
    """Worker-facing entry point; providers are injectable for tests."""
    def __init__(self, providers_factory=None):
        self.providers_factory = providers_factory or (lambda meter: Providers(
            meter, env_value('TREG_TOKEN'), openrouter_key(),
            env_value('DISCOVERY_SEARCH_MODEL') or None))

    def run(self, criteria, previous, cancelled, blocked=None):
        started = time.monotonic()
        meter = Meter(criteria.budget.max_cost_usd, started + criteria.budget.seconds, cancelled)
        pipeline = SearchPipeline(criteria, self.providers_factory(meter), meter, previous, blocked)
        qualified, review, stop = pipeline.run()
        if stop == 'cancelled': raise SearchError('cancelled')
        stats = {**pipeline.stats, **meter.counts, 'cost_usd': round(meter.cost_micro / 1_000_000, 6),
                 'elapsed_seconds': round(time.monotonic() - started, 2), 'stop_reason': stop}
        return qualified, review, stats


def main():
    """`python3 search_backend.py set-token`: store the treg token in HERMES_HOME/.env (masked prompt)."""
    import getpass
    if sys.argv[1:] != ['set-token']:
        print('usage: search_backend.py set-token', file=sys.stderr); return 2
    token = getpass.getpass('treg token (input hidden): ').strip()
    if not token or not treg_org(token):
        print('Token not recognised (no team in token); nothing stored.', file=sys.stderr); return 1
    save_env_value('TREG_TOKEN', token)
    print('TREG_TOKEN stored in HERMES_HOME/.env (mode 0600). Value not shown.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
