# AGENTS.md

Read this before using or changing the appliance. The full contract for agents that call the API is
in [`runtime/discovery-api/AGENT-GUIDE.md`](runtime/discovery-api/AGENT-GUIDE.md).

## VPS residential egress — one-time setup (agent-runnable)

Goal: one **dedicated, static residential/ISP IP per LinkedIn account**, in the account's country
(Germany), stable across restarts. Never a rotating pool.

**Human buys, agent configures.** The agent cannot purchase a proxy. Ask the human to:
1. Buy a **static/dedicated ISP or residential** endpoint in Germany (e.g. `iproyal.com` "Royal ISP",
   `webshare.io` "ISP proxies", `proxy-seller.com` "ISP", `brightdata.com`/`oxylabs.io` "ISP Proxies").
   One endpoint per LinkedIn account.
2. Enable **IP authentication / whitelist** at the provider (no proxy password). Chromium cannot use
   `user:password` in `--proxy-server`.

**Agent steps (run on the VPS, in the repo root):**

```sh
# 1. Show the human this IP and wait until they whitelisted it at the provider.
curl -s https://ifconfig.me; echo

# 2. Configure + test. The script masks credentials and writes .env (0600).
python3 setup_proxy.py

# 3. Recreate the container (not just restart) and verify the egress from inside it.
python3 setup_proxy.py --apply
docker compose exec session sh -c 'curl -s -x "$BROWSER_PROXY" https://ifconfig.me'
```

The proxy test must print an IP **different from the VPS IP** and an ISP/residential org. If it
prints the VPS IP, the whitelist or the endpoint is wrong; fix it, do not proceed.

**Verify in the browser (human, via noVNC):** open `https://browserleaks.com/ip` and
`https://dnsleaktest.com`; the IP must be the ISP/residential one, no WebRTC leak. Restart once
(`docker compose restart session`) and confirm the IP stays the same.

**Agent rules for this setup:**

- Never edit `BROWSER_PROXY` to a rotating or shared endpoint. One account = one stable IP.
- Never put credentials in `BROWSER_PROXY` and never echo `.env` or the proxy URL into logs/chat.
  If the provider requires a password, run a local forwarder (`gost`/`3proxy`) on `127.0.0.1` and
  point `BROWSER_PROXY` there.
- Never change the IP while logged in. If the egress IP changes, stop; the human logs in again via
  noVNC, then `POST /api/v1/outreach/resume` once.
- `BROWSER_TZ` (default `Europe/Berlin`) and `BROWSER_LANG` (default `de-DE`) must match the account
  and the proxy country.
- The script warns when the egress org looks like hosting/cloud. Treat that as a failed setup for
  LinkedIn, even if the IP differs from the VPS IP.

**Troubleshooting**

| Symptom | Fix |
|---|---|
| Proxy test returns the VPS IP | IP not whitelisted / wrong host:port / wrong protocol |
| `user:password` rejected | Use IP authentication or a local forwarder on `127.0.0.1` |
| Container still has no `BROWSER_PROXY` | Recreate: `docker compose up -d --force-recreate session` |
| SOCKS endpoint | Chromium supports it; verify in `browserleaks.com/ip`, stdlib test is skipped |
| Egress org contains hosting/cloud | Different product: dedicated ISP or mobile, not datacenter |
| IP changes after restart | Endpoint is not static/dedicated; do not log in until it is |

## Documented behaviours

- Agent API contract: [`runtime/discovery-api/AGENT-GUIDE.md`](runtime/discovery-api/AGENT-GUIDE.md)
- Skill for an external Hermes agent that steers the box over the API:
  [`operator-skills/outreacher/SKILL.md`](operator-skills/outreacher/SKILL.md) (copy to
  `~/.hermes/skills/outreacher/`; env `OUTREACHER_URL`, `OUTREACHER_USER`, `OUTREACHER_PASSWORD`).
  Deliberately outside `skills/`, which is mounted into the box's own discovery agent.
- Agent primitives: `GET /api/v1/actions`, `GET /api/v1/events`, `POST /api/v1/campaigns`,
  `POST /api/v1/webhooks`, `PATCH /api/v1/outreach/{id}` (see the guide, section 6)
- API and operating limits: [`runtime/discovery-api/README.md`](runtime/discovery-api/README.md)
- Block prevention / VPS setup: root [`README.md`](README.md)
- Test report: [`runtime/discovery-api/TEST_RESULTS.txt`](runtime/discovery-api/TEST_RESULTS.txt)

## Test command

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=runtime/discovery-api \
  python3 -m unittest discover -s runtime/discovery-api/tests -p 'test_*.py' -v
```
