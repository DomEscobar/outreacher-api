# Agent guide: using this box without getting the account limited

This appliance does **on-demand professional discovery** and **outreach bookkeeping**. The box itself
sends exactly one connection request per contact (with an optional note), paced, capped and only inside
the working-hours window. It never sends messages, InMails, comments, likes, follows or posts: every
message after acceptance is a draft for a human. Everything in this guide is a **hard rule for agents**,
not a suggestion.

If an agent cannot follow a rule, it must stop and ask the human.

---

## 1. Access

- Base URL: the HTTPS origin printed by the wizard (for example `https://vps.example.org:8443`).
- Authentication: HTTP Basic, user `dom`, password chosen in the wizard. Never put credentials in
  URLs, query strings, request bodies, logs, chat or screenshots.
- Browser clients (`fetch`/XHR from the authenticated UI origin) must send
  `Origin: <exact HTTPS origin>` on `POST`/`PUT`/`PATCH`/`DELETE`.
  Scripts, curl and server-to-server agents must send **no** `Origin` header.
- Request body limits: 16 KiB normally, 1 MiB under `/api/v1/registry*` (max 500 items per call).
  Larger bodies return `413 request_too_large`.
- Validation errors return `422 {"detail": "invalid_request"}` and never echo your input.
- Responses are `Cache-Control: no-store`.
- Interactive schema: `/api/v1/docs`; OpenAPI: `/api/v1/openapi.json` (authenticated).

---

## 2. Absolute rules (MUST)

1. **No agent-driven mutations.** The box sends the connection request itself (see `connect.py`); an
   agent must never add or trigger any other send, InMail, comment, like, follow or post. The `message`
   after acceptance is a draft for a human, and `do-not-contact` stays permanent.
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
| POST | `/api/v1/discoveries` | start a discovery job | `202`; body = `Criteria`; `429 queue_full` |
| GET | `/api/v1/discoveries/{id}` | job status + result | poll with backoff |
| POST | `/api/v1/discoveries/{id}/cancel` | cancel queued/running job | terminal jobs unchanged |
| DELETE | `/api/v1/discoveries/{id}` | delete job/result | `204` |
| GET | `/api/v1/discoveries` | job summaries | for dashboards, not polling loops |
| GET | `/api/v1/leads` | delivered leads | filters: `verification`, `q`, `limit` |
| GET | `/api/v1/status` | health, queue, outreach pause, registry counts | do this before a campaign |
| GET | `/api/v1/actions` | prioritized next actions (agent + human) | poll with backoff; do not loop |
| GET | `/api/v1/events?since=` | change feed with cursor | no personal content; use instead of polling lists |
| POST | `/api/v1/campaigns` | one call runs discovery → outreach → connect → acceptance → draft | `201`; per-campaign funnel |
| GET | `/api/v1/campaigns` / `{id}` | campaign list / funnel | filter contacts with `/outreach?campaign_id=` |
| POST | `/api/v1/campaigns/{id}/pause\|resume\|cancel` | campaign control | terminal states unchanged |
| POST | `/api/v1/webhooks` | signed push of events to your agent | secret returned once; https only |
| POST | `/api/v1/outreach` | create a contact + connection-note draft | `201`; `409` if exists/blocked |
| PATCH | `/api/v1/outreach/{id}` | edit note/message draft (review) | never sends |
| GET | `/api/v1/outreach` | list contacts | optional `?status=`, `?campaign_id=` |
| GET | `/api/v1/outreach/{id}` | one contact | 404 if unknown |
| POST | `/api/v1/outreach/{id}/sent` | human sent the message | do not call before it is true |
| POST | `/api/v1/outreach/{id}/replied` | human saw an answer | evidence required |
| POST | `/api/v1/outreach/{id}/no-reply` | undo a wrong reply | restores previous status |
| POST | `/api/v1/outreach/{id}/connect` | retry a failed/manual connection request | the box sends it, no agent |
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
        |                 |             |            |
        +--> expired      +--> failed <-+            +--> do_not_contact (permanent)
```

Rules:

- One contact per LinkedIn profile. A second `POST` returns `409 outreach_exists` with the id.
- `409` with `person_*`, `company_*` means the registry says no. Do not work around it with another
  profile spelling or `allow_same_company` unless the human explicitly approved it for that company.
- The agent creates the contact. The box sends the connection request (with `connection_note`) right
  after creation: `connect.state` goes `not_sent` → `sent`/`failed`. The agent must not send it again.
- After acceptance is observed, the contact moves to `connected`/research automatically. A human can
  confirm it with `POST /accepted`; do not mark it early.
- `POST /outreach/{id}/connect` only retries a `failed`/`manual` request. Never loop it.
- `ready` means a draft message exists. The human reviews and sends it; then `POST /sent`.
- Reply detection reads the inbox list at most twice a day by itself. Do not poll
  `check-connection` to simulate replies, and do not mark `replied` without evidence.
- `check-connection` is a deliberate human action: at most once per contact per 24 h, and never while
  `status.outreach.paused_until` is in the future.
- `do_not_contact` is permanent for every agent on the box. Only a human should remove it.
- After a human fixes a login/CAPTCHA via noVNC, call `POST /resume` once, then stop until the next
  scheduled read. Do not call it to bypass a pause you did not resolve.

---

## 6. Proactive agent loop (actions, events, campaigns, webhooks)

Use these instead of polling every list:

1. `GET /api/v1/status` — stop if `outreach.paused_until` is set.
2. `GET /api/v1/actions?limit=20` — ordered tasks. Each item has `type`, `ref_type`, `ref_id`, `why`,
   `actor` (`agent` may call the endpoint; `human` must decide/send) and `suggested_call`.
3. Execute only the calls you are allowed to make (`check-connection`, `connect` for `failed`/`manual`,
   `accepted` for failed research, `resume` only after a human fixed the session). Never call `/sent`
   unless a human actually sent the message.
4. `GET /api/v1/events?since=<next_cursor>` (or a webhook) and wait for the next change.

**Campaigns** are the bulk primitive: `POST /api/v1/campaigns` with `criteria` + `topic` (+ templates,
`max_contacts`, `auto_connect`). The box submits the discovery job, creates one outreach contact per
qualified lead that passes the registry, and lets the normal worker connect/research. Poll the campaign
(`status`, `counts`, `contact_ids`) or subscribe to events; review drafts via
`/api/v1/outreach?campaign_id=<id>` and `PATCH /api/v1/outreach/{id}`. `paused`/`cancelled` campaigns do
no further work.

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
| Connection requests (box) | ≤`OUTREACH_CONNECT_DAILY_CAP`=15/day, ≤`OUTREACH_CONNECT_WEEKLY_CAP`=60/week, irregular spacing, working hours |
| `POST /outreach/{id}/connect` | only for `failed`/`manual`; never in a loop |
| Outreach creation | only after a registry check |
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
