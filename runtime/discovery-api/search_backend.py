"""Search-engine discovery backend for source="search": no browser agent, no LinkedIn login.

Pipeline per job (all provider calls through treg.to, one token, pay per call), in rounds that
stop as soon as the target is reached:
 1. discover : rounds alternate between (a) Google results for
               site:linkedin.com/in <keyword> (<title> OR <titlein>) <location>  and (b) a people index
               (Exa people search: LinkedIn profiles with current positions), Google first by default;
               regions are searched through their cities (regions.py), facets add specialisations, and
               with a search memory (search_memory.py) repeat runs skip what was decided before
 2. classify : an LLM reads each profile text/snippet; name, role, firm and sector quotes are
               checked here to be literal substrings of that text; a role or firm the index dates
               as past only is rejected
 3. confirm  : the firm's own page (or imprint) is fetched; a decision-maker title next to the
               person's name makes the lead "verified", a one-person/namesake firm "plausible",
               role+firm proven only by the LinkedIn text (website silent) "linkedin",
               a website contradicting role or sector "review"
 4. firms    : if the target is still short, firm imprints in the location are mined for decision
               makers and their LinkedIn profiles are looked up (same confirmation; a profile found
               by name and place only, without the firm, is capped as a possible namesake)

Facts are accepted only as verbatim text of fetched sources (search snippets, profile index text,
firm pages). The LinkedIn association rests on the search/profile index, not on a logged-in profile
view. Failed provider calls are counted and reported, never mistaken for empty results. Secrets come
from the environment or HERMES_HOME/.env and are never logged, persisted in results or returned.
"""
import base64
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from threading import Lock
from urllib.parse import urlsplit

from models import Lead, profile_key, role_matches
from regions import expand_locations

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
MAX_FACETS = 8  # specialisations searched next to the sector term (queries only, never a check)
MAX_FIRMS = 40
SERP_DEPTH = 10  # Google has returned at most ~10 organic rows per query since Sept 2025
SERP_PER_ROUND = WORKERS * 2
# People index (Exa via treg): $0.007 per call up to 10 results, $0.001 per result beyond.
PEOPLE_ENDPOINT = 'exa.people.search'
PEOPLE_RESULTS = int(os.environ.get('DISCOVERY_PEOPLE_RESULTS', '25'))
PEOPLE_PER_ROUND = 3
MAX_PEOPLE_QUERIES = 24
PEOPLE_FAILURES_DISABLE = 2  # failed people-index calls before the job continues without it
# Payment required, or this many failed search/LLM calls in a row, stop the job as provider_error.
MAX_CONSECUTIVE_FAILURES = 5
# (seconds per attempt, attempts) per call kind. A firm page is one of several sources for a lead: it gets
# little time and one retry, so a hanging site cannot stall a whole confirmation batch.
TIMEOUTS = {'serp': (45, 3), 'people': (60, 2), 'extract': (30, 2), 'llm': (120, 3)}
TIMEOUT_KINDS = {'treg.google.serp.organic': 'serp', 'exa.people.search': 'people', 'treg.web.extract': 'extract'}
# Firm-first leads whose LinkedIn profile matched by name and place only (no firm): possible namesakes.
IDENTITY_CAP = {'place_sector': 'plausible', 'place': 'review'}
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
              'issuu.', 'slideshare.', 'github.', 'bloomberg.', 'owler.', 'apollo.io', 'lusha.', 'signalhire.',
              # other sectors: tax advisers, medical/dental, software/agencies, general directories
              'steuerberaterkammer', 'stbk-', 'bstbk.de', 'dstv.de', 'steuerberater-suchservice', 'steuerberaten.de',
              'doctolib.', 'dentolo.', 'sanego.', 'arzt-auskunft.', 'aerztekammer', 'zahnaerztekammer',
              'clutch.co', 'goodfirms.co', 'sortlist.', 'designrush.com', 'gulp.de', 'freelancermap.', 'malt.de',
              'malt.com', 'upwork.com', 'fiverr.com', 'capterra.', 'trustradius.', 'appvizer.', 'omr.com',
              'hotfrog.', 'yellowmap.', 'cybo.com', 'firmenabc.', 'moneyhouse.', 'implisense.', 'gelbe-seiten')
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
    def __init__(self, code, provider_errors=None):
        super().__init__(code)
        self.code = code
        self.provider_errors = provider_errors or {}  # failed calls by kind, for provider_error


class Stop(Exception):
    """Cooperative stop: cancelled, deadline, cost cap or provider failure. Accepted leads are kept."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ProviderFailure(Exception):
    """A provider call failed for good (after retries); `kind` is http_<status>, network or invalid_response."""
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


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


def name_line(name, pages):
    """(url, exact page line) with the person's first and last name, else None."""
    for url, text in pages:
        for line in text.splitlines():
            if len(line.strip()) >= 8 and name_in(name, line):
                return url, line.strip()[:1200]
    return None


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


def fold(s):
    """norm() without umlauts, so a word stem matches its inflections (Rechtsanwalt in Rechtsanwältin)."""
    return norm(s).replace('ä', 'a').replace('ö', 'o').replace('ü', 'u')


