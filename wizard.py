#!/usr/bin/env python3
"""Guided host provisioning. Model and tool keys are read masked here and passed to the container on stdin only; this script never stores or prints them."""
import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import provision
from network_defaults import discover_host, valid_host, is_ip, port_available, suggest_port, url_for

ROOT = Path(__file__).resolve().parent
PIN = 'nousresearch/hermes-agent@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b'

def call(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)

def answer(prompt, default=''):
    value=input(f'{prompt}' + (f' [{default}]' if default else '') + ': ').strip()
    return value or default

def yes(prompt, default=False):
    return answer(prompt+' (j/n)', 'j' if default else 'n').lower() in ('j','ja','y','yes')

def step(number, title):
    print(f'\n[{number}/7] {title}',flush=True)

def docker_ready():
    if not shutil.which('docker'): return False
    try:
        call('docker','info', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        call('docker','compose','version', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError: return False

def install_docker():
    """Official apt repository; never replace an existing Docker installation."""
    if shutil.which('docker'):
        raise SystemExit('Docker vorhanden, aber Daemon/Compose/Zugriff fehlen. Repariere das bewusst; der Wizard ersetzt keine Installation.')
    if os.geteuid()!=0:
        raise SystemExit('Docker-Installation benötigt root. Auf einer freigegebenen VM: sudo bash setup.sh. Keine automatische Rechteerweiterung.')
    release={}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1); release[k]=v.strip('"')
    distro=release.get('ID','')
    codename=release.get('VERSION_CODENAME','')
    if distro not in ('debian','ubuntu') or not re.fullmatch('[a-z]+',codename):
        raise SystemExit('Automatische Installation nur für Debian/Ubuntu. Docker nach offizieller Doku installieren.')
    # Do not uninstall or migrate packages behind the user's back.
    for package in ('docker.io','docker-compose','podman-docker','containerd','runc'):
        found=subprocess.run(['dpkg-query','-W','-f=${Status}',package],capture_output=True,text=True)
        if found.returncode==0 and 'install ok installed' in found.stdout:
            raise SystemExit(f'Konfliktpaket {package} vorhanden. Keine automatische Entfernung/Migration.')
    call('apt-get','update')
    call('apt-get','install','-y','ca-certificates','curl')
    keys=Path('/etc/apt/keyrings'); keys.mkdir(mode=0o755,exist_ok=True)
    key=keys/'docker.asc'
    if key.exists() or Path('/etc/apt/sources.list.d/docker.sources').exists():
        raise SystemExit('Docker-APT-Konfiguration existiert bereits; keine automatische Überschreibung.')
    with urllib.request.urlopen(f'https://download.docker.com/linux/{distro}/gpg',timeout=30) as response:
        data=response.read()
    if b'BEGIN PGP PUBLIC KEY BLOCK' not in data: raise SystemExit('Ungültiger Docker-Repository-Schlüssel.')
    key.write_bytes(data); key.chmod(0o644)
    arch=subprocess.check_output(['dpkg','--print-architecture'],text=True).strip()
    if arch not in ('amd64','arm64'): raise SystemExit('Dieses Bundle unterstützt amd64/arm64.')
    repo=Path('/etc/apt/sources.list.d/docker.sources')
    repo.write_text(f'Types: deb\nURIs: https://download.docker.com/linux/{distro}\nSuites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key}\n')
    call('apt-get','update')
    call('apt-get','install','-y','docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin')
    if shutil.which('systemctl'): call('systemctl','enable','--now','docker')
    if not docker_ready(): raise SystemExit('Docker wurde installiert, ist aber nicht erreichbar; bitte Host prüfen.')

def check_port(port):
    if not port_available(port):
        raise SystemExit(f'Port {port} ist nicht lokal frei. Kein Dienst wird gestoppt.')

def select_free_port(prompt, default):
    while True:
        value=answer(prompt,str(default))
        try: port=int(value)
        except ValueError:
            print('Bitte eine Portnummer eingeben.'); continue
        if port!=443 and not 1024<=port<=65535:
            print('Nutze 443 oder einen Port zwischen 1024 und 65535.'); continue
        if port_available(port): return port
        default=suggest_port()
        print(f'Port {port} ist nicht lokal frei. Neuer Vorschlag: {default}. Kein Dienst wird gestoppt.')


def select_public_access():
    detected=discover_host()
    print('Erkannte VPS-Adresse: '+(detected['host'] or 'keine eindeutige öffentliche Adresse'))
    while True:
        host=answer('Öffentliche Domain oder IP (Enter übernimmt den Vorschlag)',detected['host'])
        if valid_host(host): break
        print('Bitte eine gültige öffentliche Domain oder IP ohne https://, Port oder Pfad eingeben.')
    if not is_ip(host):
        try: socket.getaddrinfo(host,None)
        except socket.gaierror: raise SystemExit('Domain nicht auflösbar; IP wählen oder DNS prüfen.')
    standard=not is_ip(host) and port_available(80) and port_available(443)
    suggested=443 if standard else suggest_port()
    if not standard: print('Standardports sind nicht frei oder IP-Zugang gewählt: separater Test-Port wird vorgeschlagen.')
    port=select_free_port('Öffentlicher HTTPS-Port (lokal bindbar; Firewall separat)',suggested)
    tls_mode='acme' if standard and port==443 else 'internal'
    if tls_mode=='internal':
        print('Eigener Port/IP: HTTPS mit eigener CA, daher Browser-Zertifikatswarnung. Kein automatisch öffentlich vertrauenswürdiges Zertifikat.')
        if not yes('Diesen Testzugang mit eigener CA verwenden?',False):
            raise SystemExit('Abgebrochen. Für vertrauenswürdiges TLS später vorhandenen Proxy, VPN oder DNS-Challenge einrichten.')
    if not yes(f'Zugang auf diesem Host über Port {port} öffentlich bereitstellen?',False): raise SystemExit('Abgebrochen.')
    return host,port,tls_mode


def verify_url(mode):
    url=url_for(mode)
    internal=mode['mode']=='local' or mode.get('tls_mode')=='internal'
    context=ssl.create_default_context(cafile=str(ROOT/'.state/local-ca.crt')) if internal else None
    if mode['mode']=='public' and internal:
        print('Prüfe nur Dienst/Passwortschutz. Das eigene CA-Zertifikat ist NICHT öffentlich vertrauenswürdig.')
    deadline=time.monotonic()+120
    while True:
        try:
            urllib.request.urlopen(url,timeout=8,context=context).close()
            raise SystemExit('FEHLER: URL antwortet ohne Passwort. Zugang nicht als sicher melden.')
        except urllib.error.HTTPError as error:
            if error.code==401: return url
            problem=f'HTTP {error.code}'
        except (urllib.error.URLError,TimeoutError,OSError) as error:
            problem=str(error)
        if time.monotonic()>=deadline:
            raise SystemExit(f'Container gestartet, aber HTTPS-Zugang noch NICHT verifiziert: {problem}. DNS/ACME/Firewall prüfen; später Wizard erneut starten.')
        time.sleep(3)

def inside(*args):
    call('docker','compose','exec','session',*args)

DEFAULT_MODEL='deepseek/deepseek-v4.1-flash'

def secret_inside(prompt,*args,attempts=3):
    """Masked prompt; the secret reaches the container on stdin only (never argv, env or output)."""
    for _ in range(attempts):
        secret=getpass.getpass(prompt).strip()
        if not secret: return False
        result=subprocess.run(['docker','compose','exec','-T','session','python3','/opt/browser-box/configure_provider.py',*args],
                              cwd=ROOT,input=(secret+'\n').encode())
        del secret
        if result.returncode==0: return True
        print('Nicht übernommen. Bitte erneut eingeben (leer = überspringen).')
    return False

def enforce_session_config():
    settings={
        'terminal.backend':'local', 'terminal.cwd':'/opt/data/workspace',
        'browser.cdp_url':'http://127.0.0.1:9222', 'browser.backend':'off',
        'browser.headed':'true', 'browser.restrict_evaluate':'true',
        'browser.allow_private_urls':'false', 'security.redact_secrets':'true',
        'approvals.mode':'manual',
    }
    for key,value in settings.items():
        call('docker','compose','exec','-T','session','hermes','config','set',key,value)
    call('docker','compose','exec','-T','session','hermes','tools','enable','browser','web','terminal','file','skills','--platform','cli')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local',action='store_true',help='Local PC only: localhost HTTPS, no public wizard or host dependency installation')
    parser.add_argument('--plan',action='store_true',help='Show steps; no changes or network requests')
    parser.add_argument('--self-test',action='store_true',help='Real isolated local Docker test; no user secrets or public deployment')
    args=parser.parse_args()
    if args.plan:
        if args.local:
            print('LOCAL PC: Python 3.10+, Bash, Linux Docker engine + Compose v2 required; no host installs, public address lookup or public ports. localhost HTTPS only; persistent unique project/image/volumes; own provider/browser login; local CA trust required.')
            return
        print('1 Docker/Host prüfen; optionale bestätigte Installation\n2 Lokal oder Domain wählen\n3 Passwort verdeckt setzen und Images pinnen\n4 Container, Browser und Skill-Bundle bauen/starten\n5 Modell: OpenRouter-Key verdeckt + Standardmodell (oder Hermes-Dialog)\n6 Tools/Shared-Browser prüfen, treg-Token für schnelle Lead-Suche\n7 Geschützte URL zeigen, manuell LinkedIn anmelden')
        return
    if args.self_test:
        if not docker_ready(): raise SystemExit('Self-test needs working Docker/Compose.')
        if (ROOT/'.env').exists(): raise SystemExit('Self-test needs a clean extracted bundle to avoid overwriting credentials.')
        call(sys.executable,'provision.py','configure','--test','--project-name',provision.default_project(),'--port',str(suggest_port(19444)))
        try:
            call(sys.executable,'provision.py','up-local')
            call(sys.executable,'provision.py','export-ca')
            enforce_session_config()
            call(sys.executable,'provision.py','test')
            # Exercise the custom-port profile only with test bind=127.0.0.1.
            rendered=json.loads(subprocess.check_output(['docker','compose','--profile','public','config','--format','json'],cwd=ROOT,text=True))
            assert all(p.get('host_ip')=='127.0.0.1' for p in rendered['services']['public']['ports']), 'Refusing externally bound test'
            call('docker','compose','--profile','local','stop','local')
            call('docker','compose','--profile','public','up','-d','--wait','--wait-timeout','120')
            call(sys.executable,'tests/smoke.py')
            print('PASS: custom HTTPS port/profile on loopback, Basic Auth and WebSocket; no host 80/443 takeover.')
        finally:
            call('docker','compose','--profile','public','stop')
            call(sys.executable,'provision.py','stop')
        print('Wizard engine self-test passed. No model login and no public deployment.')
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty(): raise SystemExit('Bitte interaktiv per SSH/Terminal ausführen: bash setup.sh. Keine Secrets über Pipes.')
    print('Hermes Browser Box — Wizard v6\nKeine Cloud-VM wird gekauft, keine bestehenden Credentials übernommen.')
    step(1,'Host und Docker')
    if platform.system() not in ('Linux','Darwin'): raise SystemExit('Use Linux, macOS Terminal, or Windows WSL2 Ubuntu (not PowerShell/Git Bash).')
    if not args.local and platform.system()!='Linux': raise SystemExit('On macOS use bash setup.sh --local.')
    if not docker_ready():
        if args.local: raise SystemExit('Local mode installs nothing. Install/start Docker Engine or Docker Desktop, enable Compose v2 and WSL integration if applicable; then retry.')
        if not yes('Docker/Compose fehlen oder sind nicht erreichbar. Docker auf diesem Host installieren?',False):
            raise SystemExit('Abgebrochen. Docker Engine + Compose installieren und erneut ausführen.')
        install_docker()
    free=shutil.disk_usage(ROOT).free//(1024**3)
    print(f'Docker bereit. Freier Platz: {free} GiB. Plane mindestens 10 GiB und 4 GiB RAM ein.')
    if free<10 and not yes('Wenig freier Speicher. Trotzdem fortfahren?',False): raise SystemExit('Abgebrochen.')
    mode_path=ROOT/'.state/mode.json'
    configured=mode_path.exists()
    step(2,'Zugangsart')
    if configured:
        mode=json.loads(mode_path.read_text())
        if args.local and mode.get('mode')!='local': raise SystemExit('--local refuses existing public configuration. Use a fresh source directory; nothing changed.')
        if mode.get('test'): raise SystemExit('Vorhandene Testkonfiguration: normales Setup in frisch entpacktem Bundle durchführen.')
        if not yes(f"Vorhandene Konfiguration ({mode['mode']}, {mode['host']}) weiterverwenden?",True):
            raise SystemExit('Keine Überschreibung. Neues Verzeichnis/Instanz verwenden oder gezielt provision.py configure --reconfigure.')
    else:
        choice='1' if args.local else answer('1 = lokal/SSH-Tunnel, 2 = öffentliche HTTPS-Domain','1')
        if choice not in ('1','2'): raise SystemExit('Bitte 1 oder 2 wählen.')
        if choice=='2':
            domain,port,tls_mode=select_public_access()
        else:
            domain=None; tls_mode='internal'
            port=select_free_port('Lokaler HTTPS-Port',suggest_port(19443))
        project=answer('Isolierter Docker-Instanzname',provision.default_project())
        existing_project=subprocess.check_output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project],text=True).strip()
        if existing_project:
            raise SystemExit('Dieser Docker-Instanzname existiert bereits. Keine fremden Container/Volumes übernehmen; anderen Namen wählen.')
        step(3,'Passwort und Images')
        cmd=[sys.executable,'provision.py','configure','--project-name',project,'--port',str(port),'--hermes-image',PIN]
        if domain: cmd+=['--host',domain,'--tls-mode',tls_mode]
        call(*cmd)
        mode=json.loads(mode_path.read_text())
    if configured: step(3,'Zugang bleibt unverändert; keine Passwortausgabe')
    step(4,'Hermes, Browser, Tools und Skills starten')
    cmd=[sys.executable,'provision.py','up-public','--confirm-public'] if mode['mode']=='public' else [sys.executable,'provision.py','up-local']
    call(*cmd)
    if mode.get('tls_mode')=='internal' or mode['mode']=='local':
        call(sys.executable,'provision.py','export-ca')
    url=verify_url(mode)
    step(5,'Modell-Credentials — getrennt vom Browserpasswort')
    print('Empfohlen: OpenRouter (ein Key für viele Modelle, Key unter https://openrouter.ai/keys). Anbieter können kostenpflichtig sein.')
    provider_configured=False
    if yes('Jetzt Modellanbieter und Login einrichten?',True):
        login_flow=answer('1 = OpenRouter-Key + Standardmodell (empfohlen), 2 = anderer Anbieter im Hermes-Dialog, 3 = Nous-Portal-Login','1')
        if login_flow=='1':
            model=answer('Standardmodell (OpenRouter-ID, Enter = Vorschlag)',DEFAULT_MODEL)
            provider_configured=secret_inside('OpenRouter-Key (sk-or-…, verdeckte Eingabe): ','openrouter','--model',model)
        elif login_flow=='2': inside('hermes','setup','model'); provider_configured=True
        elif login_flow=='3': inside('hermes','setup','--portal'); provider_configured=True
        else: raise SystemExit('Bitte 1, 2 oder 3 wählen; keine Login-Konfiguration geändert.')
    if not provider_configured:
        print('Modell-Login übersprungen: Browser funktioniert, Hermes kann noch keine Modellanfragen ausführen.')
    step(6,'Toolauswahl und gemeinsam genutzten Browser prüfen')
    print('Browser/CDP ist lokal vorbereitet. Websuche kann eigene Credentials oder einen Tool-Gateway-Zugang benötigen; keine bezahlten Konten werden automatisch angelegt.')
    if yes('Optionale Tool-Anbieter im offiziellen Hermes-Tool-Wizard konfigurieren?',False):
        inside('hermes','setup','tools')
    print('Schnelle Lead-Suche (API source="search", ohne Browser-Agent) braucht einen treg.to-Token (https://treg.to) und den OpenRouter-Key aus Schritt 5.')
    if yes('treg.to-Token für die schnelle Lead-Suche jetzt hinterlegen?',True):
        if not secret_inside('treg-Token (verdeckte Eingabe): ','treg'):
            print('treg-Token übersprungen: source="search" meldet search_not_configured, bis er hinterlegt ist.')
    enforce_session_config()
    inside('hermes','tools','--summary')
    call('docker','compose','exec','-T','session','python3','/opt/browser-box/health.py')
    if provider_configured and yes('Optional einen kostenpflichtigen Modell-Smoke-Test starten?',False):
        inside('hermes','chat','--toolsets','skills','-q','Antworte ausschließlich mit OK. Keine Tools aufrufen.')
    step(7,'Fertig: manueller Browser-Login')
    print(f'URL: {url}\nWeb-Benutzer: dom\nPasswort: dein gewähltes Passwort (wird nicht ausgegeben).')
    if mode.get('tls_mode')=='internal' and mode['mode']=='public':
        print('Browser-Zertifikatswarnung: eigene CA. Vor sensiblen Logins Zertifikat/CA bewusst prüfen; kein automatisch vertrauenswürdiges HTTPS.')
    if mode['mode']=='public': print(f"Der Host-Port {mode['port']} muss ggf. in Anbieter-Firewall/Sicherheitsgruppe erlaubt werden. Keine Firewall wurde verändert.")
    if mode['mode']=='local' and args.local:
        print('Open the localhost URL on this PC. Local CA: verify/trust .state/local-ca.crt; see LOCAL-PC.md. No firewall/public ports required.')
    elif mode['mode']=='local':
        print(f"Nur auf dem Host erreichbar. Von deinem Rechner: ssh -L {mode['port']}:127.0.0.1:{mode['port']} USER@SERVER\nLokales TLS nutzt eine eigene CA; nicht als öffentlich verifiziertes Zertifikat behandeln.")
    print('Im Remote-Browser selbst bei LinkedIn einloggen; Hermes während Passwort/2FA pausieren. Kein automatischer Versand/Scraping.')
    print('Hermes starten: docker compose exec session /opt/browser-box/launch-hermes.sh\nStoppen: python3 provision.py stop\nDieser Container ist nicht automatisch mit dem bestehenden Telegram-Bot verbunden.')
    if yes('Hermes jetzt interaktiv starten?',False): inside('/opt/browser-box/launch-hermes.sh')

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('\nAbgebrochen. Vorhandene Container/Volumes werden nicht gelöscht.'); sys.exit(130)
    except subprocess.CalledProcessError as error:
        print(f'Schritt fehlgeschlagen (Exit {error.returncode}). Kein Erfolg behauptet. Erneut starten oder Docker-Status prüfen.',file=sys.stderr)
        sys.exit(error.returncode or 1)
