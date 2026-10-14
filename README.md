# Hermes Browser Box v6 — working source

This v6 working copy is not claimed published in Brain or deployed. Brain canonical
source remains v4; inherited test reports describe earlier versions, not v6 proof.

## Try on your own PC

```sh
bash setup.sh --local --plan
bash setup.sh --local
```

Read [LOCAL-PC.md](LOCAL-PC.md) first for Windows WSL2 Ubuntu, Linux and macOS
requirements, local CA trust and the masked-password API launcher. Local mode
installs no host dependencies and never asks for a public address. It publishes
only loopback HTTPS, with private API/CDP/VNC behind mandatory authentication.
Each fresh source directory generates its own persisted Compose project, image
tag and volumes. Do not copy any VPS `.env`, `.state`, cookies or provider volume.

## Start

```sh
bash setup.sh
```

Without `--local`, the interactive wizard provisions an isolated Hermes/browser appliance on an existing Linux VPS. It does not buy a VM/domain, import another Hermes profile, alter the firewall, or stop existing web servers.

## Automatic address and port defaults

- Detect public interface addresses and the VPS FQDN. Propose the FQDN only when its resolved addresses match a detected public address; otherwise propose the public IP. If no global interface address exists, a bounded request to api.ipify.org is used as a NAT fallback.
- Domain or IP is shown in the prompt as a default. Enter accepts it; manual override remains possible.
- Check whether TCP ports are locally bindable on IPv4 and IPv6. This is not an Internet reachability or firewall test.
- If a DNS hostname and free standard ports 80/443 are available, propose port 443 with ACME TLS.
- Otherwise propose a separate locally free port, preferring 8443, then 9443, 10443 and a bounded fallback range. An occupied user-selected port causes a new suggestion rather than stopping another service.
- No listening socket is retained during a probe. Port conflicts can still arise between probing and Docker startup; fail visibly instead of displacing another service.

## TLS on a custom port or IP

The isolated custom-port profile maps only the selected host port to Caddy's internal 443. It does not claim host ports 80/443. Caddy uses an internal CA for this mode, so the browser displays a certificate warning unless the CA is trusted manually.

**Encrypted HTTPS is not automatically publicly trusted HTTPS.** The wizard explains this and requires explicit confirmation before exposing an internal-CA test endpoint. Do not blindly dismiss warnings before sensitive logins; verify/trust your server's certificate or use an existing trusted proxy/VPN. For long-lived production use, arrange trusted certificates/identity protection separately.

ACME mode uses standard ports 80/443 and normal TLS verification. This implementation does not silently provision a DNS-challenge plugin or reuse another server's private certificate keys. A custom port does not by itself solve ACME validation.

The final URL includes the chosen port, for example `https://vps.example.org:8443/`, or brackets for an IPv6 address. A local readiness request verifies the running service's password protection; external reachability, provider firewall/security-group rules and trusted TLS are reported separately. No firewall rules are automatically added.

## Included components

- Pinned official Hermes image; non-root UID 10000, init process, resource limits and no host Docker socket.
- Shared visible Chromium browser with CDP, Xvfb, Openbox and Xterm.
- noVNC/WebSocket remote desktop, protected by Caddy HTTP Basic Auth and TLS.
- Persistent isolated state/browser profile and explicit clean browser shutdown.
- Skills `decision-maker-discovery` and `session-safety`.
- Host provisioning/network helpers and unit/integration tests.

## Guided setup

1. Check Docker/Compose. On a new Debian/Ubuntu host, optional installation from Docker's official APT repository requires confirmation and root. Existing/conflicting installations are not uninstalled or replaced automatically.
2. Choose local/SSH-tunnel access or public VPS access; accept or override detected address/port defaults.
3. Enter your browser-access password privately in the terminal, at least 16 characters. No password is printed or accepted in chat.
4. Build/start the container and selected edge profile.
5. Configure your own model credentials using the official Hermes provider/API-key wizard or Nous Portal login. No credentials from the existing bot are inherited.
6. Optionally configure tool-provider credentials. Reapply shared local-browser settings and enable browser/web/terminal/file/skills.
7. Show the URL and trust/firewall caveats; optionally launch Hermes.

Log into LinkedIn and complete 2FA manually in the remote browser while Hermes pauses. Login/containerization is not authorization for scraping, mass extraction or outreach. No automatic messages are sent.

## Commands

```sh
bash setup.sh --plan
python3 provision.py status
python3 provision.py stop
python3 provision.py shell
```

Explicit custom endpoint on an authorized host (interactive password prompt):

```sh
python3 provision.py configure --host vps.example.org --port 8443 --tls-mode internal
python3 provision.py up-public --confirm-public
```

IP addresses are accepted by `--host`. For standard ACME deployment use a real hostname and available 80/443, with `--tls-mode acme --port 443`. Never run both edge profiles against the same host port.

On a fresh copy, real isolated tests:

