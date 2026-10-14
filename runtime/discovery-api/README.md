# Discovery API v6 — isolated evaluate/repair runtime

This workspace copy has not been deployed. The original source and live service are unchanged.

## source="search" — fast search-engine discovery (no browser, no LinkedIn login)

Same endpoints and job lifecycle as below (`POST /api/v1/discoveries`, poll `GET …/{id}`, cancel,
delete, `exclude_previous`). Implemented in `search_backend.py`; the browser agent is not involved.

```json
{"source": "search", "keywords": ["Rechtsanwalt"], "titles": ["Partner", "Inhaber", "Geschäftsführer"],
 "locations": ["München", "Berlin"], "limit": 50, "include_review": false,
 "min_verification": "plausible", "expand_terms": true,
 "budget": {"seconds": 900, "max_cost_usd": 0.5}}
```

- `keywords` (required) describe the sector of any kind (e.g. `Steuerberater`, `Softwareentwicklung`,
  `Zahnarztpraxis`); `titles` are the target roles and win over any built-in vocabulary (e.g. `CEO`,
  `Head of Sales`; German feminine forms are searched too). Without `titles` the target is decision makers
  of any kind (owner, partner, Geschäftsführer, founder, CEO/C-level, board, head of/director).
  `locations` are plain place names.
  `geo_urns`, `current_company_ids`, `network` and `search_url` are LinkedIn-agent inputs and are
  rejected here. `limit` may be up to 100 for this source (30 for the agent sources).
- Pipeline: (1) Google results for `site:linkedin.com/in <keyword> <title> <location>`;
  (2) an LLM reads each result title/snippet, and name, role, firm and sector quotes must be literal
  substrings of that snippet; (3) the firm's own page or imprint is fetched (directories and
  data brokers are never used as the firm website); (4) when the target is still short, firm imprints
  in the location are mined for decision makers and their LinkedIn profiles are looked up.
- `expand_terms` (default true): one LLM call proposes up to 4 related sector terms and up to 4 exact
  equivalents of the requested titles (other wording/language, e.g. Head of Sales → Vertriebsleiter).
  Equivalent titles are also accepted by the role check; originals are queried first for every location.
  The terms used are reported as `expanded_keywords` / `expanded_titles`.
- `min_verification` sets the lowest tier that counts toward `limit`: `verified`, `plausible` (default) or
  `linkedin` (useful for tech companies or corporate roles that company websites rarely list).
- Every lead has `verification`: `verified` = a target title stands next to the person's name on the
  company's own website; `plausible` = one-person or namesake firm, title not
  stated; `linkedin` = role and company proven only by the LinkedIn search snippet, the website is silent;
  `review` = the website contradicts role or sector (e.g. listed as associate) or nothing was proven. Tiers at or
  above `min_verification` count toward `limit`; the others are returned only with
  `include_review: true`. Evidence items carry `source` (`search_snippet` or `firm_website`); quotes
  are exact source lines (case/whitespace normalised). `company` prefers the legal name from the imprint.
- The LinkedIn association rests on the search index (name in the profile title, plus firm or place
  in the snippet), not on a logged-in profile view; `identity_status` is `verified` only when the
  firm website confirmed the role and the snippet names the firm. Human review stays required.
- Budget: `seconds` is the job deadline; `max_cost_usd` caps provider + LLM spend (treg charges and
  OpenRouter `usage.cost`); in-flight parallel calls may overshoot by a few cents at most. On a
  stop the job returns `partial` with `stop_reason` `timed_out`/`budget_exhausted`; metadata reports
  `cost_usd`, `provider_calls`, `llm_calls`, `verification_counts`, `strategies`.
- Configuration (private volume, never in results or logs): `TREG_TOKEN` (store it with
  `docker compose exec session python3 /opt/browser-box/discovery-api/search_backend.py set-token`,
  masked prompt) and the existing `OPENROUTER_API_KEY` in `HERMES_HOME/.env`; optional
  `DISCOVERY_SEARCH_MODEL` (default `deepseek/deepseek-v4.1-flash`). Missing configuration fails the
  job with `search_not_configured`. Providers: treg routes `treg.google.serp.organic` (crawl4ai
  excluded by default, `DISCOVERY_SEARCH_SERP_EXCLUDE`) and `treg.web.extract`.