def firm_named(text, tokens, own):
    """The snippet names the firm, as whole words and not merely through the person's own name: a firm word
    that is not one of the person's name tokens, or for a namesake firm (Müller Rechtsanwälte for Thomas
    Müller) the shared token a second time, beside the name."""
    t = translit(norm(text))
    count = lambda tok: len(re.findall(r'(?<!\w)' + re.escape(tok) + r'(?!\w)', t))
    return any(count(tok) for tok in tokens - own) or any(count(tok) >= 2 for tok in tokens & own)


def feminine(title):
    """German feminine form of a job title (Partner -> Partnerin, Vertriebsleiter -> Vertriebsleiterin), else None."""
    if title.casefold() in FEMININE: return FEMININE[title.casefold()]
    return title + 'in' if re.search(r'(?:leiter|führer|inhaber|gründer|gesellschafter)$', title.casefold()) else None


def title_query(title):
    """Search term for one title: one query covers both forms, e.g. (Partner OR Partnerin)."""
    fem = feminine(title)
    return f'({title} OR {fem})' if fem else title


def people_row(result):
    """One people-index hit as candidate text: name, current positions, location and highlight lines.
    Positions the index dates as ended are kept apart (`past`) so a stale role cannot qualify."""
    url = linkedin_profile(result.get('url'))
    if not url: return None
    props = next((e.get('properties') or {} for e in result.get('entities') or []
                  if isinstance(e, dict) and e.get('type') == 'person'), {})
    current, past = [], []
    for w in props.get('workHistory') or []:
        if not isinstance(w, dict) or not isinstance(w.get('dates'), dict): continue  # undated: unknown
        company = w.get('company')
        company = company.get('name') if isinstance(company, dict) else company if isinstance(company, str) else None
        position = (sanitize(w.get('title')), sanitize(company))
        if not (position[0] or position[1]): continue
        if w['dates'].get('to') is None: current.append(position)
        else: past.append(position)
    name = sanitize(result.get('title') or props.get('name'))
    if not name: return None
    lines = [name] + [f'Aktuelle Position: {" – ".join(p for p in pos if p)}' for pos in current]
    if sanitize(props.get('location')): lines.append(f'Ort: {sanitize(props["location"])}')
    # Highlights are profile text; markdown links keep their visible text only.
    for h in result.get('highlights') or []:
        if not isinstance(h, str): continue
        text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', h)
        lines += [l.strip() for l in text.splitlines() if l.strip() and l.strip() != '...']
    text = ''
    for line in dict.fromkeys(lines):
        if len(text) + len(line) + 1 > 1200: break
        text += line + '\n'
    return {'url': url, 'text': text.strip(), 'current': current, 'past': past}


def stale(quote, field, row):
    """The quoted role (field 0) or firm (field 1) matches only positions the index dates as past."""
    q = norm(quote)
    hit = lambda positions: any(p[field] and len(q) >= 4 and (q in norm(p[field]) or norm(p[field]) in q) for p in positions)
    return hit(row.get('past') or []) and not hit(row.get('current') or [])


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
    """Job-wide cost, deadline, cancellation and provider-health guard shared by all worker threads."""
    def __init__(self, max_cost_usd, deadline, cancelled):
        self.limit_micro, self.deadline, self.cancelled = int(max_cost_usd * 1_000_000), deadline, cancelled
        self.lock, self.cost_micro = Lock(), 0
        self.counts = {'provider_calls': 0, 'llm_calls': 0}
        self.errors, self.failures, self.provider_down = {}, 0, False
        self.timing = {}  # call kind -> {'calls', 'seconds', 'max_seconds'}: where a job's time goes

    def timed(self, kind, seconds):
        with self.lock:
            t = self.timing.setdefault(kind, {'calls': 0, 'seconds': 0.0, 'max_seconds': 0.0})
            t['calls'] += 1
            t['seconds'] = round(t['seconds'] + seconds, 2)
            t['max_seconds'] = round(max(t['max_seconds'], seconds), 2)

    def charge(self, micro, kind):
        with self.lock:
            self.cost_micro += int(float(micro or 0))
            self.counts[kind] += 1

    def failure(self, kind, critical=True):
        """Count a failed call. Payment required anywhere, or MAX_CONSECUTIVE_FAILURES failed calls the
        pipeline cannot work without (search, LLM), stop the job; page fetches only count."""
        with self.lock:
            self.errors[kind] = self.errors.get(kind, 0) + 1
            if critical: self.failures += 1
            if kind.endswith('http_402') or self.failures >= MAX_CONSECUTIVE_FAILURES: self.provider_down = True
        if self.provider_down: raise Stop('provider_error')

    def success(self, critical=True):
        if critical:
            with self.lock: self.failures = 0

    def check(self):
        if self.cancelled(): raise Stop('cancelled')
        if self.provider_down: raise Stop('provider_error')
        if time.monotonic() >= self.deadline: raise Stop('timed_out')
        if self.cost_micro >= self.limit_micro: raise Stop('budget_exhausted')


def http_post(url, body, headers, timeout=90, retries=4, auth_fatal=True):
    """(JSON body, response headers). Rejected credentials raise SearchError when `auth_fatal`; any other
    final failure raises ProviderFailure, so a broken provider never looks like an empty result."""
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method='POST',
                                     headers={'Content-Type': 'application/json; charset=utf-8', **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), r.headers
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                retry = e.headers.get('Retry-After')
                time.sleep(min(30.0, float(retry)) if retry and retry.replace('.', '', 1).isdigit() else 2 ** attempt * 2)
                continue
            if e.code in (401, 403) and auth_fatal: raise SearchError('search_not_configured') from None
            raise ProviderFailure(f'http_{e.code}') from None
        except ValueError:
            if attempt < retries - 1: continue
            raise ProviderFailure('invalid_response') from None
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise ProviderFailure('network') from None


