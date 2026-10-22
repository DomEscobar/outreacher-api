# Discovery API v6 — isolated evaluate/repair runtime

This workspace copy has not been deployed. The original source and live service are unchanged.

**Agents: read [`AGENT-GUIDE.md`](AGENT-GUIDE.md) before calling any endpoint.** It contains the
access rules (`Origin`, auth, body limits), the absolute read-only/challenge rules, the endpoint map,
the outreach state machine, the error-handling matrix and the self-imposed rate limits.

## source="search" — fast search-engine discovery (no browser, no LinkedIn login)

Same endpoints and job lifecycle as below (`POST /api/v1/discoveries`, poll `GET …/{id}`, cancel,
delete, `exclude_previous`). Implemented in `search_backend.py`; the browser agent is not involved.

```json
{"source": "search", "keywords": ["Rechtsanwalt"], "titles": ["Partner", "Inhaber", "Geschäftsführer"],
 "locations": ["NRW", "München"], "limit": 50, "include_review": false,
 "min_verification": "plausible", "expand_terms": true, "exclude_previous_companies": false,
 "budget": {"seconds": 900, "max_cost_usd": 0.5}}
```

- `keywords` (required) describe the sector of any kind (e.g. `Steuerberater`, `Softwareentwicklung`,
  `Zahnarztpraxis`); `titles` are the target roles and win over any built-in vocabulary (e.g. `CEO`,
  `Head of Sales`; German feminine forms are searched too). Without `titles` the target is decision makers
  of any kind (owner, partner, Geschäftsführer, founder, CEO/C-level, board, head of/director).
  `locations` are plain place names or regions (see below).
  `geo_urns`, `current_company_ids`, `network` and `search_url` are LinkedIn-agent inputs and are
  rejected here. `limit` may be up to 100 for this source (30 for the agent sources).
- Pipeline, in rounds that stop as soon as `limit` counting leads are confirmed:
  (1a) Google results for `site:linkedin.com/in <keyword> (<title> OR <titlein>) <location>`, twelve
  queries per round, at most 80; (1b) people index: `exa.people.search` via treg (free-text query
  "<titles> im Bereich <keyword> in <location>", one to three queries per round and 10 to
  `DISCOVERY_PEOPLE_RESULTS`=25 profiles per call, sized to the leads still missing; $0.007 + $0.001 per
  result beyond 10). Rounds alternate, Google first: where firms publish their teams the first Google
  round usually reaches the target and the index is never paid for; in thin sectors the index follows
  right after it instead of after the whole Google plan has used up the deadline.
  `DISCOVERY_PEOPLE_SEARCH=first` starts with the index, `0` uses Google only;
  (2) an LLM reads each profile text/snippet, and name, role, firm and sector quotes must be literal
  substrings of it; (3) the firm's own page or imprint is fetched (directories and data brokers are never
  used as the firm website); (4) when the target is still short, firm imprints in the location are mined
  for decision makers and their LinkedIn profiles are looked up.
- Every profile is read once per job, whichever strategy finds it first. Firm-website confirmation runs in
  batches that shrink near the target, and a firm that already holds `max_per_company` leads is skipped
  before any paid confirmation. Metadata: `strategies` (used, in order), `qualified_by_strategy`,
  `people_queries`, `queries`.
- People-index text is the profile's name, its current positions as dated by the index
  ("Aktuelle Position: <title> – <firm>"), location and highlight lines (markdown links reduced to their
  text); evidence from it has `source: "people_index"`. A quoted role or firm that the index dates only as
  past is rejected (`stale_rejected`). After two failed index calls a job continues without it.
- A website tier needs the website facts it rests on in the evidence: `verified` a firm-website quote
  for the role, `plausible` firm-website quotes for the name and the firm (namesake or one-person firm).
  A quote the LLM copied verbatim (up to case/whitespace) that matches no whole source line is kept as
  quoted rather than dropped.
- Query plan: original sector terms × requested titles first, then equivalent titles, then expanded
  sector terms; inside each group the locations take turns, so neither the round order nor the 80-query
  cap starves a location or an expanded term. Google returns about 10 rows per query (depth 10).