- Measured once on 2026-10-14 (Munich, lawyers, prototype of this pipeline): 18 verified +
  5 plausible of 28 leads, all 23 correct on manual review, about USD 0.025 and 90 s. One city, small
  sample: treat as an indication, not a guarantee of yield or precision.
- Not established by this source: current employment beyond the fetched pages, contact permission
  (UWG §7) or a GDPR basis for processing. Fetched page text is transient and not persisted.

## Shared registry — one lookup/suppression list for all agents

Implemented in `registry.py` (private `registry.sqlite3`, 0600). Persons are matched by canonical LinkedIn
profile (`de.linkedin.com/in/x/de` = `www.linkedin.com/in/x`), companies by website domain (preferred) or a
normalised company name (legal forms and `&`/`und` ignored). Only keys, status, source, note and timestamps are stored.

Statuses: `delivered` (returned by a search; expires after `REGISTRY_DELIVERED_DAYS`=365), `in_outreach`,
`contacted`, `expired`, `do_not_contact` (sticky: only DELETE removes it), `customer`, `excluded`.
`delivered` never overwrites another status. Blocking: every status except `delivered`/`expired`.

For external agents (same Basic Auth; POSTs from scripts send no `Origin` header, browsers must use this origin):

```sh
# Should I skip these? (batch, up to 500)
curl -u dom https://HOST/api/v1/registry/check -H 'Content-Type: application/json' -d '{"items": [
  {"linkedin_url": "https://www.linkedin.com/in/max-muster"},
  {"company_website": "https://firma.de", "company_name": "Firma GmbH"}]}'
# -> [{"index":0,"known":true,"blocked":true,"reasons":["person_contacted"],"person":{...},"company":null}, ...]

# Register my own contacts / suppression list (batch, up to 500; idempotent upsert)
curl -u dom https://HOST/api/v1/registry -H 'Content-Type: application/json' -d '{"items": [
  {"linkedin_url": "https://www.linkedin.com/in/max-muster", "company_website": "https://firma.de",
   "status": "contacted", "source": "agent-b", "note": "Kampagne Q4"},
  {"company_name": "Bestandskunde AG", "status": "customer", "source": "crm"}]}'
# -> {"created": 2, "updated": 0, "unchanged": 0, "ids": [...]}

curl -u dom 'https://HOST/api/v1/registry?status=do_not_contact&source=agent-b'   # list / export
curl -u dom -X DELETE https://HOST/api/v1/registry/<id>                            # free a person/company again
```

Effects inside the box: search never delivers a known person (with `exclude_previous`; otherwise only
blocking ones), skips companies with a blocking status, and caps counting leads per company and run with
`max_per_company` (default 1, `null` = no cap); metadata reports `excluded_by_registry` and
`excluded_same_company`. Every delivered lead is recorded as `delivered`. Outreach checks the registry
before any paid call: `409` with `person_do_not_contact|person_customer|person_excluded`,
`person_in_registry` (another agent works on this person), `company_blocked`, or `company_in_outreach`
(override with `"allow_same_company": true`). Outreach writes `in_outreach`, `contacted` (sent),
`expired`, and `do_not_contact` (`POST /api/v1/outreach/{id}/do-not-contact`). Deleting an outreach
contact frees the person only if it was still `in_outreach` from this box. Lead rows show
`registry_status` and `company_registry` (other entries at the same company). Existing deliveries and
outreach contacts are imported once at startup.

## Outreach — connect first, research and write after acceptance

Implemented in `outreach.py`. Nothing is sent, typed or clicked on LinkedIn; all texts are drafts.

```json
POST /api/v1/outreach
{"linkedin_url": "https://www.linkedin.com/in/…", "company_website": "https://firma.de/",
 "topic": "KI-Automationen", "template": "Kurz, Sie-Form, ein konkreter Bezug, Frage nach 15-Minuten-Call",
 "note_template": "optional guidance for the connection note", "connection_note": true,
 "note_max_chars": 200, "message_max_chars": 1200, "language": "de", "budget": {"max_cost_usd": 0.15}}
```