def header(headers, name):
    try: return headers.get(name) or headers.get(name.lower())
    except AttributeError: return None


class Providers:
    """treg (SERP, people index, page extraction) and OpenRouter (LLM). Replaceable in tests."""
    def __init__(self, meter, treg_token, openrouter_key, model=None):
        org = treg_org(treg_token)
        if not treg_token or not org or not openrouter_key: raise SearchError('search_not_configured')
        self.meter, self.model = meter, model or DEFAULT_MODEL
        self.treg_headers = {'X-Treg-Token': treg_token, 'X-Treg-Org': org, 'X-Treg-Route-Max-Cost': '0.01'}
        self.llm_headers = {'Authorization': 'Bearer ' + openrouter_key}
        # DISCOVERY_PEOPLE_SEARCH: unset/1 = rounds alternate starting with Google, first = people index first, 0 = off
        mode = os.environ.get('DISCOVERY_PEOPLE_SEARCH', '1')
        self.people_search, self.people_order = mode != '0', 'first' if mode == 'first' else 'after_google'
        self.people_failures = 0

    def _call(self, source, url, body, headers, timeout, critical=True, kind=None):
        """Response body or None after a failure counted as <source>_<kind> (e.g. treg_http_402: top up treg).
        Non-critical calls (page fetches, people index) treat rejected credentials as a failure of that
        call, not of the configuration. `timeout` is (seconds per attempt, attempts); no attempt runs past
        the job deadline, so one hanging page cannot hold a whole batch beyond it."""
        self.meter.check()
        seconds, attempts = timeout
        started = time.monotonic()
        try:
            r, h = http_post(url, body, headers, timeout=max(5.0, min(seconds, self.meter.deadline - started)),
                             retries=attempts, auth_fatal=critical)
        except ProviderFailure as f:
            self.meter.failure(f'{source}_{f.kind}', critical)
            return None, {}
        finally:
            self.meter.timed(kind or source, time.monotonic() - started)
        self.meter.success(critical)
        return (r if isinstance(r, dict) else {}), h

    def _treg(self, endpoint, body, headers=None, critical=True, estimate_micro=0):
        kind = TIMEOUT_KINDS.get(endpoint, 'extract')
        r, h = self._call('treg', TREG + endpoint, body, {**self.treg_headers, **(headers or {})}, TIMEOUTS[kind], critical, kind)
        if r is None:
            self.meter.charge(0, 'provider_calls')
            return None
        charged = (r.get('_treg') or {}).get('charged_micro')
        if charged is None: charged = header(h, 'X-Treg-Cost-Micro')
        if charged is None and isinstance(r.get('costDollars'), dict):
            charged = float(r['costDollars'].get('total') or 0) * 1_000_000
        self.meter.charge(estimate_micro if charged is None else charged, 'provider_calls')
        # Routed capabilities wrap provider rows in `output`; direct endpoints return the provider body.
        return r['output'] if isinstance(r.get('output'), dict) else r

    def serp(self, query, limit=SERP_DEPTH):
        out = self._treg('treg.google.serp.organic', {'q': query, 'country': 'de', 'language': 'de', 'limit': limit},
                         {'X-Treg-Route-Exclude': SERP_EXCLUDE} if SERP_EXCLUDE else None) or {}
        return [{'url': r.get('url') or r.get('link') or '', 'title': r.get('title') or '',
                 'description': r.get('snippet') or r.get('description') or ''}
                for r in out.get('results') or [] if isinstance(r, dict)][:limit]

    def people(self, query, limit=PEOPLE_RESULTS):
        """Exa people search: LinkedIn profiles with highlight lines and dated work history."""
        body = {'query': query, 'category': 'people', 'numResults': limit, 'includeDomains': ['linkedin.com'],
                'contents': {'highlights': True}}
        out = self._treg(PEOPLE_ENDPOINT, body, {'X-Treg-Route-Max-Cost': '0.2'}, critical=False,
                         estimate_micro=7000 + 1000 * max(0, limit - 10))
        if out is None:
            with self.meter.lock:
                self.people_failures += 1
                if self.people_failures >= PEOPLE_FAILURES_DISABLE: self.people_search = False
            return []
        return [row for row in map(people_row, (r for r in out.get('results') or [] if isinstance(r, dict))) if row]

    def extract(self, url):
        pages = (self._treg('treg.web.extract', {'url': url}, critical=False) or {}).get('pages') or []
        return (pages[0].get('markdown') or pages[0].get('content') or '') if pages else ''

    def llm(self, user):
        r, _ = self._call('openrouter', OPENROUTER, {'model': self.model, 'temperature': 0, 'response_format': {'type': 'json_object'},
                                       'reasoning': {'enabled': False}, 'usage': {'include': True},
                                       'messages': [{'role': 'system', 'content': EXTRACT_SYSTEM}, {'role': 'user', 'content': user}]},
                          self.llm_headers, TIMEOUTS['llm'], kind='llm')
        if r is None:
            self.meter.charge(0, 'llm_calls')
            return {}
        self.meter.charge(float((r.get('usage') or {}).get('cost') or 0) * 1_000_000, 'llm_calls')
        if not r.get('choices'):  # an error body with status 200 is a failed call, not an empty answer
            self.meter.failure('openrouter_invalid_response')
            return {}
        try:
            text = r['choices'][0]['message']['content'] or ''
            match = re.search(r'\{.*\}', text, re.S)
            return json.loads(match.group(0)) if match else {}
        except (TypeError, KeyError, IndexError, ValueError):
            return {}


