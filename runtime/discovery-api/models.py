"""Strict request/result contracts; all URLs are untrusted input."""
import ipaddress
import json
import re
from typing import Annotated, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

Text = Annotated[str, Field(strict=True, min_length=1, max_length=160, pattern=r'^[^\x00-\x1f\x7f]+$')]


def public_url(value: str) -> str:
    if len(value) > 2048 or re.search(r'[\s\\\x00-\x1f]', value):
        raise ValueError('invalid URL')
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('HTTPS public URL required')
    host = p.hostname.lower().rstrip('.')
    if re.fullmatch(r'[0-9.]+', host):
        try:
            if not ipaddress.ip_address(host).is_global: raise ValueError('private address forbidden')
        except ValueError:
            raise ValueError('nonpublic or ambiguous address') from None
    if '.' not in host or host.endswith(('.local', '.internal', '.localhost', '.test', '.invalid', '.example')) or host == 'localhost':
        raise ValueError('public hostname required')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError('private address forbidden')
    return value


def profile_url(value: str) -> str:
    public_url(value)
    p = urlsplit(value)
    if p.netloc != 'www.linkedin.com' or not re.fullmatch(r'/in/[\w\-]+/?', unquote(p.path)) or p.query or p.fragment:
        raise ValueError('canonical LinkedIn profile URL required')
    return value


def profile_key(value: str) -> str:
    return 'https://www.linkedin.com' + unquote(urlsplit(value).path).rstrip('/').casefold()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Observation(str):
    """Exact transient tool text with origin; never stored in API results."""
    def __new__(cls, text, tool_name):
        obj = super().__new__(cls, text)
        obj.tool_name = tool_name
        return obj


class Budget(Strict):
    seconds: int = Field(default=180, ge=15, le=1800)
    max_turns: int = Field(default=12, ge=1, le=120)
    max_cost_usd: float = Field(default=0.5, gt=0, le=10)  # source=search: provider + LLM spend cap per job


class Criteria(Strict):
    keywords: list[Text] = Field(default_factory=list, max_length=12)
    titles: list[Text] = Field(default_factory=list, max_length=12, validation_alias=AliasChoices('titles', 'roles'))
    geo_urns: list[str] = Field(default_factory=list, max_length=12)
    current_company_ids: list[str] = Field(default_factory=list, max_length=12)
    network: list[Literal['F', 'S', 'O']] = Field(default_factory=list, max_length=3)
    limit: int = Field(default=15, ge=1, le=100)
    source: Literal['linkedin', 'web', 'search'] = 'linkedin'
    locations: list[Text] = Field(default_factory=list, max_length=12)  # source=search: plain place names
    include_review: bool = False  # source=search: also return leads below min_verification
    # source=search: lowest tier that counts toward limit (verified > plausible > linkedin > review).
    min_verification: Literal['verified', 'plausible', 'linkedin'] = 'plausible'
    expand_terms: bool = True  # source=search: LLM adds related sector terms and equivalent title wordings
    max_per_company: int | None = Field(default=1, ge=1, le=100)  # source=search: counting leads per company; null = no cap
    exclude_previous: bool = True
    search_url: str | None = Field(default=None, max_length=4096)
    budget: Budget = Field(default_factory=Budget)

    @field_validator('geo_urns')
    @classmethod
    def urns(cls, values):
        if any(not re.fullmatch(r'(?:urn:li:geo:)?[0-9]{1,20}', v) for v in values):
            raise ValueError('numeric geo ID or urn:li:geo:<ID> required')
        return values

    @field_validator('current_company_ids')
    @classmethod
    def ids(cls, values):
        if any(not re.fullmatch(r'[0-9]{1,20}', v) for v in values):
            raise ValueError('numeric company IDs required')
        return values

    @field_validator('search_url')
    @classmethod
    def search(cls, value):
        if value is None:
            return value
        public_url(value)
        p = urlsplit(value)
        if p.netloc != 'www.linkedin.com' or p.path != '/search/results/people/' or p.fragment:
            raise ValueError('LinkedIn people search URL required')
        allowed = {'keywords', 'title', 'geoUrn', 'currentCompany', 'network', 'origin', 'sid', 'page'}
        pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
        if len(pairs) > 12 or len({k for k, _ in pairs}) != len(pairs):
            raise ValueError('duplicate or excessive parameters')
        for k, v in pairs:
            if k not in allowed or len(v) > 1000 or re.search(r'[\x00-\x1f]', v):
                raise ValueError('unsupported search parameter')
            if k == 'page' and (not re.fullmatch(r'[1-9][0-9]?', v) or int(v) > 10):
                raise ValueError('page must be 1..10')
            if k in {'geoUrn', 'currentCompany', 'network'}:
                values = json.loads(v)
                if not isinstance(values, list) or len(values) > 12 or any(not isinstance(x, str) for x in values):
                    raise ValueError('invalid filter array')
                pattern = r'(?:urn:li:geo:)?[0-9]{1,20}' if k == 'geoUrn' else r'[0-9]{1,20}'
                if any((x not in {'F', 'S', 'O'} if k == 'network' else not re.fullmatch(pattern, x)) for x in values):
                    raise ValueError('invalid filter value')
        return value  # supplied valid URL preserved byte-for-byte

    @model_validator(mode='after')
    def coherent(self):
        if self.search_url and (self.source != 'linkedin' or any([self.keywords, self.titles, self.geo_urns, self.current_company_ids, self.network])):
            raise ValueError('search_url cannot be combined with filters or web source')
        if not self.search_url and not any([self.keywords, self.titles, self.geo_urns, self.current_company_ids]):
            raise ValueError('at least one search criterion required')
        if self.source == 'search':
            if not self.keywords:
                raise ValueError('search source requires keywords describing the sector')
            if any([self.geo_urns, self.current_company_ids, self.network]):
                raise ValueError('search source takes locations, not LinkedIn filter IDs')
        else:
            if (self.locations or self.include_review or self.min_verification != 'plausible' or not self.expand_terms
                    or self.max_per_company != 1):  # compare values: stored jobs re-validate with every field set
                raise ValueError('locations, include_review, min_verification, expand_terms and max_per_company require source=search')
            if self.limit > 30:
                raise ValueError('limit above 30 requires source=search')
        return self

    def target_url(self):
        if self.search_url:
            return self.search_url
        params = {}
        if self.keywords: params['keywords'] = ' '.join(self.keywords)
        if self.titles: params['title'] = ' OR '.join(self.titles)
        for key, val in [('geoUrn', self.geo_urns), ('currentCompany', self.current_company_ids), ('network', self.network)]:
            if val: params[key] = json.dumps(val, separators=(',', ':'))
        return 'https://www.linkedin.com/search/results/people/?' + urlencode(params)