```sh
bash setup.sh --self-test
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Self-test credentials are generated only in .state/test-auth.json (0600), never printed. Test configuration refuses public deployment and custom-profile tests are hard-checked to bind only 127.0.0.1. Test containers are stopped afterward; volumes are retained, not deleted.

## Persistence and updates

The Brain ai-automation/hermes-browser-box contains earlier canonical source (v4), a versioned source manifest and restoration helper; this v6 working source is not yet published there. Restore at one canonical head, verify every file fingerprint and edit a fresh working copy. No chat attachment or GitHub repository is required.

The script refuses to overwrite an existing configuration unless --reconfigure is explicitly used. Source-code updates must not copy .env, .state, provider secrets or browser volumes. Restart the wizard after an update; an already-running Python prompt continues its old code until interrupted.

## Security limits

- Docker is not a VM boundary; Chromium uses --no-sandbox due container restrictions. Prefer a dedicated VM for stronger isolation.
- Browser cookies/profile and provider credentials persist but are not automatically encrypted at rest. Protect host access/backups.
- Skills are behavior guidance, not a technical credential-exfiltration guarantee. No session recording is enabled.
- Raw VNC, CDP and noVNC ports are not published. Only the selected authenticated edge is exposed.
- Basic Auth is not MFA or brute-force protection. For sustained public use add appropriate identity/IP/VPN protection.
- Sharing the owned browser profile with another independently launched browser is unsupported. Clean CDP shutdown preserves recent profile changes during controlled restarts; host power loss is not covered.

## Sources and tested scope

- https://hermes-agent.nousresearch.com/docs/user-guide/docker
- https://hermes-agent.nousresearch.com/docs/user-guide/features/browser
- https://caddyserver.com/docs/automatic-https
- https://caddyserver.com/docs/caddyfile/directives/basic_auth

Current local provisioning checks: `tests/LOCAL-TEST-RESULTS.txt`; see TEST-RESULTS.md for inherited earlier-version evidence. Real provider login, public ACME issuance, real LinkedIn login and external reachability are separate pilot steps, not implied by local Docker test success.

## Desktop transport / v4 correction

The desktop backend is native `/usr/bin/X0tigervnc` under Supervisor, not the daemonizing Perl wrapper or x11vnc. It listens on loopback only. `SecurityTypes None` and `UseBlacklist=0` apply solely to this private, deliberately passwordless backend; authenticated HTTPS at Caddy remains mandatory and unchanged. Do not apply those options to public or password-authenticated VNC services.

Healthchecks complete shared RFB negotiation and server initialization without requesting pixels. An open port, RFB banner or HTTP 101 alone does not prove a working desktop. `runtime/rfb_probe.py` validates a non-empty framebuffer directly or through the internal WebSocket bridge and logs only protocol/geometry/byte counts, never pixels or clipboard. The guarded localhost smoke test also checks the complete frame through TLS, Basic Auth and Caddy.

For a noVNC Connecting/Failed to connect state, inspect backend logs and test a complete RFB exchange. A banner-only disconnect loop can create false security failures. Use the foreground server, preserve volumes and Caddy credentials, and refresh the client after replacing the session service. Never expose raw VNC or CDP to solve this failure.

## On-demand Discovery API / v6 working source

The same authenticated HTTPS origin now serves `/api/v1/docs` and `/api/v1/openapi.json`. POST `/api/v1/discoveries` accepts flexible strict search criteria or a validated exact LinkedIn people-search URL and returns a job ID. GET `/api/v1/discoveries/{id}` returns status and validated JSON; cancellation and deletion endpoints are documented in the schema. No outreach sender is installed: nothing is ever sent, typed or clicked on LinkedIn. The only recurring task is the optional outreach connection check described below (read-only, daily, capped).

The v6 runtime adds bounded serialized research passes, strict observed role/sector qualification, evidence checking and honest partial outcomes; see its own contract and tests. Only one API research job drives the shared browser at a time. Read-only tool dispatch excludes arbitrary code, vault, click/type, terminal and sending. Normal accessible pagination is bounded; login/challenge/access limits stop the job. Results may be partial, blocked or failed; 15 is a target, never padded. Lead data stays privately in the volume with 30-day API retention; Hermes's own traces require their separate private retention policy. The skill is guidance, not an OS/network sandbox.

For fast lead lists use `"source": "search"`: the same endpoint runs a search-engine pipeline (LinkedIn profiles from Google results, roles confirmed on the firm's own website/imprint, `verification` tier per lead, `max_cost_usd` cap) without the browser agent or a LinkedIn login. It needs `TREG_TOKEN` (`docker compose exec session python3 /opt/browser-box/discovery-api/search_backend.py set-token`) and the configured OpenRouter key. Example request: `kanzlei-deutschland.json`, sent with `py local-api.py --request kanzlei-deutschland.json` (Windows) or `python3 local-api.py --request kanzlei-deutschland.json`.

Web UI: `https://<host>/ui/` (same password) shows overview, searches, all leads with evidence, and the outreach board with copy buttons; `/` still opens the remote desktop. Read-only overview endpoints for own tools: `GET /api/v1/discoveries`, `GET /api/v1/leads?verification=&q=`, `GET /api/v1/status`; `POST /api/v1/outreach/{id}/sent` marks a message as sent by you.

Shared registry (`/api/v1/registry`, `/api/v1/registry/check`): one lookup/suppression list keyed by LinkedIn profile and company domain/name. Other agents check and register their contacts there (batch up to 500); search never re-delivers known people, skips blocked companies and returns at most `max_per_company` (default 1) leads per company; outreach refuses blocked people/companies and supports a permanent "do not contact".

Outreach (`/api/v1/outreach`): register a LinkedIn profile plus firm website/name, a `topic` and a `template`; you get a short connection note to send yourself. Once a day the box checks read-only (background browser tab, no clicks, daily cap, pause on login walls) whether the connection was accepted; then it researches the firm for the topic (findings with verbatim quotes and URLs) and drafts the message following the template (`status: ready`). Sending stays manual. Disable the check with `OUTREACH_BACKGROUND=0`.

See `runtime/discovery-api/README.md` for the exact request/response contract, CA-scoped curl and known provenance limitations. Model use consumes the configured provider's quota/balance. Source and synthetic fixtures may be saved in Brain; actual lead results, credentials and volumes must never be included.