- `linkedin_url` (any LinkedIn host, canonicalised) and `company_website` or `company_name` are required.
  One contact per profile (`409 outreach_exists` with its id).
- 201 → `status: awaiting_connection`, `name`/`headline` from the search index and an optional
  `connection_note` (topic is context only, no pitch). Send the request yourself.
- Background check (`OUTREACH_CHECK_HOURS`=24, `OUTREACH_DAILY_CHECKS`=40, 20–60 s between views):
  the profile is opened in a background tab of the shared Chromium via CDP, only the top card text
  is read, the tab is closed (`Target.createTarget/attachToTarget`, `Page.navigate`,
  `Runtime.evaluate`, `Target.closeTarget`; no `Input.*`). States: `pending`, `not_connected`,
  `connected`, `login_required` (pauses all checks for 6 h until a human logs in via noVNC).
  Checks never overlap a browser-agent discovery job (shared lock). After `OUTREACH_EXPIRE_DAYS`=30
  without acceptance → `expired`. `POST /api/v1/outreach/{id}/accepted` starts research manually.
  Each check is a normal profile view by your account (visible to the person). The connection-state
  patterns were tested on synthetic text only; calibrate them once on real profiles before relying on them.
- After acceptance (`researching` → `ready`): the firm website, linked service/team/career/news pages
  (same host, up to 5) are fetched via treg; the LLM returns up to 5 `findings` (`need`, `where`,
  verbatim `quote`, `url`) — findings whose quote is not literally on the fetched page are dropped —
  and a `message` draft following `template`. `error: research_thin` when no finding survived.
- `GET /api/v1/outreach/{id}`, `GET /api/v1/outreach?status=ready`, `DELETE /api/v1/outreach/{id}`.
  Records (incl. template) stay in the private `outreach.sqlite3` (0600) for 180 days.
- Legal: drafts do not establish consent; LinkedIn's terms and UWG/GDPR duties stay with the sender.

## v2 resilience and bounded research

- Validate the outer final-result envelope strictly, then each lead independently. Unknown envelope fields fail the pass; unknown lead/evidence fields and invalid required name/profile/evidence drop that candidate. Only malformed optional **string** `company_website` URLs are nulled and explicitly added to `missing_information`; never guess a scheme or host. Wrong field types remain invalid.
- Parse only one successful terminal stream-json `result.text`. One unambiguous object inside prose/fences is accepted; multiple objects are rejected. Observations come only from successful exact-allowlisted tool results, retain their exact text and transient tool origin, and are never persisted. A search snippet alone cannot establish direct profile-document identity. Profile evidence requires a direct browser/document tool observation, exact profile source URL outside the quoted text, and a verbatim name quote with both `name` and `linkedin_url` support. URL-prefix collisions, unrelated-page links, joined observations and unsupported facts fail.
- With supplied `titles`, count only supported observed requested-role matches in `leads[]`, `returned_count` and metadata `qualified_count`. Matching uses casefolded word boundaries and explicit German variants (Inhaber/in, Mitinhaber, Partner/in, Geschäftsführer/in, geschäftsführend[e/er/en/em/es]); it does not infer Geschäftsführer from GF, Gründer, Geschäftsleitung or Rechtsanwalt. Explicit assistant/HR Business Partner/Partner Manager roles do not prove firm leadership. Unknown/nonmatching roles stay out of the qualified result; aggregate/per-iteration `rejected_unqualified_count` reports them without padding a review list into the quota. Sector fit is generic, with no hard-coded sector vocabulary: when `keywords` are supplied, the agent judges whether the candidate's firm belongs to that sector and must add a separate evidence item with `supports=[sector]` whose verbatim quote (from the profile or the firm's own website, researched via web_search/web_extract when the profile is unclear) shows the firm's sector. That quote passes the same observed-evidence gate as every other fact; its relevance remains an agent judgement shown to the human reviewer. Candidates without sector evidence are counted in `rejected_out_of_scope_count` and stay repairable. The prompt prioritizes current leadership proof from company websites plus direct LinkedIn identity. Human review remains required: provenance/fit filtering is not independent truth, current employment or permission to contact.
- Run serialized subprocess passes requesting at most three new candidates, carrying all previous and newly accepted canonical exclusions. Page hints advance through ordinary accessible search pages 1–10, starting at the supplied page when present. Supplied search URL/opaque IDs remain byte-for-byte unchanged in the prompt. Public-web passes vary a requested role focus without relaxing criteria. Stop on a reported barrier; never attempt alternate navigation to evade one.
- **One budget for the entire job:** one monotonic deadline; aggregate stdout + stderr cap of 2 MiB; existing per-line cap; no queue changes. Each pass reserves at most eight of the remaining CLI turns for requests up to 30 turns (for larger explicit requests initially 16, leaving room for per-candidate firm website search/extract, adapting to 14 after fully accepted batches with no pending repair) and charges that entire allocation, even if it uses fewer observed calls. This conservative reservation covers text-only/model turns not represented in tool events, so the sum of all CLI max-turn allocations never exceeds request `max_turns`. Actual `tool_use` events are separately counted and checked; missing tool-use streams cannot earn extra work. The process-local locked dispatch gate also caps execution, including parallel tool batches. For max_turns=30, maximum allocations are 8+8+8+6; this deliberately may yield partial results rather than secretly lifting limits.
- Keep earlier accepted leads when a later pass exits, times out, overflows or fails final schema validation. Return `partial` with a safe `stop_reason`, aggregate counts and per-iteration metadata. A blocked pass returns `blocked` with supported earlier leads and the declared barrier. Cancellation still clears the result and cannot resurrect deleted/cancelled jobs. Raw diagnostics/model errors are never returned.

