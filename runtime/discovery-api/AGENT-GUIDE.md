# Agent guide: using this box without getting the account limited

This appliance does **on-demand professional discovery** and **outreach bookkeeping**. Division of labour:

- **Agent (you):** templates, searches/campaigns, registry, monitoring, briefing the human, editing drafts.
- **Human:** every click on LinkedIn — "Vernetzen" (connect) and pasting/sending messages and follow-ups —
  guided by the UI start page „Heute“.
- **Box:** finds leads, notices invitations and acceptances (reads the sent-invitations list and the
  newest connections about three times a day), writes drafts from templates, notices replies (inbox list,
  at most twice a day), shows due follow-ups.

The box never sends messages, InMails, comments, likes, follows or posts, and by default it does not send
connection requests either (opt-in `OUTREACH_AUTO_CONNECT=1`, paced and capped). Everything in this guide
is a **hard rule for agents**, not a suggestion.

If an agent cannot follow a rule, it must stop and ask the human.

---

## 1. Access

- Base URL: the HTTPS origin printed by the wizard (for example `https://vps.example.org:8443`).
- Authentication: HTTP Basic, user `dom`, password chosen in the wizard. Never put credentials in
  URLs, query strings, request bodies, logs, chat or screenshots.
- Browser clients (`fetch`/XHR from the authenticated UI origin) must send
  `Origin: <exact HTTPS origin>` on `POST`/`PUT`/`PATCH`/`DELETE`.
  Scripts, curl and server-to-server agents must send **no** `Origin` header.
- Request body limits: 16 KiB normally, 1 MiB under `/api/v1/registry*` (max 500 items per call) and
  `/api/v1/templates*`.
  Larger bodies return `413 request_too_large`.
- Validation errors return `422 {"detail": "invalid_request"}` and never echo your input.
- Responses are `Cache-Control: no-store`.
- Interactive schema: `/api/v1/docs`; OpenAPI: `/api/v1/openapi.json` (authenticated).

---

## 2. Absolute rules (MUST)