- `expand_terms` (default true): one LLM call proposes up to 4 related sector terms, up to 4 exact
  equivalents of the requested titles (other wording/language, e.g. Head of Sales → Vertriebsleiter) and
  up to 8 facets (specialisations inside the sector, e.g. Arbeitsrecht, Familienrecht). Equivalent titles
  are also accepted by the role check; facets only add queries ("Rechtsanwalt Arbeitsrecht (Partner OR
  Partnerin) Köln", for the index "…, Schwerpunkt Arbeitsrecht, …") to reach other corners of the sector
  and never widen a check. Reported as `expanded_keywords` / `expanded_titles` / `expanded_facets`.
- Regions (`regions.py`): a German state, metro region or "Deutschland" in `locations` ("NRW",
  "Nordrhein-Westfalen", "Bayern", "Ruhrgebiet", "Rhein-Main", …) is searched through its largest cities in
  population order (NRW: Köln, Düsseldorf, Dortmund, … Münster); the people index is first asked for the
  region as a whole. The location check still uses the requested area, so a firm elsewhere in the region
  counts. Several entries take turns (Köln, München, Düsseldorf, …); other places stay as given.
  Metadata: `regions`.
- Every candidate keeps the place it was found in (the searched place its text names, else the place of
  its query); firm-website lookups and the firm-first profile match use that place, not the first one
  requested. Candidates are confirmed taking turns by place, so the first leads are not all from the
  place searched first.
- Time limits per call (seconds per attempt × attempts): Google 45 × 3, people index 60 × 2, firm page
  30 × 2, LLM 120 × 3, and no attempt runs past the job deadline, so one hanging firm site cannot stall a
  confirmation batch (it used to be 120 s × 4 per page). Metadata `call_seconds` shows calls, total and
  slowest seconds per kind.
- Memory across runs (`search_memory.py`, private `search_memory.sqlite3`, 0600, 90 days; only with
  `exclude_previous: true`): per criteria (sector terms, titles, locations, `min_verification`) it keeps
  the queries run with the number of results still open at the end, and SHA-256 keys of profiles and firm
  imprints already decided (rejected at classification, checked below the target tier, delivered, imprint
  read). A repeat search skips decided people and imprints without paying for them again
  (`skipped_known`), brings back open candidates (not yet checked, or counting but beyond the limit), and
  moves queries that left nothing open behind fresh ones
  (`demoted_queries`), so facets, further places and expanded terms come first. Queries are stored as run;
  no names, quotes or profile URLs. Other criteria start fresh; the evaluation set never uses it.
- `exclude_previous_companies` (default false): also skip every firm already in the shared registry, even
  if only delivered before (`excluded_known_company`); by default only persons are excluded across runs
  and a firm can return with another person.
- `min_verification` sets the lowest tier that counts toward `limit`: `verified`, `plausible` (default) or
  `linkedin` (useful for tech companies or corporate roles that company websites rarely list).
- Every lead has `verification`: `verified` = a target title stands next to the person's name on the
  company's own website; `plausible` = one-person or namesake firm, title not
  stated; `linkedin` = role and company proven only by the LinkedIn search snippet, the website is silent;
  `review` = the website contradicts role or sector (e.g. listed as associate) or nothing was proven. Tiers at or
  above `min_verification` count toward `limit`; the others are returned only with
  `include_review: true`. Evidence items carry `source` (`search_snippet` or `firm_website`); quotes
  are exact source lines (case/whitespace normalised). `company` prefers the legal name from the imprint.
- The LinkedIn association rests on the search/profile index (name in the profile title, plus firm or
  place in the snippet), not on a logged-in profile view; `identity_status` is `verified` only when the
  firm website confirmed the role and the snippet names the firm as its own word (the person's surname
  inside the name line does not count; a namesake firm such as "Müller Rechtsanwälte" must appear beside
  the name). Firm-first leads whose profile matched by name and place only are capped as possible
  namesakes: at most `plausible` when the snippet also has a sector term, else `review`.
  Human review stays required.
- Budget: `seconds` is the job deadline; `max_cost_usd` caps provider + LLM spend (treg charges from
  `_treg.charged_micro` or the `X-Treg-Cost-Micro` header, Exa `costDollars`, OpenRouter `usage.cost`);
  in-flight parallel calls may overshoot by a few cents at most. On a stop the job returns `partial` with
  `stop_reason` `timed_out`/`budget_exhausted`; metadata reports `cost_usd`, `provider_calls`,
  `llm_calls`, `verification_counts`, `strategies`.
