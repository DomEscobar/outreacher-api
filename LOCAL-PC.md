# Local PC quick start — v6 working source

## Requirements (install deliberately, before running)

- Bash, Python **3.10+** (stdlib only on the host), Docker CLI, a running **Linux**
  Docker engine and **Compose v2** (`docker compose`, not legacy `docker-compose`).
  No host Hermes install is needed. Internet is needed for registry pulls, image
  package build, provider login and research. Docker Hub rate limits may apply.
- Windows: use Docker Desktop with its WSL2 backend and **Ubuntu WSL2 integration
  enabled**. Run commands in the Ubuntu terminal, not PowerShell, CMD or Git Bash.
  Install Python in Ubuntu if missing (explicitly: `sudo apt update` then
  `sudo apt install python3`). Keep source under the WSL Linux home rather than
  `/mnt/c` for filesystem permissions/performance. Start Docker Desktop first;
  do not install a competing Docker daemon inside WSL. Open the resulting URL in
  your Windows browser; if localhost forwarding is disabled, fix WSL/Desktop
  integration rather than changing the binding to `0.0.0.0`.
- macOS: install/start Docker Desktop for the correct Intel or Apple Silicon
  architecture, and install Python 3.10+ deliberately if `python3` is missing.
  Run in Terminal with Bash. No Rosetta/emulated amd64 is required by this pin.
- Linux: install Docker Engine plus its Compose plugin, or Docker Desktop, by
  the official instructions. Your terminal must have Docker daemon permission.
  Docker-group membership effectively grants root capabilities; choose knowingly.
- Plan for **at least 10 GiB free disk**, preferably more for layers/builds and
  private data, **8 GiB host RAM**, and Docker VM allocation of roughly **6 GiB
  RAM / 2 CPUs or more**. Session limit is 4 GiB RAM, 2 CPUs, with 1 GiB shared
  memory; Caddy, build work and the host need extra headroom. Disk preflight does
  not measure Docker Desktop's separate VM disk allowance. A large first build
  and cold start may exceed the wizard timeout; inspect status/logs and retry,
  never delete volumes to “fix” it. Desktop sleep interrupts jobs.

Check from the same terminal:

```sh
python3 --version
docker info
docker compose version
bash setup.sh --local --plan
bash setup.sh --local
```

Use a fresh **sanitized source** directory: no copied `.env`, `.state`, VPS
credentials, browser cookies or volumes. Local mode never installs host packages,
looks up a public IP/FQDN, opens public ports, changes the firewall or deletes
volumes. Enter your own 16+ character password in masked terminal prompts; HTTP
Basic Auth username is `dom`. A random default instance name is saved in `.env`;
all Compose commands from this directory reuse its isolated project, `:v6` image
tag and persistent volumes. Existing unrelated project resources are refused.
Repeated setup resumes your configuration, not a new profile.

Your URL is typically `https://localhost:19443/`; the wizard may suggest another
free port. NoVNC shows the **container's browser**, not your normal PC browser.
Raw API 8081, noVNC 6080, VNC and CDP are never host-published. Configure your own
provider in the official container Hermes wizard; keys/OAuth and browser logins
stay in this appliance's private persistent volume. Provider setup may be skipped
for a browser-only test; successful setup exit is not proof a model works.
Provider calls/web tools can cost money. Log into LinkedIn and do 2FA yourself
inside noVNC while the agent pauses. Do not share browser control during API work.

## Local HTTPS and CA trust — not public ACME

Caddy creates a **unique private local CA**. The wizard exports only its public
root certificate to `.state/local-ca.crt` and verifies TLS with that scoped CA.
This does **not** install a trusted certificate in your host/browser. A browser
warning is expected until you explicitly trust the correct CA. Do not disable
TLS verification globally or copy Caddy's private `root.key`.

If needed, export again after startup:

```sh
python3 provision.py export-ca
openssl x509 -in .state/local-ca.crt -noout -subject -fingerprint -sha256
```

Inspect the certificate/fingerprint from your own source directory before trust.
On macOS import the certificate into your login Keychain with Keychain Access
and explicitly choose its trust. On Windows copy **only this .crt** from WSL to
Windows and import into the current user's Trusted Root Certification Authorities
(certmgr.msc). On Linux use your browser's certificate/Authorities import UI;
Firefox may use a separate store on any OS. Trusting a root grants authority
beyond localhost in that store; prefer a dedicated test-browser profile where
available and remove its trust when finished. CA trust is never installed by the
script. Verify local browser guidance for your version. Do not blindly bypass a
warning before sensitive logins. If the volume is replaced, the CA changes.

## One explicit API submission and one GET

The launcher uses Python stdlib, prompts via masked `getpass`, holds Basic Auth
in process memory (not argv, shell history, environment or a password file),
uses the exact local CA, bypasses HTTP proxy environment for loopback, and refuses
redirects/non-local origins. It does not poll, schedule, send messages or follow
up automatically. With no action it GETs the OpenAPI JSON (no model call).

```sh
python3 local-api.py
# Inspect/edit this bounded example first; POST can consume provider credits:
python3 local-api.py --request local-request.example.json
# Replace JOB_ID with the ID returned by the POST; GET is one request only:
python3 local-api.py --job JOB_ID
# Optional explicit endpoint/CA, e.g. after copying public certificate only:
python3 local-api.py --base https://localhost:19443 --ca .state/local-ca.crt --job JOB_ID
```

The example targets three profiles, not guaranteed results. Runtime may return
partial/blocked/failed and never pads unsupported contacts. Plain criteria JSON
is not an arbitrary prompt. Exact schema: `/api/v1/docs` and runtime README.
Returned real data is private; do not publish it with source or test fixtures.

## Resume, stop, diagnose

```sh
python3 provision.py status
python3 provision.py stop
python3 provision.py up-local
# Configure own provider later, in its official interactive dialog:
docker compose exec session hermes setup model
docker compose exec session /opt/browser-box/launch-hermes.sh
docker compose logs --tail 100 session local
```

Stop retains volumes and all your private credentials/profile/data. Never use
`down -v` as routine cleanup. Do not share this state with a VPS or another
Chromium process. Docker is not a VM sandbox; Chromium runs without its native
sandbox in this appliance. Protect host access and backups.

## Pin and tested scope

The unchanged official index digest
`nousresearch/hermes-agent@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b`
was inspected from the registry using `docker buildx imagetools inspect --raw`:

- linux/amd64: `sha256:3140cc382d3f21a18019d28a1f89efb2af21ae9f86364895ccc019c7c7f8a446`
- linux/arm64: `sha256:1697e5df232a46c52650a612e9637bb3b0fffc70650af9c33eb593c45f70688f`

No `latest` upgrade was substituted. Provisioning checks Linux engine architecture
against registry index platforms and fails explicitly on an unsupported/missing
platform rather than silently emulating. This supports native Intel/AMD and Apple
Silicon Linux containers; it is not proof that every laptop OS was exercised.
Top-level unit/wizard tests are synthetic/mocked unless labeled otherwise.
An isolated Linux Docker test here **does not prove Windows/macOS laptop testing,
real provider/LinkedIn login or genuine research**. Those require your local pilot.
Inherited `TEST-RESULTS.md` is earlier-version evidence, not current v6 proof.
This source is not claimed deployed to a live VPS or published to Brain.

Official installation references:
- https://docs.docker.com/desktop/setup/install/windows-install/
- https://docs.docker.com/desktop/features/wsl/
- https://docs.docker.com/desktop/setup/install/mac-install/
- https://docs.docker.com/engine/install/
- https://hermes-agent.nousresearch.com/docs/user-guide/docker