class Evidence(Strict):
    url: str = Field(max_length=2048)
    quote: str = Field(min_length=8, max_length=1200)
    supports: list[Literal['name', 'current_role', 'company', 'company_website', 'linkedin_url', 'sector']] = Field(min_length=1, max_length=6)
    source: Literal['search_snippet', 'firm_website'] | None = None  # set by source=search only
    _url = field_validator('url')(public_url)


class Lead(Strict):
    name: Text
    current_role: Text | None = None
    company: Text | None = None
    company_website: str | None = Field(default=None, max_length=2048)
    linkedin_url: str = Field(max_length=2048)
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    identity_status: Literal['verified', 'needs_review']
    missing_information: list[Text] = Field(default_factory=list, max_length=12)
    fit_reason: Text | None = None  # inference, never evidence
    # source=search only: how far the firm's own website confirmed the decision-maker role.
    verification: Literal['verified', 'plausible', 'linkedin', 'review'] | None = None
    verification_reason: Text | None = None
    _profile = field_validator('linkedin_url')(profile_url)

    @field_validator('company_website')
    @classmethod
    def website(cls, value):
        return public_url(value) if value else value


class DiscoveryResult(Strict):
    # Adapter-only accounting; not accepted from model JSON or emitted as lead data.
    _stats: dict = PrivateAttr(default_factory=dict)
    _candidate_verdicts: list = PrivateAttr(default_factory=list)
    outcome: Literal['completed', 'partial', 'blocked']
    leads: list[Lead] = Field(default_factory=list, max_length=30)
    blocked_reason: Literal['login_required', 'challenge', 'access_limit', 'access_denied'] | None = None

    @model_validator(mode='after')
    def blocked(self):
        if self.outcome == 'blocked' and not self.blocked_reason:
            raise ValueError('blocked reason required')
        if self.outcome != 'blocked' and self.blocked_reason:
            raise ValueError('unexpected blocked reason')
        return self