- Provider failures are never read as "nothing found": every failed call is counted in
  `provider_errors` as `<treg|openrouter>_<http_<status>|network|invalid_response>` (e.g.
  `treg_http_402` = treg balance used up, `openrouter_http_402` = OpenRouter credits). Payment
  required (402) anywhere, or five failed search/LLM calls in a row, stop the job with
  `stop_reason: provider_error` (job `failed`, `error: provider_error` when nothing was found yet).
  Failed page fetches (e.g. a missing `/impressum`) only count. 401/403 on search or LLM calls still mean
  `search_not_configured`.
- Configuration (private volume, never in results or logs): `TREG_TOKEN` (store it with
  `docker compose exec session python3 /opt/browser-box/discovery-api/search_backend.py set-token`,
  masked prompt) and the existing `OPENROUTER_API_KEY` in `HERMES_HOME/.env`; optional
  `DISCOVERY_SEARCH_MODEL` (default `deepseek/deepseek-v4.1-flash`). Missing configuration fails the
  job with `search_not_configured`. Providers: treg routes `treg.google.serp.organic` (crawl4ai
  excluded by default, `DISCOVERY_SEARCH_SERP_EXCLUDE`) and `treg.web.extract`.
- Measured once on 2026-10-14 (Munich, lawyers, prototype of this pipeline): 18 verified +
  5 plausible of 28 leads, all 23 correct on manual review, about USD 0.025 and 90 s. One city, small
  sample: treat as an indication, not a guarantee of yield or precision.
- Evaluation set (`eval_search.py`, cases in `eval_cases.json`: lawyers, tax advisers and software firms
  in München and Hamburg, 10 leads each, at most $0.40 per case). Inside the appliance:
  `python3 /opt/browser-box/discovery-api/eval_search.py plan` (cases and maximum spend, no calls), then
  `… eval_search.py run --max-total-usd 1.5` (paid) writes `leads.jsonl`, `summary.json` and `review.csv`
  to a new private directory under `/opt/data/discovery-api/eval/` (0700/0600). Fill `person_ok`,
  `role_ok`, `sector_ok`, `location_ok` with y/n, then `… eval_search.py score <dir>` reports precision per
  verification tier, per strategy and per case, plus yield and cost per qualified lead. Runs bypass the job
  store and the registry (nothing is recorded as delivered, nothing excluded). Lead rows stay private.
- Measured 2026-10-21 with this set, once per case and order, before manual review (yield and cost only,
  precision not yet labelled): index first: 60/60 leads, $0.59, 48 verified + 12 plausible counting;
  Google only: lawyers and tax advisers 40/40 for $0.18 (36 verified), software München 6/10 when the
  600 s deadline ended, software Hamburg stopped by `treg_http_402` (balance used up). Hence the default:
  Google first, index in alternating rounds.