# ---------------------------------------------------------------------------- pipeline

class SearchPipeline:
    def __init__(self, criteria, providers, meter, previous=(), blocked=None, memory=None):
        self.c, self.p, self.meter, self.memory = criteria, providers, meter, memory
        blocked = blocked or {}
        self.blocked_domains, self.blocked_names = set(blocked.get('domains', ())), set(blocked.get('names', ()))
        # exclude_previous_companies: every firm in the registry (also merely delivered) is skipped.
        known = criteria.exclude_previous_companies
        self.known_domains = set(blocked.get('known_domains', ())) if known else set()
        self.known_names = set(blocked.get('known_names', ())) if known else set()
        self.company_counts, self.company_names = {}, {}
        self.sector = list(criteria.keywords)
        self.extra_keywords, self.facets = [], []
        self.areas = [l for l in criteria.locations if l]  # as requested: the location check uses these
        places, self.regions = expand_locations(criteria.locations)
        self.locations = places or ['']  # searched places: regions resolved to their cities
        self.query_loc = {}  # query -> the place it searches
        self.default_titles = not criteria.titles
        self.base_titles = list(criteria.titles) or list(DEFAULT_TITLES)  # as requested, for the queries
        self.titles = list(self.base_titles)  # role check; LLM-proposed equivalents are added
        self.sector_words = {translit(t) for kw in self.sector for t in re.split(r'[^\w]+', norm(kw)) if t}
        self.seen = {profile_key(u) for u in previous}
        self.classified = set()  # profiles already read in this job (any strategy, accepted or not)
        self.origin = {}  # linkedin_url -> strategy that found the lead
        self.lock = Lock()
        self.min_rank = TIER_ORDER[criteria.min_verification]
        self.stats = {'candidate_count': 0, 'queries': 0, 'people_queries': 0, 'stale_rejected': 0, 'firms_checked': 0,
                      'expanded_keywords': [], 'expanded_titles': [], 'expanded_facets': [], 'regions': self.regions,
                      'excluded_by_registry': 0, 'excluded_known_company': 0, 'excluded_same_company': 0,
                      'skipped_known': 0, 'demoted_queries': 0,
                      'verification_counts': {t: 0 for t in TIER_ORDER}, 'strategies': [], 'qualified_by_strategy': {}}

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

    # -- 1+2: LinkedIn profiles from the people index and from search results, fetched in rounds
    def expand(self):
        """LLM-proposed wordings: related sector terms, SAME-role titles (other language/wording) and facets
        (specialisations searched next to the sector term, to reach other corners of it). Expanded titles are
        also accepted by the role check, so they must be true equivalents; facets never widen any check."""
        o = self.p.llm(
            f'Suggest LinkedIn search terms. Sector: {self.sector}. ' +
            (f'Target titles: {self.titles}. ' if not self.default_titles else '') +
            f'Return {{"keywords": [...], "titles": [...], "facets": [...]}} with at most {MAX_EXPANSIONS} keywords and '
            f'titles and {MAX_FACETS} facets. keywords: other words people in this sector use in their LinkedIn headline '
            'or company name for the same kind of firm (German and English, synonyms; no places, no job titles). '
            'titles: ONLY exact equivalents of the target titles in other wording or language (e.g. "Head of Sales" -> '
            '"Vertriebsleiter", "Sales Director"); no broader or different roles; empty list if no target titles were '
            'given. facets: specialisations or fields inside this sector, one or two words each (e.g. for lawyers '
            '"Arbeitsrecht", "Familienrecht"; for software firms "SaaS", "App-Entwicklung").')
        clean = lambda values, known, n: [v for v in dict.fromkeys(sanitize(x, 60) for x in (values or []) if isinstance(x, str))
                                          if v and norm(v) not in {norm(k) for k in known}][:n]
        keywords = clean(o.get('keywords'), self.sector, MAX_EXPANSIONS)
        titles = clean(o.get('titles'), self.titles, MAX_EXPANSIONS) if not self.default_titles else []
        facets = clean(o.get('facets'), self.sector + keywords, MAX_FACETS)
        self.stats['expanded_keywords'], self.stats['expanded_titles'], self.stats['expanded_facets'] = keywords, titles, facets
        return keywords, titles, facets

    def planned(self, queries, places, cap):
        """Unique queries tagged with their place; earlier runs' spent queries last (memory), then capped."""
        plan = {}
        for q, loc in zip(queries, places):
            plan.setdefault(q, loc)
        ordered = list(plan)
        if self.memory:
            ordered, demoted = self.memory.order(ordered)
            self.stats['demoted_queries'] += demoted
        self.query_loc.update(plan)
        return ordered[:cap]

    def people_queries(self):
        """Free-text people-index queries: requested roles, sector term (then facets), place. A region is asked
        as a whole first (the index understands "Nordrhein-Westfalen"), then city by city."""
        roles = self.base_titles
        role_text = ', '.join(roles[:-1]) + ' oder ' + roles[-1] if len(roles) > 1 else roles[0]
        places = self.regions + [l for l in self.locations if l] or ['Deutschland']
        terms = [f'im Bereich {kw}' for kw in self.sector + self.extra_keywords] + \
                [f'im Bereich {kw}, Schwerpunkt {f}' for f in self.facets for kw in self.sector]
        pairs = [(f'{role_text} {term} in {loc}', loc) for term in terms for loc in places]
        return self.planned([q for q, _ in pairs], [l for _, l in pairs], MAX_PEOPLE_QUERIES)

    def serp_queries(self, extra_titles):
        """Originals first, then expanded titles, expanded sector terms and facets; within each, places take
        turns, so neither the round order nor the MAX_QUERIES cap starves a place or a term."""
        base, more = [title_query(t) for t in self.base_titles], [title_query(t) for t in extra_titles]
        pairs = ([(kw, t) for kw in self.sector for t in base] + [(kw, t) for kw in self.sector for t in more] +
                 [(kw, t) for t in base + more for kw in self.extra_keywords] +
                 [(f'{kw} {f}', t) for t in base for f in self.facets for kw in self.sector])
        rows = [(f'site:linkedin.com/in {kw} {t} {loc}'.strip(), loc) for kw, t in pairs for loc in self.locations]
        return self.planned([q for q, _ in rows], [l for _, l in rows], MAX_QUERIES)

    def from_people(self, queries, remaining):
        self.stats['people_queries'] += len(queries)
        results = min(PEOPLE_RESULTS, max(10, 3 * remaining))  # 10 results cost the same as fewer
        batches = self.pool(lambda q: self.p.people(q, results), queries)
        return self.classify_new([{**r, 'source': 'people_index', 'query': q} for q, batch in zip(queries, batches) for r in batch])

    def from_serp(self, queries, remaining=None):
        self.stats['queries'] += len(queries)
        batches = self.pool(lambda q: self.p.serp(q, SERP_DEPTH), queries)
        return self.classify_new([{'url': linkedin_profile(r['url']), 'text': f"{r['title']}\n{r['description']}",
                                   'source': 'search_snippet', 'query': q} for q, batch in zip(queries, batches) for r in batch])

    def place_of(self, text, query):
        """The searched place the profile text names, else the place its query searched."""
        t = norm(text)
        return next((l for l in self.locations if l and re.search(r'(?<!\w)' + re.escape(norm(l)) + r'(?!\w)', t)),
                    self.query_loc.get(query, ''))

    def classify_new(self, rows):
        """Classify profiles not delivered before, not decided in an earlier run with the same criteria
        (memory) and not yet read in this job (by any strategy). Each query is noted with its new results."""
        items, new = {}, {}
        with self.lock:
            for r in rows:
                key = profile_key(r['url']) if r['url'] else None
                if not key or key in self.seen or key in self.classified: continue
                if self.memory and self.memory.known('profile', key):
                    self.stats['skipped_known'] += 1
                    self.classified.add(key)
                    continue
                if key not in items:
                    items[key] = {**r, 'linkedin_url': r['url'], 'loc': self.place_of(r['text'], r['query'])}
                new.setdefault(r['query'], set()).add(key)
            self.classified.update(items)
        if self.memory:
            for q in {r['query'] for r in rows}: self.memory.note(q, 'profile', new.get(q, ()))
        items = list(items.values())
        self.stats['candidate_count'] += len(items)
        chunks = [items[i:i + 8] for i in range(0, len(items), 8)]
        return [lead for batch in self.pool(self.classify, chunks) for lead in batch]

    def decide(self, kind, value):
        if self.memory: self.memory.decide(kind, value)

    def classify(self, chunk):
        areas = self.areas
        user = (f'Target: {self.target_roles()} at companies in the sector described by {self.sector}' +
                (f', located in or near one of {areas}' + (' (a state or region includes all its towns)' if self.regions else '')
                 if areas else '') + '. '
                'For EACH item return an object with keys: index, name, current_role, company, is_decision_maker (bool), '
                'in_sector (bool), in_location (bool or null), quote_name, quote_role, quote_company, quote_sector. '
                'is_decision_maker means: the current role is one of the target roles. in_sector means the COMPANY itself '
                '(current employer) belongs to that sector; a person with a matching qualification who works for a business '
                'of another kind is in_sector=false. Lines "Aktuelle Position: ..." are the current positions; roles the '
                "text dates as ended are not current. Quotes must be exact substrings of that item's text. "
                'Return {"items": [...]}.\n\n' +
                '\n'.join(f'[{i}] {it["text"]}' for i, it in enumerate(chunk)))
        leads = []
        for o in self.p.llm(user).get('items') or []:
            try: it = chunk[int(o.get('index'))]
            except (TypeError, ValueError, IndexError): continue
            lead = self.qualify(o, it)
            if lead: leads.append(lead)
            else: self.decide('profile', profile_key(it['linkedin_url']))  # rejected: not read again for these criteria
        return leads

    def qualify(self, o, it):
        """The candidate lead from one classified item, or None."""
        src = it['text']
        if not (o.get('is_decision_maker') and o.get('in_sector') and o.get('in_location') is not False): return None
        quotes = {'name': o.get('quote_name'), 'current_role': o.get('quote_role'),
                  'company': o.get('quote_company'), 'sector': o.get('quote_sector')}
        if not all(verbatim(q, src) for q in quotes.values()): return None
        if stale(quotes['current_role'], 0, it) or stale(quotes['company'], 1, it):
            with self.lock: self.stats['stale_rejected'] += 1
            return None  # the index dates this role or firm as past
        name = sanitize(o.get('name'))
        if not (name and name_in(name, src) and self.role_ok(o.get('quote_role') or '')): return None
        evidence = [{'url': it['linkedin_url'], 'quote': src.strip()[:1200], 'source': it.get('source', 'search_snippet'),
                     'supports': ['name', 'linkedin_url', 'current_role', 'company', 'sector']}]
        return {'name': name, 'current_role': sanitize(o.get('current_role')), 'company': sanitize(o.get('company')),
                'linkedin_url': it['linkedin_url'], 'linkedin_text': src, 'loc': it.get('loc', ''), 'evidence': evidence}

    # -- 3: confirmation on the firm's own website
    def firm_pages(self, lead):
        name = ' '.join(t.capitalize() for t in name_key(lead['name']))
        firm = clean_company(lead.get('company'))
        loc = lead.get('loc') or next((l for l in self.locations if l), '')  # the place this lead was found in
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
        quotes = (('quote_role', ['name', 'current_role']), ('quote_firm', ['company']), ('quote_sector', ['sector'])) + \
                 ((('quote_solo', ['company']),) if solo else ())
        for key, supports in quotes:
            if ok(o.get(key)):
                url = page_of(o[key])
                # The whole source line; else the quote itself (verbatim up to case/whitespace), never dropped silently.
                quote = (source_quote(o[key], dict(pages).get(url, corpus), anchor=surname(lead['name']) if key == 'quote_role' else None)
                         or (sanitize(o[key], 1200) if len(o[key].strip()) >= 8 else None))
                if quote: evidence.append({'url': url, 'quote': quote, 'source': 'firm_website', 'supports': supports})
        if near_title:
            evidence.append({'url': page_of(near_title), 'quote': near_title[:1200], 'source': 'firm_website', 'supports': ['current_role']})
        named = name_line(lead['name'], pages) if on_site else None
        if named and not any(named[1] == e['quote'] for e in evidence):
            evidence.append({'url': named[0], 'quote': named[1], 'source': 'firm_website', 'supports': ['name']})
        site = lambda field: any(e.get('source') == 'firm_website' and field in e['supports'] for e in evidence)
        lead = {**lead, 'company': firm_name or clean_company(lead.get('company')), 'company_website': website,
                'role_on_site': sanitize(o.get('role_on_site')) if (role_quoted or near_title) else None, 'evidence': evidence}
        if (o.get('listed_in_other_role') and not role_ok) or non_partner or o.get('firm_in_sector') is False:
            return self.finish(lead, 'review', 'company website contradicts the role or sector')
        # A website tier needs the website facts it rests on in the evidence, not only in the LLM's reading.
        if firm_ok and on_site and role_ok and site('current_role'):
            return self.finish(lead, 'verified', 'target title next to the name on the company website')
        if firm_ok and on_site and (namesake or solo) and site('name') and site('company'):
            return self.finish(lead, 'plausible', 'one-person or namesake company; title not stated on the website')
        return self.finish(lead, *self.snippet_only(lead, 'target title not confirmed on the company website'))

    @staticmethod
    def snippet_only(lead, reason):
        """Website silent (not contradicting): the LinkedIn text alone proved role and firm -> tier "linkedin"."""
        proven = any(e.get('source') in ('search_snippet', 'people_index') and {'current_role', 'company'} <= set(e['supports'])
                     for e in lead['evidence'])
        return ('linkedin', 'role and company only in the LinkedIn snippet or profile index; ' + reason) if proven else ('review', reason)

    def finish(self, lead, tier, reason):
        cap = IDENTITY_CAP.get(lead.get('identity_match'))
        if cap and TIER_ORDER[tier] < TIER_ORDER[cap]:
            tier, reason = cap, 'LinkedIn profile found by name and place only (possible namesake); ' + reason
        reason = reason[:160]
        firm = clean_company(lead.get('company'))
        snippet_firm = firm_named(lead.get('linkedin_text') or '', self.tokens(firm), {translit(t) for t in name_key(lead['name'])})
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
        rows = [(f'{kw} {loc} {suffix}'.strip(), loc) for loc in self.locations for kw in self.sector for suffix in ('Impressum', 'Team')]
        queries = self.planned([q for q, _ in rows], [l for _, l in rows], MAX_QUERIES)
        hosts, skipped = {}, set()  # host -> (url, place of its query); hosts passed over once
        for q, batch in zip(queries, self.pool(lambda q: self.p.serp(q, 10), queries)):
            new = set()
            for r in batch:
                host = (urlsplit(r['url']).hostname or '').removeprefix('www.')
                if (not host or not r['url'].startswith('https://') or any(s in host for s in SKIP_HOSTS)
                        or host in hosts or host in skipped): continue
                if self.memory and self.memory.known('firm', host):  # imprint read in an earlier run with these criteria
                    self.stats['skipped_known'] += 1
                    skipped.add(host)
                    continue
                if self.company_blocked(f'https://{host}', None):  # excluded firm: no imprint paid for
                    skipped.add(host)
                    continue
                hosts[host] = (r['url'], self.query_loc.get(q, ''))
                new.add(host)
            if self.memory: self.memory.note(q, 'firm', new)
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
        host, (url, loc) = item
        imprint = self.p.extract(url if 'impressum' in url.lower() else f'https://{host}/impressum')[:15000]
        if len(imprint) < 100: return []
        user = (f'Sector of interest: {self.sector}. Target: {self.target_roles()}. From this company imprint/team text, '
                'return {"in_sector": bool, "quote_sector": str, "company": str, "quote_company": str, "people": '
                '[{"name": str, "current_role": str, "quote_name_role": str}]}. people: only persons in a target role '
                '(imprints usually name owners, managing directors, board members or representing partners). '
                'quote_name_role is ONE exact substring containing the name and the role. Text:\n\n' + imprint)
        o = self.p.llm(user)
        if o: self.decide('firm', host)  # imprint read: not again for these criteria
        if not o.get('in_sector') or not verbatim(o.get('quote_sector'), imprint): return []
        company = sanitize(o.get('company')) if o.get('company') and verbatim(o.get('quote_company'), imprint) else None
        leads = []
        for person in (o.get('people') or [])[:4]:
            name, q = sanitize(person.get('name')), person.get('quote_name_role')
            if not (name and verbatim(q, imprint) and name_in(name, q) and self.role_ok(q)): continue
            profile = self.find_linkedin(name, company, loc)
            if not profile: continue
            lead = {'name': name, 'current_role': sanitize(person.get('current_role')), 'company': company, 'loc': loc,
                    'linkedin_url': profile['url'], 'linkedin_text': profile['text'], 'identity_match': profile['match'],
                    'evidence': [{'url': profile['url'], 'quote': profile['text'][:1200], 'source': 'search_snippet',
                                  'supports': ['name', 'linkedin_url']}]}
            leads.append(self.confirm(lead, f'https://{host}', [(f'https://{host}/impressum', imprint)]))
        return [l for l in leads if l]

    def find_linkedin(self, name, company, loc=''):
        """Profile with the name in its title plus the firm (match "firm") or only the place `loc` (where the
        firm was found) in the snippet ("place_sector" when a sector term is there too, else "place"): the
        latter two may be namesakes."""
        tokens, own = (self.tokens(company) if company else set()), {translit(t) for t in name_key(name)}
        # Query with a firm word that is not part of the person's own name, if the firm has one.
        firm_word = (sorted(tokens - own, key=len, reverse=True) or sorted(tokens, key=len, reverse=True) or [''])[0]
        by_place = None
        for query in (f'site:linkedin.com/in "{name}" {firm_word}'.strip(), f'site:linkedin.com/in "{name}" {loc}'.strip()):
            for r in self.p.serp(query, 5):
                url = linkedin_profile(r['url'])
                text = f"{r['title']}\n{r['description']}"
                if not url or profile_key(url) in self.seen or not name_in(name, r['title']): continue
                if firm_named(text, tokens, own):
                    return {'url': url, 'text': text, 'match': 'firm'}
                if not by_place and loc and norm(loc) in norm(text):
                    by_place = {'url': url, 'text': text, 'match': 'place_sector' if self.sector_in(text) else 'place'}
        return by_place

    def sector_in(self, text):
        t = fold(text)
        return any(fold(k) in t for k in self.sector + self.extra_keywords)

    # -- orchestration
    def run(self):
        need = self.c.limit
        leads, stop = [], None
        try:
            self.extra_keywords, extra_titles, self.facets = self.expand() if self.c.expand_terms else ([], [], [])
            self.titles = self.titles + extra_titles  # equivalents count for the role check, too
            serp = [['linkedin_search', self.serp_queries(extra_titles), self.from_serp, 0]]
            people = ([['people_search', self.people_queries(), self.from_people, 0]]
                      if getattr(self.p, 'people_search', False) else [])
            # Google rounds are cheap and suffice where firms publish their teams; the people index fills
            # thin sectors. Rounds alternate, so neither waits until the other has used up the deadline.
            self.alternate(people + serp if getattr(self.p, 'people_order', 'after_google') == 'first' else serp + people,
                           leads, need)
            if self.qualified(leads) < need:
                for lead in self.firm_first(need - self.qualified(leads)):
                    if self.claim(lead['linkedin_url']) and self.accept(lead):
                        leads.append(lead)
                        self.origin[lead['linkedin_url']] = 'firm_websites'
                        if not self.counts(lead): self.decide('profile', profile_key(lead['linkedin_url']))
        except Stop as s:
            stop = s.reason
        for lead in leads: self.stats['verification_counts'][lead['verification']] += 1
        leads.sort(key=lambda l: TIER_ORDER[l['verification']])
        qualified = [l for l in leads if self.counts(l)][:need]
        # Delivered leads are decided; counting leads beyond the limit stay open and come back next time.
        for lead in qualified: self.decide('profile', profile_key(lead['linkedin_url']))
        if self.memory: self.memory.save()  # also after a stop: what was run and decided stays true
        for lead in qualified:
            strategy = self.origin.get(lead['linkedin_url'], 'linkedin_search')
            self.stats['qualified_by_strategy'][strategy] = self.stats['qualified_by_strategy'].get(strategy, 0) + 1
        review = ([l for l in leads if TIER_ORDER[l['verification']] > self.min_rank] if self.c.include_review else [])
        stop = stop or ('target_reached' if len(qualified) >= need else 'search_exhausted')
        return qualified, review[:max(0, 100 - len(qualified))], stop

    def alternate(self, stages, leads, need):
        """One round per strategy in turn ([strategy, queries, fetch, cursor]) until the target is reached
        or every strategy has used its queries."""
        progressed = True
        while progressed and self.qualified(leads) < need:
            progressed = False
            for stage in stages:
                strategy, queries, fetch, i = stage
                if i >= len(queries) or self.qualified(leads) >= need: continue
                if strategy == 'people_search' and not self.p.people_search: continue  # index unavailable this job
                self.meter.check()
                if strategy not in self.stats['strategies']: self.stats['strategies'].append(strategy)
                remaining = need - self.qualified(leads)
                # People-index calls are priced per result: size them to what is still missing.
                size = max(1, min(PEOPLE_PER_ROUND, -(-remaining // 8))) if strategy == 'people_search' else SERP_PER_ROUND
                stage[3] = i + size
                self.confirm_all(self.spread(fetch(queries[i:i + size], remaining)), leads, need, strategy)
                progressed = True

    @staticmethod
    def spread(candidates):
        """Candidates taking turns by place (Köln, Düsseldorf, Dortmund, …), so the leads confirmed first are
        not all from the place searched first."""
        groups = {}
        for c in candidates: groups.setdefault(c.get('loc') or '', []).append(c)
        return [c for row in zip_longest(*groups.values()) for c in row if c]

    def confirm_all(self, candidates, leads, need, strategy):
        """Confirm candidates on firm websites in batches that shrink near the target (no confirming 12 for 1)."""
        i = 0
        while i < len(candidates) and self.qualified(leads) < need:
            self.meter.check()
            size = max(2, min(WORKERS * 2, 2 * (need - self.qualified(leads))))
            batch = [l for l in candidates[i:i + size] if not self.company_blocked(None, l.get('company'))
                     and not self.company_full(l.get('company')) and self.claim(l['linkedin_url'])]
            i += size
            for found, lead in zip(batch, self.pool(self.confirm, batch)):
                accepted = self.accept(lead, found.get('company'))
                if accepted:
                    leads.append(lead)
                    self.origin[lead['linkedin_url']] = strategy
                if lead is None or accepted and not self.counts(lead):  # checked and below the target tier: decided
                    self.decide('profile', profile_key(found['linkedin_url']))

    def company_full(self, name):
        """A firm already holds max_per_company counting leads (by name, before any paid confirmation)."""
        from registry import company_name_key
        key = company_name_key(clean_company(name)) if self.c.max_per_company else None
        if not key: return False
        with self.lock:
            if self.company_names.get(key, 0) < self.c.max_per_company: return False
            self.stats['excluded_same_company'] += 1
        return True

    def company_blocked(self, website, name):
        """Firm with a blocking registry status, or (exclude_previous_companies) any firm in the registry."""
        from registry import company_domain, company_name_key
        domain, key = company_domain(website), company_name_key(name)
        hit = lambda domains, names: bool((domain and domain in domains) or (key and key in names))
        stat = ('excluded_by_registry' if hit(self.blocked_domains, self.blocked_names) else
                'excluded_known_company' if hit(self.known_domains, self.known_names) else None)
        if stat:
            with self.lock: self.stats[stat] += 1
        return bool(stat)

    def accept(self, lead, found_company=None):
        """Registry-blocked companies never; at most max_per_company counting leads per company and run.
        `found_company` is the firm name the candidate was found with (recorded for company_full)."""
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
            for name in {company_name_key(lead.get('company')), company_name_key(clean_company(found_company))} - {None, ''}:
                self.company_names[name] = self.company_names.get(name, 0) + 1
        return True

    def claim(self, url):
        key = profile_key(url)
        with self.lock:
            if key in self.seen: return False
            self.seen.add(key)
            return True

    def counts(self, lead):
        """The lead's tier counts toward the limit (min_verification or better)."""
        return TIER_ORDER[lead['verification']] <= self.min_rank

    def qualified(self, leads):
        return sum(self.counts(l) for l in leads)


class SearchBackend:
    """Worker-facing entry point; providers are injectable for tests."""
    def __init__(self, providers_factory=None):
        self.providers_factory = providers_factory or (lambda meter: Providers(
            meter, env_value('TREG_TOKEN'), openrouter_key(),
            env_value('DISCOVERY_SEARCH_MODEL') or None))

    def run(self, criteria, previous, cancelled, blocked=None, memory=None):
        """`memory`: a search_memory.MemoryView for these criteria, or None (no cross-run memory)."""
        started = time.monotonic()
        meter = Meter(criteria.budget.max_cost_usd, started + criteria.budget.seconds, cancelled)
        pipeline = SearchPipeline(criteria, self.providers_factory(meter), meter, previous, blocked, memory)
        qualified, review, stop = pipeline.run()
        if stop == 'cancelled': raise SearchError('cancelled')
        if stop == 'provider_error' and not (qualified or review): raise SearchError('provider_error', dict(meter.errors))
        stats = {**pipeline.stats, **meter.counts, 'provider_errors': dict(meter.errors), 'call_seconds': dict(meter.timing),
                 'cost_usd': round(meter.cost_micro / 1_000_000, 6),
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