1. **No agent-driven mutations on LinkedIn.** Connecting, messaging and follow-ups are done by the human
   (or, opt-in, the box's paced connection request). An agent must never add or trigger any send, InMail,
   comment, like, follow or post, and never marks something as done that the human did not do
   (`/opened`, `/sent`, `/followups/{n}/sent`, `/replied`, `/reply-handled` are human facts).
   Drafts are for a human, and `do-not-contact` stays permanent.
2. **Never evade a barrier.** A login wall, CAPTCHA, `/checkpoint`, `/authwall`, `unusual activity`,
   `rate limit` or HTTP 429 means: stop. Do not solve CAPTCHAs, rotate proxies, change fingerprints,
   replay cookies or retry with different filters. Escalate to a human.
3. **One stable egress IP per LinkedIn account.** No proxy rotation while a session is active.
   No parallel browser sessions on the same profile. Do not move a logged-in profile between
   environments or IPs.
4. **Respect pacing.** Discovery jobs are serialized by one worker; the queue admits at most 8.
   Submit only what you need. Poll a job at most once every 5 seconds, then back off.
5. **Minimise data.** Do not persist, log or forward inbox text, names/previews of people who are not
   contacts, full lead exports or provider payloads. The server already keeps records private;
   your side must too.
6. **Registry first.** Before any outreach, `POST /api/v1/registry/check` for the person/company.
   Honour `blocked`, `do_not_contact`, `customer` and every other blocking status permanently.
7. **Human review.** Every lead and every message is reviewed by a human before it is used or sent.
8. **Stop honestly.** `blocked`, `partial`, `provider_error`, `*_http_402`, `429` and repeated 5xx
   are stop signals, not invitations to retry harder.
9. **Do not drive the browser yourself.** No CDP, noVNC automation, Selenium or OS-level clicking from
   an agent. The box owns Chromium through its lock and its read-only CDP reader.
10. **Budgets are limits.** Set `budget.seconds`, `budget.max_turns` and `budget.max_cost_usd`.
    Never try to work around turn, cost or output caps.

---

## 3. Endpoint map

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET/POST | `/api/v1/templates` | list / create message templates | see section 5a |
| GET/PUT/DELETE | `/api/v1/templates/{id}` | one template | edits never change existing drafts |
| POST | `/api/v1/discoveries?template_id=` | start a discovery job (= a campaign when a template is given) | `202`; body = `Criteria`; `429 queue_full`; `404 template_not_found` |
| GET | `/api/v1/discoveries/{id}` | job status + result | poll with backoff |
| POST | `/api/v1/discoveries/{id}/cancel` | cancel queued/running job | terminal jobs unchanged |
| DELETE | `/api/v1/discoveries/{id}` | delete job/result | `204` |
| GET | `/api/v1/discoveries` | job summaries | for dashboards, not polling loops |
| GET | `/api/v1/leads` | delivered leads (+ `template_id`, outreach state) | filters: `verification`, `q`, `limit` |
| GET | `/api/v1/status` | health, queue, outreach pause, registry counts | do this before a campaign |
| GET | `/api/v1/actions` | prioritized next actions (agent + human) | poll with backoff; do not loop |
| GET | `/api/v1/events?since=` | change feed with cursor | no personal content; use instead of polling lists |
| POST | `/api/v1/campaigns` | discovery → contacts prepared for the human → acceptance → draft | `201`; `template_id` or `topic`; per-campaign funnel |
| GET | `/api/v1/campaigns` / `{id}` | campaign list / funnel | filter contacts with `/outreach?campaign_id=` |
| POST | `/api/v1/campaigns/{id}/pause\|resume\|cancel` | campaign control | terminal states unchanged |
| POST | `/api/v1/webhooks` | signed push of events to your agent | secret returned once; https only |
| POST | `/api/v1/outreach` | prepare a contact for the human to connect | `201`; `template_id` or `topic`; `409` if exists/blocked |
| PATCH | `/api/v1/outreach/{id}` | edit note/message/follow-up draft (review) | never sends; `followup_index` + `followup_message` |
| GET | `/api/v1/outreach` | list contacts | optional `?status=`, `?campaign_id=` |
| GET | `/api/v1/outreach/{id}` | one contact | 404 if unknown |
| POST | `/api/v1/outreach/{id}/opened` | human opened the profile to connect | **UI only**, never an agent |
| POST | `/api/v1/outreach/{id}/sent` | human sent the message | do not call before it is true |
| POST | `/api/v1/outreach/{id}/followups/{n}/sent` | human sent follow-up *n* | in order; `409` otherwise; human fact |
| POST | `/api/v1/outreach/{id}/replied` | human saw an answer | evidence required |
| POST | `/api/v1/outreach/{id}/reply-handled` | human answered the reply | takes it off the to-do list |
| POST | `/api/v1/outreach/sync-invitations` | look at sent invitations + newest connections now | human-triggered; never poll |
| POST | `/api/v1/outreach/{id}/no-reply` | undo a wrong reply | restores previous status |
| POST | `/api/v1/outreach/{id}/connect` | retry a failed request the box sent | only with `OUTREACH_AUTO_CONNECT=1` |
| POST | `/api/v1/outreach/{id}/accepted` | human accepted the connection | starts research |
| POST | `/api/v1/outreach/{id}/check-connection` | manual read-only profile look | use sparingly |
| POST | `/api/v1/outreach/{id}/do-not-contact` | permanent opt-out | sticky; never undo casually |
| POST | `/api/v1/outreach/resume` | clear a persisted login/challenge pause | only after a human fixed it |
| DELETE | `/api/v1/outreach/{id}` | delete a contact | frees the person if still `in_outreach` |
| POST | `/api/v1/registry/check` | batch lookup (≤500) | call before contacting |
| POST | `/api/v1/registry` | batch upsert (≤500) | your own contacts/suppression list |
| GET | `/api/v1/registry` | list/export | filters `status`, `source`, `q`, `limit` |
| GET | `/api/v1/registry/{id}` | one entry | 404 if unknown |
| DELETE | `/api/v1/registry/{id}` | free a person/company | `204` |

`/ui/` is for humans only. Do not script it.

---

## 4. Discovery (`Criteria`)

Example:

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
  "budget": {"seconds": 180, "max_turns": 12, "max_cost_usd": 0.5}
}
```

- `source`:
  - `linkedin`: read-only browser agent on people search/profiles.
  - `web`: public-web research.
  - `search`: paid SERP + page extraction; supports `locations`, `min_verification`,
    `include_review`, `expand_terms`, `max_per_company`, `exclude_previous_companies` and
    `limit` up to 100. `keywords` are required.
- `limit`: 1–30 for `linkedin`/`web`, 1–100 for `search`.
- `budget.seconds`: 15–1800; `max_turns`: 1–120; `max_cost_usd`: >0 and ≤10.
- `search_url`: exactly one valid `https://www.linkedin.com/search/results/people/` URL; cannot be
  combined with filter fields. Only `keywords`, `title`, `geoUrn`, `currentCompany`, `network`,
  `origin`, `sid`, `page` (1–10) are allowed. Do not rewrite supplied opaque IDs.