- Measured 2026-10-21 with `--repeat 2` (memory, second run excluding the first run's leads), "NRW":
  lawyers 10 + 10 leads, $0.035 + $0.056, 244 s + 165 s, 35 people skipped in run 2; software 10 + 10,
  $0.046 + $0.045, 365 s + 138 s, 26 skipped, the index added 2 in run 2. No person and no firm in both
  runs. Before the call time limits, lawyers run 2 had stalled at 2 leads in 627 s.
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

Implemented in `outreach.py` (bookkeeping/research) and `connect.py` (the opt-in mutation: the connection
request). Messages after acceptance are drafts; the box never sends them.

**Default flow (manual connecting):** the UI start page „Heute“ shows what to do next (to-do cards for replies, ready messages, failed drafts and invitations not found; one „Nächster Lead“; a calm „Läuft von selbst“ summary; the browser tab title counts open to-dos). „Vernetzen ↗“ creates the contact (no paid call: name
and role come from the lead) and opens the LinkedIn profile; you click „Vernetzen“ there yourself. About
three times a day the box reads your sent invitations and your newest connections (see *Invitation look*
below): `connection.state: pending` once the invitation is listed, `status: connected` once the person is
among your newest connections, then research → `ready`. „Öffnen & kopieren“ copies the draft, opens the
profile (click „Nachricht“, paste) and marks the contact `sent` (UI: „wartet auf Antwort“).

```json
POST /api/v1/outreach
{"linkedin_url": "https://www.linkedin.com/in/…", "company_website": "https://firma.de/",
 "name": "Erika Musterfrau", "headline": "Partnerin",
 "topic": "KI-Automationen", "template": "Kurz, Sie-Form, ein konkreter Bezug, Frage nach 15-Minuten-Call",
  "note_template": "optional guidance for the connection note", "connection_note": false,
  "note_max_chars": 200, "message_max_chars": 1200, "language": "de", "auto_connect": false,
  "budget": {"max_cost_usd": 0.15}}
```

- `linkedin_url` (any LinkedIn host, canonicalised) and `company_website` or `company_name` are required.
  One contact per profile (`409 outreach_exists` with its id).
- 201 → `status: awaiting_connection`, `name`/`headline` (as given, else from the search index) and an
  optional `connection_note` (topic is context only, no pitch). With `name` given and
  `connection_note: false` nothing is looked up or written (cost 0). `connect.state` is `manual` unless
  the box sends requests itself: `OUTREACH_AUTO_CONNECT=1` (default `0`) plus `auto_connect` (default =
  that setting) → the background worker sends the request with the note; `connect.state`
  (`not_sent` → `sending` → `sent`/`failed`) reports it. A request found under sent invitations sets
  `connect.state: sent` either way, so the box never sends one you already sent.
- Connection requests (`connect.py`, the only module allowed to click; only with `OUTREACH_AUTO_CONNECT=1`): one profile at a time in a
  background tab via CDP, "Vernetzen"/"Connect", optional "Notiz hinzufügen" + "Senden". Hard stops:
  daily cap `OUTREACH_CONNECT_DAILY_CAP`=15, weekly cap `OUTREACH_CONNECT_WEEKLY_CAP`=60, irregular
  spacing `OUTREACH_CONNECT_SPACING`=`90-240` s, a randomized `OUTREACH_CONNECT_DWELL`=`3-9` s on the
  profile before clicking, working-hours window, and any challenge/rate-limit/login page pauses everything
  for `OUTREACH_BLOCK_PAUSE_HOURS`/`LOGIN_PAUSE_HOURS` (persisted). An exhausted invitation allowance
  (`Einladungslimit`, `weekly invitation limit`) pauses for `OUTREACH_LIMIT_PAUSE_HOURS`=72. Unknown UI
  (button not found, modal not opened, no send button) is retried up to `OUTREACH_CONNECT_ATTEMPTS`=3
  times with `OUTREACH_CONNECT_RETRY_HOURS`=6, then `connect.state: failed` with `connect.error: connect_*`.
  There is deliberately **no** fingerprint spoofing, CAPTCHA solving, mouse/keystroke simulation, proxy
  rotation or rate-limit circumvention: the box stays within human-scale limits and stops honestly.
  `POST /api/v1/outreach/{id}/connect` retries a failed/manual request once; the rate/UI patterns are
  tested on synthetic pages only, so calibrate on a real profile before relying on it.
- `awaiting_connection` is the workflow stage (connect first); the observed state is
  `connection.state`, which stays `unknown` until the invitation look (or a profile check) sees it.
  `connect.state` says who sends the request and whether it was seen as sent.
- Invitation look (`OUTREACH_INVITE_CHECK=1`, default on): while contacts are `awaiting_connection`,
  about every `OUTREACH_INVITE_CHECK_HOURS`=4 (±jitter, inside `OUTREACH_ACTIVE_HOURS`) and at most
  `OUTREACH_INVITE_DAILY_CAP`=3 times per local calendar day, the box opens
  `https://www.linkedin.com/mynetwork/invitation-manager/sent/` and then
  `https://www.linkedin.com/mynetwork/invite-connect/connections/` (newest first) in background tabs of
  the shared Chromium (same CDP calls as the profile check: no clicks, no typing, no scrolling) and reads
  the profile links of up to 200 cards per page. A card counts only when it holds exactly one profile and
  a „Zurückziehen“/„Withdraw“ (sent) or „Nachricht“/„Message“ (connections) button, so suggestion boxes on
  the same page never count. A contact matches by profile URL, otherwise by a first+last name that is
  unique among contacts and cards. Listed under sent → `connection.state: pending`, `connect.state: sent`;
  among the newest connections → `status: connected` (also for `expired` contacts: a late acceptance
  counts), research starts. Not being listed proves nothing (the list may be cut off or not rendered):
  the contact stays as it is. A look starts only when a contact has waited 20 min (time to actually click
  „Vernetzen“). Contacts sent by hand get no routine profile views (`connection.next_check_at: null`);
  with `OUTREACH_INVITE_CHECK=0` they fall back to the profile check below. After
  `OUTREACH_EXPIRE_DAYS`=30 without acceptance → `expired`. Login wall / challenge pause everything as
  below. The lists (other people's names) stay in memory for that look and are never stored or returned.
- `POST /api/v1/outreach/sync-invitations` runs one look now (UI: „Jetzt nachschauen“): ignores the
  schedule, window and daily cap, never a pause. Returns counts only: `{checked, sent_listed,
  connections_listed, contacts_waiting, pending, connected}`, or `{checked: false, reason}` with
  `nothing_waiting|paused|login_required|blocked|unreadable`.
- `POST /api/v1/outreach/{id}/check-connection` checks the profile status now, read-only: one look at the
  public profile, nothing sent/connected/messaged. It ignores the schedule and the daily cap (a deliberate
  user action), uses the shared browser lock, and on a login wall pauses background checks and reports
  `login_required`. For a contact already `sent`/`replied`/`do_not_contact` it is a no-op (the connection
  question is settled). The UI exposes it as the "Profilstatus prüfen" button in the contact's Verbindung
  panel, with each state labelled: `not_connected` (no request visible), `pending` (request outstanding),
  `connected` (already connected), `unknown` (not yet checked / not readable), `login_required`.
- Background profile check, only for requests sent by the box (or for every contact with
  `OUTREACH_INVITE_CHECK=0`) (`OUTREACH_CHECK_HOURS`=24, `OUTREACH_DAILY_CHECKS`=10,
  `OUTREACH_PROFILE_SPACING`=`120-300` seconds between views; the whole
  loop is off when `OUTREACH_BACKGROUND=0`, and `check-connection` is then the only way to update the state):
  the profile is opened in a background tab of the shared Chromium via CDP, only the top card text
  is read, the tab is closed (`Target.createTarget/attachToTarget`, `Page.navigate`,
  `Runtime.evaluate`, `Target.closeTarget`; no `Input.*`). States: `pending`, `not_connected`,
  `connected`, `login_required` (pauses checks until a human logs in via noVNC).
  A challenge/rate-limit/restriction page (`unusual activity`, `rate limit`, `/checkpoint/challenge`, …)
  pauses everything for `OUTREACH_BLOCK_PAUSE_HOURS`=12. The pause is stored in `meta.paused_until`, so a
  container restart does not resume reads against a challenged session; `POST /api/v1/outreach/resume`
  clears it after a human logged in. Checks never overlap a browser-agent discovery job (shared lock).
  After `OUTREACH_EXPIRE_DAYS`=30 without acceptance → `expired`.
  `POST /api/v1/outreach/{id}/accepted` marks it connected manually.
  Each check is a normal profile view by your account (visible to the person). The connection-state
  patterns were tested on synthetic text only; calibrate them once on real profiles before relying on them.
- After acceptance (`researching` → `ready`): the firm website, linked service/team/career/news pages
  (same host, up to 5) are fetched via treg; the LLM returns up to 5 `findings` (`need`, `where`,
  verbatim `quote`, `url`) — findings whose quote is not literally on the fetched page are dropped —
  and a `message` draft following `template`. `error: research_thin` when no finding survived.
- Reply check (`replied`): while contacts in `ready` or `sent` wait for an answer, at most every
  `OUTREACH_REPLY_CHECK_HOURS` (12, never less) and at most `OUTREACH_INBOX_DAILY_CAP`=2 times per local
  calendar day the box opens `https://www.linkedin.com/messaging/` in a background tab of the shared
  Chromium and reads the visible
  text of the first 40 rows of the conversation list (same CDP calls as the profile check: no thread is
  opened, no clicks, no typing, no scrolling). A conversation whose preview does not start with "Sie:" /
  "You:" ends with their message; when its name matches exactly one waiting contact (first and last name,
  titles ignored), that contact becomes `replied` with `reply: {detected_at, source: "inbox",
  previous_status}`. An answer to the connection note (status `ready`) counts too. Names shared by two
  contacts or two conversations, and contacts without a known name, are left to the human. The list's
  text (names and previews of every conversation, including people not in outreach) stays in memory for
  that check and is never stored, logged or returned.
  All automated reads are confined to `OUTREACH_ACTIVE_HOURS`=8–20 local time (set `TZ`/`BROWSER_TZ`,
  default `Europe/Berlin`); outside it work is deferred to the next window plus a random offset.
  An empty or unreadable conversation list is treated as a failed look and backs off exponentially
  (12 h → 24 h → 48 h) instead of retrying twice a day forever. A login wall pauses for 6 h, a
  challenge/rate-limit page (`OUTREACH_BLOCK_PAUSE_HOURS`=12) for 12 h; both are persisted across
  restarts and cleared by `POST /api/v1/outreach/resume` after a human logs in via noVNC. Nothing tries
  to get around a wall. `OUTREACH_REPLY_CHECK=0` turns the check off.
- Templates ("Vorlagen", `/api/v1/templates` CRUD, UI: „Vorlagen“): `{name, topic?, message, followups: [{after_days, message}], is_default}`. In `message` and follow-ups, `{{name}}`, `{{vorname}}`, `{{nachname}}`, `{{firma}}`, `{{rolle}}`, `{{website}}` come from the contact; any other `{{…}}` is an instruction for the model (e.g. `{{Eisbrecher mit Bezug zur Firma, 1–2 Sätze}}`), which writes only that field from the firm website (findings as usual); everything else stays word for word (stray markers are stripped, a field left empty sets `error: field_missing`). A template of variables only costs nothing. Without placeholders `template` stays free guidance as before. A search gets a template with `POST /api/v1/discoveries?template_id=…` (a "campaign" = search + template); its leads carry `template_id`, and `POST /api/v1/outreach` with `template_id` copies the template into the contact (later edits of the template do not change existing drafts). One template is always the default.
- Follow-ups: after the human copied the message (`status: sent`), follow-up *n* is due `after_days` after `sent_at` while no answer came, in order; `POST /api/v1/outreach/{id}/followups/{n}/sent` records that it was copied into LinkedIn (`409` out of order or after a reply), `PATCH` with `followup_index` + `followup_message` edits one. `/api/v1/actions` lists due ones as `send_followup`; a reply stops them.
- `POST /api/v1/outreach/{id}/reply-handled` takes an answer off the to-do list after you replied (`reply.handled_at`, status stays `replied`).
- `POST /api/v1/outreach/{id}/replied` marks an answer by hand; `POST /api/v1/outreach/{id}/no-reply`
  undoes a wrong one (back to the previous status). Known false positive: an older conversation with that
  person where they wrote last. `/api/v1/status` reports `reply_check_enabled`, `last_inbox_check`,
  `next_inbox_check`, `inbox_reads_today`, `invite_check_enabled`, `last_invite_check` (last complete
  look), `next_invite_check`, `invite_reads_today`, `invite_daily_cap`, `paused_until`,
  `connect_enabled`, `connect_last_24h` and `connect_daily_cap`.
- Calibrate the invitation look once after logging in:
  `docker compose exec session python3 /opt/browser-box/discovery-api/outreach.py invitations-check` prints
  counts only (`sent_listed`, `connections_listed`, `contacts_waiting`, `contacts_pending`,
  `contacts_connected`, `login_wall`) and changes nothing. If `sent_listed` is 0 while you have pending
  invitations, LinkedIn's markup differs from the expected cards and nothing is detected until it is
  adapted. The card handling was tested on a synthetic page in headless Chromium, not on LinkedIn.
- Calibrate the reply check once on the real page after logging in:
  `docker compose exec session python3 /opt/browser-box/discovery-api/outreach.py inbox-check` prints counts
  only (rows read, conversations, last message ours/theirs, contacts waiting, contacts with a reply) and
  changes nothing. If `rows_read` is 0 while the inbox has conversations, LinkedIn's markup differs from
  the expected thread links and the check finds no replies until it is adapted. The markup handling was
  tested on a synthetic page in headless Chromium, not on LinkedIn.
- This reads your own inbox by automation, which LinkedIn's terms do not allow; the account risk stays
  with you. The check keeps to what a person does and makes no attempt to disguise automation.
- `GET /api/v1/outreach/{id}`, `GET /api/v1/outreach?status=ready`, `DELETE /api/v1/outreach/{id}`.
  Records (incl. template) stay in the private `outreach.sqlite3` (0600) for 180 days.
- Legal: drafts do not establish consent; LinkedIn's terms and UWG/GDPR duties stay with the sender.

## Agent API: actions, events, campaigns, webhooks

These endpoints are built for an external agent (or the UI) to act proactively without polling every list.
All of them carry ids/statuses only; names, URLs and message text stay in the existing records.

**Actions** — `GET /api/v1/actions?kind=&limit=`
An ordered next-best-action list: `fix_session` (paused browser), `send_connection` (failed),
`connect_manually` (human: a look at sent invitations after creation did not list it),
`check_acceptance` (due), `retry_research` (failed), `review_message` (ready draft), `handle_reply`,
`review_expired`, `review_job`, `review_campaign`. Each item has `priority`, `why`, `actor`
(`agent` may call the endpoint, `human` must decide/send) and `suggested_call`. The list never triggers
work by itself.

**Events** — `GET /api/v1/events?since=<cursor>&limit=`
Change feed over outreach, discovery, campaign and pause/resume state. Use `next_cursor` as the next
`since`. Boundary items are not repeated (`updated_at > since`); poll at most every 10 s, back off to
60 s. Payload: `{type, ref_id, status, updated_at, campaign_id?, connect?, connection?, error?}`.

**Campaigns** — `POST /api/v1/campaigns`, `GET /api/v1/campaigns`, `GET /{id}`,
`POST /{id}/pause|resume|cancel`, `DELETE /{id}`.
A campaign runs one discovery job (`criteria`), then creates one outreach contact per qualified lead
that passes the registry, respecting `max_contacts`, then the normal worker connects, waits for
acceptance, researches and drafts. `status`: `queued` → `discovering` → `outreaching` →
`awaiting_review` (all automation done, drafts exist) or `done`; plus `paused`, `failed`, `cancelled`.
`counts`/`connect_counts`/`cost_usd` are the campaign funnel. Contacts carry `campaign_id`; filter with
`GET /api/v1/outreach?campaign_id=…`. Pausing stops further fan-out; cancelling also cancels the
discovery job. Blocked registry people are skipped, never overridden.

**Review edits** — `PATCH /api/v1/outreach/{id}` with `connection_note`, `message` or `topic`.
It only edits the draft (note only before the request was sent, message only from `researching` on) and
never sends anything.

**Webhooks** — `POST /api/v1/webhooks {url, secret?, events?}`, `GET /api/v1/webhooks`,
`DELETE /api/v1/webhooks/{id}`.
Only public `https` endpoints. The secret is returned once (or supplied by you, min. 16 chars). Delivery:
POST JSON `{"events":[…]}` with `X-Hermes-Signature: sha256=<HMAC-SHA256 over the raw body>`; no
redirects; retries with exponential backoff; a cursor per webhook avoids duplicates. Private/loopback
targets are refused at delivery.



### Operating limits that actually reduce block risk

- **One stable IP per account session.** Do not rotate residential proxies while logged in: an IP change
  mid-session is itself a checkpoint trigger. A datacenter IP is worse than a stable residential one, but
  rotating is worse than both. Log in from the place the box normally egresses from.
  Configure a dedicated static residential/ISP endpoint with `BROWSER_PROXY` (IP-whitelisted at the
  provider; Chromium cannot use `user:password` in `--proxy-server`). When set, the browser also gets
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`. See the root README for verification steps.
- **Never solve or bypass a CAPTCHA/challenge.** Stop, wait out the pause, and log in by hand via noVNC if
  needed. Do not export/replay cookies between environments; the persistent Chromium profile is the
  supported (and safer) path.
- **Environment parity.** `TZ` (default `Europe/Berlin`) and `BROWSER_LANG` (default `de-DE`) are set for
  the container/browser, and fonts (`fonts-liberation`, `fonts-noto-color-emoji`) are installed so the
  server looks like a normal desktop. Do not fake a Windows UA on a Linux kernel: the mismatch is a
  stronger signal than the honest Linux UA.
- **Volume.** 10 profile views/day, at most 2 inbox looks/day, irregular 2–5 min spacing, working hours
  only. The forum advice of "80 requests/hour" or "100 actions/day" is scraping advice and will get a
  logged-in account limited.

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