### v6 evaluate/repair and integration

`evaluation.py` tracks deterministic per-candidate verdicts (duplicate, invalid, schema, unsupported fields, profile provenance, role, scope, and supported-but-over-quota). Aggregate and per-pass `rejection_counts` expose those categories without retaining rejected personal records. These are primary verdicts in validation order, not a claim that every gap is an unsupported fact. Invalid/schema candidates are dropped locally; arbitrary rejected fields/errors are not reflected into a prompt.

Each job owns a bounded repair queue (six canonical profiles, at most two follow-up offers each). Later pass prompts receive only enumerated gaps and supported observed partial facts/evidence as **untrusted JSON DATA**. Unsupported values, free-form model rationale and missing-information strings are omitted from feedback. Existing actual successful tool observations retain their exact text and tool origin transiently across passes. Retained role/company/website facts and evidence can fill omitted fields in a repaired proposal, but the combined candidate is revalidated against those actual observations with the identical exact-evidence/direct-profile gates. A duplicate within a rejected batch cannot erase a supported partial. Accepted/prior/definitively irrelevant/exhausted-repair profile exclusions are job-local; global memory/background review remain disabled.

`procedure.py` explicitly loads and injects shipped `linkedin-discovery` procedure **2.0.0** from `/opt/browser-box/skills/linkedin-discovery/SKILL.md`, never the mutable session profile or a caller-supplied path. Host unit tests fall back to this repository's `skills/linkedin-discovery/SKILL.md` through the correct relative path. Missing/incompatible procedure fails closed (`procedure_unavailable`). Installing a skill alone is no longer presented as active use.

`rendering.py` deterministically builds accepted API lead JSON and validates it against `AcceptedResult` **before persistence**; no model-generated prose or second JSON object is a rendered response. Strict final-envelope ambiguity rejection is unchanged. `store.py`, routes, SQLite schema, cancellation/transaction behavior, policy hooks, cumulative deadline/output caps and request ceilings are unchanged.

The read-only inspected deployed `/opt/hermes/hermes_cli/stream_json.py` emits tool calls/results, text deltas, tokens and terminal `result.text`, but **no measured model-iteration counter**. No version-verified iteration hook was added. `charged_turns` remains the full allocation charged to admission control, with explicit `reserved_turns`, `budget_accounting=full_cli_reservation`, and `measured_model_iterations=null`. Individual `tool_use_count` is separate. Larger explicit requests use initial 16-turn reservations (room for firm website search/extract per candidate), adapt down to 14 after a fully accepted batch without pending repairs, and otherwise reserve 16; requests up to 30 retain caps of 8. Every cap is clipped to the cumulative remaining reservation budget. These smaller reservations are mechanism-tested only, not proof of research yield.

