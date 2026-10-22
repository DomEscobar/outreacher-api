---
name: outreacher
version: 1.0.0
description: Steer the Outreacher box over its HTTPS API - templates, searches/campaigns, registry, daily briefing. The human does every click on LinkedIn.
---
# Operating the Outreacher box

You run campaigns for the human through the box's API. The box finds leads, notices invitations,
acceptances and replies, and writes message drafts from templates. **The human does every click on
LinkedIn** (connect, paste and send messages and follow-ups) on the UI start page „Heute“. You prepare,
watch and brief. The full contract is `runtime/discovery-api/AGENT-GUIDE.md` in the repository; it wins
over this skill if they ever differ.

## Access

- `OUTREACHER_URL` (HTTPS origin, e.g. `https://vps.example.org:8443`), `OUTREACHER_USER`,
  `OUTREACHER_PASSWORD` come from your environment. Never print, log or echo them, never put them in URLs.
- Call the API with curl, HTTP Basic auth, `Content-Type: application/json`, and **no** `Origin` header:

```sh
api() { curl -sS -u "$OUTREACHER_USER:$OUTREACHER_PASSWORD" -H 'Content-Type: application/json' "$@"; }
api "$OUTREACHER_URL/api/v1/status"
```

- Schema: `$OUTREACHER_URL/api/v1/openapi.json`. Never script the UI (`/ui/`) or the browser.

## Never

- Click, connect, message, like, follow or post on LinkedIn, or drive any browser.
- Call `/opened`, `/sent`, `/followups/{n}/sent`, `/replied`, `/reply-handled` — those record what the
  human did. Do not call `sync-invitations` or `check-connection` in loops.
- Retry around `blocked`, `429`, login walls or `status.outreach.paused_until`: stop and tell the human.
- Remove `do_not_contact`, ignore registry blocks, or use `allow_same_company` without the human's yes.
- Forward lead lists, inbox text or personal data outside the box. Report names only to the human.

## Daily briefing (once in the morning, plus on webhook events)

1. `GET /api/v1/status` — if `outreach.paused_until` is set: tell the human „LinkedIn braucht dich:
   im Desktop einloggen“ and stop. If `search.configured` is false, mention it once.
2. `GET /api/v1/actions?limit=50`, group by `type`, look up names with `GET /api/v1/outreach/{ref_id}`.
3. Brief the human in German, short, most urgent first, for example:
   „Heute für dich: 1 Antwort (Erika Musterfrau), 2 Nachrichten zum Senden, 1 Follow-up fällig,
   5 Leads zum Vernetzen. Alles auf „Heute“.“
   Map: `handle_reply` → Antworten, `review_message` → Nachrichten, `send_followup` → Follow-ups,
   `connect_manually` → zu vernetzen, `fix_session` → Login nötig, `retry_research` → Entwurf neu.
4. Agent-actor items you may handle yourself: `retry_research` (`POST /outreach/{id}/accepted`, once).

## New campaign („Kampagne“ = target group + template)

1. Agree with the human: who (branch keywords, region, roles), how many, which template.
2. Template: `GET /api/v1/templates`; reuse one, or draft a new one **with the human** and save it:

```sh
api -X POST "$OUTREACHER_URL/api/v1/templates" -d @template.json
```

   Text in the human's voice, word for word. `{{name}}`, `{{vorname}}`, `{{nachname}}`, `{{firma}}`,
   `{{rolle}}` come from the profile; any other `{{…}}` is an instruction the box's model fills per
   person, e.g. `{{Eisbrecher mit Bezug zur Firma, 1–2 Sätze}}` or
   `{{Anrede mit Frau/Herr und Nachname}}`. Follow-ups: `[{"after_days": 10, "message": "…"}]`.
   Never invent claims, numbers or references.
3. Search with the template (the leads land on „Heute“ and get that template when the human connects):

```sh
api -X POST "$OUTREACHER_URL/api/v1/discoveries?template_id=$TEMPLATE_ID" -d '{
  "source": "search", "keywords": ["Steuerberater"], "locations": ["München"], "limit": 20,
  "budget": {"seconds": 1800, "max_cost_usd": 0.5}}'
```

   Poll `GET /api/v1/discoveries/{id}` after 5 s, 10 s, 20 s, then every 60 s until it is not
   `queued`/`running`. Report the count, not the names list. `partial` is fine; never re-run to pad.
4. Bulk only when the human asks for it: `POST /api/v1/campaigns` with `criteria`, `template_id`,
   `max_contacts` prepares contacts directly (they appear on „Heute“ as „vorbereitet“).
   Keep it to what the human can connect: about 20 per day.

## Drafts

You may improve a draft or follow-up before the human sends it (`PATCH /api/v1/outreach/{id}` with
`message`, or `followup_index` + `followup_message`). Keep the template's fixed text unless the human
asked otherwise; never add facts that are not in the contact's `findings`.

## Registry

Before preparing contacts yourself: `POST /api/v1/registry/check {"items": [...]}` (≤ 500). When the human
says someone is a customer or must not be contacted: `POST /api/v1/registry` with `customer` or
`do_not_contact` and `source: "hermes"`.

## Stop signals

`401` (wrong credentials), `403 origin_forbidden` (you sent an Origin header), `404`, `409 person_*`/
`company_*` (registry says no), `429 queue_full` (wait 2 min, retry once), job `blocked`,
`provider_error` / `*_http_402` (credits), `paused_until`: stop that line of work and tell the human.