- Statuses: `queued`, `running`, `succeeded`, `partial`, `blocked`, `failed`, `timed_out`.
  Only `succeeded` means the requested count was reached. `partial` and `blocked` are honest
  outcomes — never pad them or retry to force a count.
- Every factual lead field must be supported by verbatim evidence. Do not trust or forward
  `fit_reason` as fact; it is an inference.
- `error` values such as `provider_error`, `search_not_configured`, `budget_exhausted` and
  `*_http_402` mean: stop and tell the human (credits/config), do not loop.

Polling pattern: `GET` after 5 s, then 10 s, then 20 s, capped at 60 s, until terminal.
Cancel anything you no longer need.

---

## 5. Outreach state machine

```
awaiting_connection -> connected -> researching -> ready -> sent -> replied
        |                 |             |            |       |
        +--> expired      +--> failed <-+            |       +--> follow-ups 1..n (due after_days, until a reply)
                                                     +--> do_not_contact (permanent)
```

Who moves a contact forward:

| Step | Who | How the box knows |
|---|---|---|
| contact prepared | agent (`POST /outreach`, campaign) or human (UI „Vernetzen ↗“) | `connect.opened_at` empty = still on the human's „Heute“ list |
| connection request | human on LinkedIn | sent-invitations list → `connection.state: pending` |
| accepted | the other person | newest connections → `status: connected`, draft written → `ready` |
| message sent | human („Öffnen & kopieren“) | `POST /sent` from the UI |
| follow-up *n* | human, when due | `POST /followups/{n}/sent` from the UI |
| reply | the other person | inbox list → `replied`; human answers, then `reply-handled` |

Rules:

- One contact per LinkedIn profile. A second `POST` returns `409 outreach_exists` with the id.
- `409` with `person_*`, `company_*` means the registry says no. Do not work around it with another
  profile spelling or `allow_same_company` unless the human explicitly approved it for that company.
- The agent prepares the contact (`template_id` or `topic`; pass `name`/`headline` when known: no paid
  lookup). It then appears on the human's „Heute“ page as the next lead to connect, and in `/actions` as
  `connect_manually` (actor `human`). Never set `opened` and never call `/opened`: that is the human's
  click. By default the box does not send connection requests (`connect.state: manual`); only with
  `OUTREACH_AUTO_CONNECT=1` does it (`not_sent` → `sent`/`failed`). Never send one yourself.
- About three times a day the box reads the sent invitations and the newest connections: listed under
  sent → `connection.state: pending`, `connect.state: sent`; among the newest connections → `connected`,
  then research automatically. A human can confirm it with `POST /accepted`; do not mark it early.
  `POST /outreach/sync-invitations` runs that look now; it is a human action, do not poll it.
- `connect_manually` in `/actions` means the human still has to connect (not opened yet, or a look after
  opening did not list the invitation): include it in your briefing, do nothing else.
- `POST /outreach/{id}/connect` only retries a `failed` request sent by the box. Never loop it.
- `ready` means a draft message exists. You may improve it with `PATCH` (review help); the human copies
  and sends it, the UI then records `POST /sent`.
- `send_followup` in `/actions`: follow-up *n* is due (no answer `after_days` after the message). Same
  rules: you may edit its text, the human sends it.
- Reply detection reads the inbox list at most twice a day by itself. Do not poll
  `check-connection` to simulate replies, and do not mark `replied` without evidence.
- `check-connection` is a deliberate human action: at most once per contact per 24 h, and never while
  `status.outreach.paused_until` is in the future.
- `do_not_contact` is permanent for every agent on the box. Only a human should remove it.
- After a human fixes a login/CAPTCHA via noVNC, call `POST /resume` once, then stop until the next
  scheduled read. Do not call it to bypass a pause you did not resolve.

---

## 5a. Templates („Vorlagen“) and campaigns

A template is the human's message, word for word, with placeholders:

```json
POST /api/v1/templates
{"name": "Steuerberater – KI-Potenzialanalyse", "topic": "KI-Automatisierung für Steuerkanzleien",
 "message": "Hallo {{name}},\n\n{{Eisbrecher mit Bezug zur Firma, 1–2 Sätze}}\n\n…fixed text…\n\nViele Grüße, Dominic",
 "followups": [{"after_days": 10, "message": "Hallo {{name}},\n…"}, {"after_days": 20, "message": "…"}],
 "is_default": false}
```

- `{{name}}`, `{{vorname}}`, `{{nachname}}`, `{{firma}}`, `{{rolle}}`, `{{website}}` come from the contact.
  Any other `{{…}}` is an instruction for the box's model, which writes only that field (facts from the
  firm website). The rest is never rewritten. A field left empty sets `error: field_missing`.
- Exactly one template is the default (used for leads of searches without a template). Contacts keep a
  copy of the template at creation; editing a template only affects new contacts.
- Write templates in the human's voice and get their approval before using a new one. Do not invent
  claims, numbers or references for them.

A **campaign** is a target group plus a template:

- Light: `POST /api/v1/discoveries?template_id=<id>` with `Criteria`. The leads land on the human's
  „Heute“ page and get that template when the human clicks „Vernetzen ↗“.
- Bulk: `POST /api/v1/campaigns {"name", "criteria", "template_id", "max_contacts"}`. The box runs the
  search and prepares one contact per qualified lead (registry-filtered) with the template; they appear
  on „Heute“ as the next leads to connect. Prefer the light form unless the human asked for bulk.

---

## 6. Proactive agent loop (actions, events, campaigns, webhooks)

Use these instead of polling every list:

1. `GET /api/v1/status` — stop if `outreach.paused_until` is set.
2. `GET /api/v1/actions?limit=20` — ordered tasks. Each item has `type`, `ref_type`, `ref_id`, `why`,
   `actor` (`agent` may call the endpoint; `human` must decide/send) and `suggested_call`.
3. Execute only the calls you are allowed to make (`accepted` for failed research, `connect` for a
   `failed` box request, `resume` only after a human fixed the session, `PATCH` for draft edits).
   Human-actor items (`connect_manually`, `review_message`, `send_followup`, `handle_reply`,
   `fix_session`) go into your briefing for the human, with names from `GET /outreach/{id}`.
4. `GET /api/v1/events?since=<next_cursor>` (or a webhook) and wait for the next change.

Action types: `fix_session`, `send_connection` (failed box request), `connect_manually`,
`check_acceptance`, `retry_research`, `review_message`, `send_followup`, `handle_reply`,
`review_expired`, `review_job`, `review_campaign`. Due follow-ups change no record (they are due by
time), so check `/actions` at least once a day even when no event arrived.

**Campaigns** (see 5a): poll the campaign (`status`, `counts`, `contact_ids`) or subscribe to events;
review drafts via `/api/v1/outreach?campaign_id=<id>` and `PATCH /api/v1/outreach/{id}`.
`paused`/`cancelled` campaigns do no further work.

**Webhooks:** `POST /api/v1/webhooks {url, events, secret?}`. The secret is returned once; verify
`X-Hermes-Signature: sha256=<hex>` over the raw body. Delivery is retried with exponential backoff and
never follows redirects. Payloads contain ids/statuses only. Only `https` public endpoints are accepted.

Rate limits for the loop: actions/events at most once every 10 s (back off to 60 s), campaigns at most
1 in flight per agent, webhooks are pushed (do not also poll). All other limits from section 8 apply.

---

## 7. Error handling matrix

| Response / state | Agent action |
|---|---|
| `401` | Stop. Do not retry. Credentials or proxy auth is wrong. |
| `403 origin_forbidden` | Fix `Origin`: browser must use the exact origin, scripts must omit it. |
| `404` | Stop; refresh the id or drop the item. |
| `409 outreach_exists` | Reuse the returned id; do not create a duplicate. |
| `409 person_*` / `company_*` | Honour the registry. Escalate if the human wants an exception. |
| `413 request_too_large` | Reduce the body / batch size. |
| `422 invalid_request` | Fix the schema. Never guess fields or coerce types. |
| `429 queue_full` | Back off 30–120 s, retry once; then wait for a human. |
| `5xx` | Back off 5 s → 15 s → 60 s, max 3 tries, then escalate. |
| job `blocked` | Stop the campaign. Human review. |
| job `partial` | Accept what is supported; do not re-run to force a count. |
| `provider_error`, `*_http_402` | Stop spending, escalate (balance/config). |
| `status.outreach.paused_until` set | Stop all browser-triggering calls; escalate. |