Docker's existing `COPY runtime/ /opt/browser-box/` includes all three new modules automatically. The appliance Dockerfile must also package the repository skill tree at `/opt/browser-box/skills/` (installer owner's scope); startup already expects that path. No local live deployment, image build, paid model job, credential access or Brain publication was performed here. Isolated appliance build/policy compatibility and real requested-count acceptance belong to the parent.

### Local regression verification

From the appliance root run:

```sh
PYTHONPATH=runtime/discovery-api python3 -m unittest discover -s runtime/discovery-api/tests -v
```

The final execution output is `TEST_RESULTS.txt` in this directory. Fixtures are explicitly synthetic, including actual OS subprocesses receiving complete real worker prompts, job-local repair feedback, retained successful observations, proof/no-proof/search-only/error/profile-mismatch cases, cancellation during a held repair process, dedup/exclusions and repeated exact persisted API readback. A real subprocess-shaped HTTP regression exhausts **120 full reserved turns with 8 individual tool calls** and still returns a valid persisted HTTP 200 partial response with model iterations unknown. This demonstrates transport/accounting, not genuine leads. Existing warning: Starlette/httpx test-client deprecation.

## Original integration and contract (v1 baseline)


This directory is the image-integrated FastAPI backend for occasional, explicitly requested professional discovery. It does not send outreach (the outreach endpoints below only draft texts). Supervisor starts it privately on port 8081; the existing authenticated Caddy site routes /api/v1 and /api/v1/* to it. Chromium, provider credentials and lead data remain in the existing private volume. No raw backend port is published.

## API contract

All routes retain `/api/v1`:

- `POST /api/v1/discoveries` → 202, private random job ID; 429 if eight queued/running jobs already exist.
- `GET /api/v1/discoveries/{id}` → persisted status and validated result, or 404.
- `POST /api/v1/discoveries/{id}/cancel` → pending/running job becomes `failed`, error `cancelled`; its subprocess group terminates. Terminal jobs are unchanged.
- `DELETE /api/v1/discoveries/{id}` → 204; cancel if active, delete job/result. Dedup keys remain until retention expires.
- `/api/v1/openapi.json` and `/api/v1/docs` → real generated request, job and lead schemas once deployed. There is no claim that any external endpoint is currently live.

Request example (criteria only, not a prompt):

```json
{
  "keywords": ["industrial automation"],
  "titles": ["Managing Director", "Founder"],
  "geo_urns": [],
  "current_company_ids": [],
  "network": ["S", "O"],
  "source": "linkedin",
  "limit": 5,
  "exclude_previous": true,
  "budget": {"seconds": 180, "max_turns": 12}
}
```

`roles` is an input alias for `titles`; do not supply both. Keywords/titles are bounded lists of plain search data. Geo values accept numeric IDs or `urn:li:geo:<numeric-ID>`; company IDs are numeric strings. Leading zeroes and supplied URNs are preserved, not repaired or mapped from human place names. `network` permits LinkedIn codes `F`, `S`, `O` only. `source` is `linkedin` or `web`; limit is 1–30. Budget seconds is 15–1800; max_turns is 1–120. Unknown fields, type coercions, unrestricted prompt/shell fields and oversized bodies are rejected.

Alternatively provide `search_url` alone (plus limit/exclusions/budget). It must be an exact HTTPS `www.linkedin.com/search/results/people/` URL, without userinfo, port, fragment, duplicate parameters or unknown parameters. Only `keywords`, `title`, `geoUrn`, `currentCompany`, `network`, `origin`, `sid`, `page` are permitted (page 1–10 for normal accessible pagination; never access-limit evasion), and array filters are validated. A valid supplied URL is preserved exactly. It cannot be combined with filter fields or `source=web`. Generated query parameters use `urllib.parse.urlencode` and JSON arrays, never string concatenation of raw parameter values.

Statuses: `queued`, `running`, `succeeded`, `partial`, `blocked`, `failed`, `timed_out`. `succeeded` requires the requested number of accepted profiles and a completed backend outcome; otherwise counts remain honest. Jobs expose `requested_count`, `returned_count`, `missing_count`, metadata, and `result=null` until a real result exists. Partial and blocked results may include supported profiles found before stopping. No padding or count guarantee.

Lead fields: `name`, nullable `current_role`, nullable `company`, nullable `company_website`, canonical `linkedin_url`, `evidence` (`url`, verbatim `quote`, `supports`), `identity_status`, `missing_information`, nullable `fit_reason`. Every populated factual field must appear literally in supported evidence. For a LinkedIn URL, the exact observed profile document address plus a verbatim name quote from that same URL also supports association; the address need not be printed inside page body text. A name quote from an unrelated page does not establish that association. Evidence URL and quote must co-occur in successful observed browser/web tool output; final model claims alone are rejected. Unsupported leads are dropped, not stored in the dedup index. `fit_reason` remains a separate inference; contact permission is explicitly not established. This conservative check proves observed provenance, **not independent truth, freshness, identity accuracy, or legal eligibility**. Truncated/URL-less tool outputs can cause otherwise useful candidates to be rejected. Do not loosen validation merely to meet a requested count.

## Worker and safety

SQLite defaults to `/opt/data/discovery-api/jobs.sqlite3`, directory 0700, files 0600. One worker thread serializes API jobs; a process lock prevents two services using the same directory. Every connection is transaction-scoped and explicitly closed. Startup marks both queued and running interrupted jobs failed; it never automatically replays discovery. Retention defaults to 30 days for terminal jobs and dedup keys, cleaned at startup/submission and by an idle maintenance check. Maintenance does not trigger discovery/model work. SQLite secure deletion is enabled. Protect volume/backups separately.

The verified pinned Hermes source `/opt/hermes/hermes_cli/stream_json.py` emits final `{type:"result", exit_code, text, ...}`. This implementation parses **only the final event's `text`** as result JSON, never concatenates tool outputs/deltas. Tool result output is used only for provenance validation. CLI arguments are constructed as a list, no shell; the bounded query arrives on stdin via `--query-file -`. CLI max-turns/run-budget and an independent wall timeout bound work. Stdout plus stderr is capped at 2 MiB; individual stdout records at 128 KiB. Raw diagnostics and observations are not retained in API logs/SQLite. Process groups receive TERM then KILL on timeout, cancellation, shutdown, overflow and completion.

**Important source finding:** Hermes's `browser` bundle includes vault tools, arbitrary `browser_exec`, CDP, click/type and console. Merely using `-t browser,web` is NOT read-only. The standalone `hermes_discovery.py` entrypoint installs a process-local schema and dispatch allowlist, without changing installed Hermes source. Allowed tools: `browser_navigate`, `browser_snapshot`, `browser_scroll`, `browser_back`, `browser_get_images`, `web_search`, `web_extract`. Vault, arbitrary code, click/type/press, CDP, terminal, sending, cron and delegation are excluded. `agent_policy.py` forces per-process `skip_memory=True`, `skip_background_review=True`, the browser/web toolsets, and filters post-initialization auxiliary schemas. No live profile settings are changed. LinkedIn navigation is restricted to canonical profiles, people searches and company/about pages. The wrapper fails on unsupported source interfaces; run its no-model check after every Hermes upgrade.

Restrictions on captchas, access limits, sensitive data and page-embedded instructions are also carried in the preloaded skill and task prompt. Never bypass a blocked page. Basic browse/web tools still execute trusted installed implementation and third-party page scripts; this is not an OS sandbox or complete DNS/redirect egress firewall. Keep the appliance's private-network URL protections, trusted tool/plugin configuration and network controls. The API accepts no arbitrary fetch endpoint. Do not use other Hermes/browser agents concurrently; the lock coordinates **this API**, not human clicks or unrelated clients.

API retention/log caps do not delete/cap Hermes's own session database, session traces, model-provider records or existing global logs. Those may include research content. Apply a separate private retention policy to that existing runtime before production use; do not publish lead/session content to Brain. The API itself never accesses Brain. Only source and explicitly synthetic test fixtures belong in source publication.

## Integration invariants (carried forward from browser-box v5)

- Docker copies the trusted runtime tree to `/opt/browser-box`; the wrapper is executable and the session remains non-root. The existing image supplies FastAPI/Pydantic/uvicorn; no operator credentials are inherited.
- Startup installs `skills/linkedin-discovery`, prepares private `/opt/data/discovery-api`, and Supervisor starts one uvicorn worker on internal port 8081. The API uses the existing configured provider/browser, not a second Chromium. State/profile volumes are preserved during session recreation; do not promise real third-party login persistence solely from synthetic cookie tests.
- Provisioning sets `DISCOVERY_ALLOWED_ORIGIN` to the exact HTTPS origin. Existing global Caddy Basic Auth protects both API and noVNC; the prefix is not stripped. Raw API/CDP/Docker socket are not exposed. No standalone unauthenticated API deployment is supported.
- Run `/opt/browser-box/discovery-api/hermes_discovery.py --check-policy` after Hermes upgrades. Unexpected tool names/source interfaces block use. No installed Hermes source/profile settings are edited by the wrapper.
- Verify absent/wrong credentials at 401, authenticated OpenAPI/docs at 200, invalid input at 422, cross-origin writes at 403, and a complete explicitly authorized real job. Unit fixtures do not demonstrate real discovery. See appliance TEST-RESULTS.md for the separate executed live scope and limits.

## Tests

From this source directory:

```sh
python3 -m unittest discover -s tests -v
```

Tests use explicitly synthetic lead data (`Synthetic Fixture Person`) and a real subprocess fixture, never a model/browser or fabricated production response. Coverage includes strict criteria/URL validation, request size limits, encoded filters and URL preservation, evidence rejection, within/across-job dedup, queue concurrency/serialization, private persistence, restart recovery/locking, retention, cancellation/deletion, error redaction, output overflow, nonzero exits, malformed stream, closed-pipe hangs, wall timeouts, API-to-subprocess integration, OpenAPI result schemas, and the technical tool policy. See `TEST_RESULTS.txt` for actual final execution output.

## Using the integrated API

Use the existing HTTPS origin and Basic Auth username `dom`, with the password chosen in the wizard. No password reset is required. POST creates a job; GET retrieves its JSON. Model/tool use starts only after a submission. For internal-CA HTTPS, trust the exact appliance CA for this request; do not disable public TLS validation or globally trust an unrelated CA.

On the VPS, custom-port profile `public` can export its PUBLIC certificate (never copy root.key):

```sh
docker compose cp public:/data/caddy/pki/authorities/local/root.crt .state/client-ca.crt
```

Set BASE_URL to the actual HTTPS origin printed by the wizard, then:

```sh
curl --cacert .state/client-ca.crt -u dom \
  -H 'Content-Type: application/json' \
  -d '{"keywords":["kanzlei"],"titles":["Inhaber","Partner"],"limit":15,"budget":{"seconds":300,"max_turns":25}}' \
  "$BASE_URL/api/v1/discoveries"

curl --cacert .state/client-ca.crt -u dom "$BASE_URL/api/v1/discoveries/JOB_ID"
```

curl prompts for the password instead of putting it into arguments or chat. With publicly trusted TLS, use normal system trust instead of the internal-CA option. Open `/api/v1/docs` in your already authenticated browser for interactive requests and the exact schema. Titles are desired role filters, not evidence that a result holds that role. URNs/IDs are caller-supplied; no guessed region mapping.

## Extended on-demand batch budget

Explicit request ceilings are `budget.seconds: 15..1800` and `budget.max_turns: 1..120`; defaults remain 180 seconds/12 turns. Requests up to 30 turns retain eight-turn per-pass reservations; larger explicit budgets reserve up to 12 turns per pass, adapting to 8 after a fully accepted batch with no pending repair. The single job deadline/output cap/turn ledger remain shared across all passes. The prompt exposes the per-pass tool limit and requires early JSON wrap-up, not exhaustion. Ten-contact verification requests explicitly use 1200 seconds/120 turns; they do not silently override caller budgets. Invalid candidates, unsupported roles and access barriers are never relaxed to fill quota.