def role_matches(role, titles):
    """Literal requested roles plus explicit German inflections; no guessed synonyms."""
    if not titles: return True
    if not role: return False
    value = role.casefold()

    variants = {
        'geschäftsführer': r'geschäftsführ(?:er(?:in|innen)?|end(?:e[rmns]?)?)',
        'inhaber': r'(?:mit[- ]?)?inhaber(?:in|innen)?',
        'partner': r'partner(?:in|innen|s)?',
    }
    for title in titles:
        term = title.casefold()
        # Any German job title also matches its feminine forms (Vertriebsleiter -> Vertriebsleiterin).
        pattern = variants.get(term, re.escape(term) + r'(?:in|innen)?')
        if term in variants and re.search(r'\b(?:assisten\w*|sekretär\w*|referent\w*)\b', value):
            continue
        if term == 'partner' and re.search(r'\b(?:business|hr|human resources)\s+partner\b|\bpartner\s+manager\b', value):
            continue  # Explicit ambiguous employment titles, not firm partnership proof.
        if re.search(r'(?<!\w)' + pattern + r'(?!\w)', value): return True
    return False


def scope_matches(lead, keywords):
    # The agent judges sector fit against the requested keywords (no hard-coded sector
    # vocabulary) but must back it with an observed verbatim quote marked 'sector'.
    # Only already-validated evidence reaches here, so the quote is real; its relevance
    # remains an agent judgement shown to the human reviewer.
    if not keywords: return True
    return any('sector' in e['supports'] for e in lead['evidence'])


def accepted_leads(result, observations, limit, previous, titles=(), keywords=()):
    accepted, seen = [], {profile_key(p) for p in previous}
    for lead in result.leads:
        data, verdict, _ = evaluate_lead(lead, observations, seen, titles, keywords)
        if verdict != 'accepted': continue
        accepted.append(data)
        seen.add(profile_key(lead.linkedin_url))
        if len(accepted) >= limit: break
    return accepted


def evaluate_lead(lead, observations, previous=(), titles=(), keywords=()):
    """Single source of truth for qualification and exact-evidence repair gaps."""
    key = profile_key(lead.linkedin_url)
    if key in previous: return None, 'duplicate', []
    valid = [e for e in lead.evidence if any(e.quote in o and
             re.search(r'(?<![\w/])' + re.escape(e.url) + r'''(?=$|[\s"'<>)\]}])''', o.replace(e.quote, ''))
             for o in observations)]
    profile_proof = any(e.url.rstrip('/') == lead.linkedin_url.rstrip('/') and
               lead.name in e.quote and {'name', 'linkedin_url'} <= set(e.supports) and
               any(getattr(o, 'tool_name', 'web_extract') in
                   {'web_extract', 'browser_navigate', 'browser_snapshot', 'browser_scroll', 'browser_back'} and
                   e.quote in o and re.search(r'(?<![\w/])' + re.escape(e.url) +
                   r'''(?=$|[\s"'<>)\]}])''', o.replace(e.quote, '')) for o in observations)
               for e in valid)
    fields = {'name': lead.name, 'linkedin_url': lead.linkedin_url, 'current_role': lead.current_role,
              'company': lead.company, 'company_website': lead.company_website}
    def supported(field, value):
        for e in valid:
            if field not in e.supports: continue
            if value.casefold() in e.quote.casefold(): return True
            if field == 'linkedin_url' and e.url.rstrip('/') == value.rstrip('/') and lead.name in e.quote:
                return True
        return False
    gaps = [field for field, value in fields.items() if value and not supported(field, value)]
    data = lead.model_dump()
    data['evidence'] = [{**e.model_dump(), 'source': None} for e in valid]
    data['verification'] = data['verification_reason'] = None  # an agent cannot self-certify a tier
    # Retain supported facts as untrusted DATA for repair; no model rationale or
    # arbitrary missing-information strings enter the next process's feedback.
    partial = {field: value for field, value in fields.items() if value and field not in gaps}
    partial['evidence'] = data['evidence']
    partial['profile_provenance_verified'] = profile_proof
    if not profile_proof: return partial, 'profile_provenance', sorted(set(gaps + ['linkedin_url']))
    if gaps: return partial, 'unsupported_fields', gaps
    data['missing_information'] = list(dict.fromkeys(data['missing_information'] + [k for k, v in fields.items() if v is None]))
    if not lead.current_role or not lead.company: data['identity_status'] = 'needs_review'
    if not role_matches(data['current_role'], titles): return partial, 'role', ['current_role']
    if not scope_matches(data, keywords): return partial, 'scope', ['sector']
    return data, 'accepted', []