Never log response bodies containing personal data. Log status codes and job ids only.

---

## 8. Self-imposed rate limits

| Action | Limit |
|---|---|
| Discovery submissions per agent | 1 in flight; ≤8 queued globally |
| Polling `GET /discoveries/{id}` | ≤1 per 5 s, then exponential backoff to 60 s |
| `POST /registry/check` / `POST /registry` | batch up to 500; ≤1 call per 10 s |
| `POST /outreach/{id}/check-connection` | human-triggered; ≤1 per contact per 24 h |
| Connection requests (box, opt-in `OUTREACH_AUTO_CONNECT=1`) | ≤`OUTREACH_CONNECT_DAILY_CAP`=15/day, ≤`OUTREACH_CONNECT_WEEKLY_CAP`=60/week, irregular spacing, working hours |
| `POST /outreach/{id}/connect` | only for a `failed` box request; never in a loop |
| Outreach creation | only after a registry check; prepare at most what the human can connect (about 20 per day) |
| Automated reads that open LinkedIn | 08:00–20:00 local time only (a human-triggered `check-connection` is exempt) |

The server additionally enforces: 10 profile views/day, at most 2 inbox looks/day, irregular
spacing, working-hours window, persisted pause on any challenge, and exponential backoff on an
empty/unreadable inbox. Do not try to raise these through retries.

---

## 9. Privacy and retention

- Jobs and dedup keys: 30 days. Outreach records: 180 days. Files are private (`0600`, dir `0700`).
- Do not copy personal data into public logs, issue trackers, chat or Brain. Do not send full lead
  lists outside the box.
- Inbox text is intentionally never stored by the box. Do not add code that stores it.
- Credentials belong in the private volume; never in request bodies or docs.

---

## 10. Pre-flight checklist

- [ ] `GET /api/v1/status` → search configured, `outreach.paused_until` is null.
- [ ] Auth works; correct `Origin` behaviour for your client type.
- [ ] Registry check done for every target.
- [ ] Budget set explicitly (`seconds`, `max_turns`, `max_cost_usd`).
- [ ] A human is available to log in / solve a challenge and to review drafts.
- [ ] One stable egress IP, no proxy rotation, no second browser session.
- [ ] You are not automating any click, type, send or scroll on LinkedIn.

---

## 11. When LinkedIn shows a wall

1. Stop every call that triggers browser work (`check-connection`; do not create new jobs).
2. Note time, endpoint and HTTP status. Do not capture screenshots containing other people's data.
3. Ask the human to log in via noVNC and complete 2FA manually.
4. Only after the human confirms: `POST /api/v1/outreach/resume` once.
5. If it recurs the same day, stop for the day. Do not rotate IPs or profiles.

---

## 12. Forbidden anti-patterns

- `undetected-chromedriver`, Selenium, headless mode, DevTools automation from an agent.
- Residential-proxy rotation, cookie export/replay, profile copying between machines.
- CAPTCHA/challenge solvers, "human-like" mouse/vision clickers.
- Scraping inbox threads, storing conversation previews, bulk-exporting personal data.
- Multiple accounts behind one IP, or one account behind changing IPs.
- Retrying `429`/`403`/`blocked` in a loop, or raising limits via parallel calls.
- Publishing leads, inbox text, tokens or passwords anywhere outside the private volume.

---

## 13. Minimal examples

Script (no `Origin`):

```sh
curl -u dom -H 'Content-Type: application/json' \
  -d '{"keywords":["kanzlei"],"titles":["Inhaber"],"limit":5,
       "budget":{"seconds":300,"max_turns":25,"max_cost_usd":0.2}}' \
  "$BASE_URL/api/v1/discoveries"
```

Browser (exact origin):

```js
await fetch('/api/v1/discoveries/JOB_ID', { credentials: 'include' });
await fetch('/api/v1/outreach/ID/sent', { method: 'POST', credentials: 'include',
  headers: { Origin: location.origin } });
```

Registry before contact:

```sh
curl -u dom -H 'Content-Type: application/json' \
  -d '{"items":[{"linkedin_url":"https://www.linkedin.com/in/example"}]}' \
  "$BASE_URL/api/v1/registry/check"
```

If `blocked` is `true`, do not create outreach.
