"""Close Chromium through CDP on container shutdown so profile changes reach disk."""
import json
import os
import signal
import subprocess
import threading
import urllib.request
from websockets.sync.client import connect
from pathlib import Path
import fcntl

# Keep the browser consistent with the account's real locale/timezone. TZ is set per container
# (compose.yaml, BROWSER_TZ); the language must match the LinkedIn UI the account was logged in with.
BROWSER_LANG = os.environ.get('BROWSER_LANG', 'de-DE')
# Open LinkedIn only on the very first start of this profile. On every later start (including a
# crash loop) the browser opens about:blank, so restarts do not reload feed/messaging over and over.
# An explicit BROWSER_START_URL always wins (operator choice).
START_URL = os.environ.get('BROWSER_START_URL')
FIRST_RUN_MARKER = Path('/opt/data/.browser-start-done')
start_url = START_URL or ('about:blank' if FIRST_RUN_MARKER.exists() else 'https://www.linkedin.com/login')

# This appliance owns an isolated profile. Exclusive lease prevents concurrent
# runners; only stale Chromium singleton links are removed, never cookie data.
profile=Path('/opt/data/browser-profile')
profile.mkdir(parents=True,exist_ok=True)
lease=(profile/'.appliance-lock').open('a')
try: fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit('Browser profile already owned by another appliance runner.')
for name in ('SingletonLock','SingletonCookie','SingletonSocket'):
    path=profile/name
    if path.is_symlink(): path.unlink()

stop=threading.Event()
def requested(*args): stop.set()
signal.signal(signal.SIGTERM,requested)
signal.signal(signal.SIGINT,requested)

# Optional upstream HTTP(S)/SOCKS proxy, e.g. a dedicated static-residential endpoint. Chromium does
# not accept username:password in --proxy-server, so use a provider IP allowlist (whitelist this
# VPS) or a local forwarder that adds the credentials. Do not point this at a rotating pool while a
# LinkedIn session is active.
BROWSER_PROXY = os.environ.get('BROWSER_PROXY', '').strip()
args=[
    '/usr/bin/chromium','--no-sandbox','--disable-dev-shm-usage',
    '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9222',
    '--user-data-dir=/opt/data/browser-profile','--no-first-run',
    '--password-store=basic','--start-maximized',
    f'--lang={BROWSER_LANG}', f'--accept-lang={BROWSER_LANG[:2]}',
]
if BROWSER_PROXY:
    # Keep WebRTC from leaking the VPS address around the proxy.
    args += [f'--proxy-server={BROWSER_PROXY}', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']
args.append(start_url)
browser=subprocess.Popen(args)
FIRST_RUN_MARKER.touch(exist_ok=True)
while browser.poll() is None and not stop.wait(0.25): pass
if stop.is_set() and browser.poll() is None:
    try:
        with urllib.request.urlopen('http://127.0.0.1:9222/json/version',timeout=3) as response:
            endpoint=json.load(response)['webSocketDebuggerUrl']
        with connect(endpoint,open_timeout=3,close_timeout=1) as ws:
            ws.send(json.dumps({'id':1,'method':'Browser.close'}))
        browser.wait(timeout=12)
        print('Chromium closed cleanly; profile persisted.',flush=True)
    except Exception:
        print('Clean Chromium shutdown failed; terminating without persistence guarantee.',flush=True)
        browser.terminate()
        try: browser.wait(timeout=3)
        except subprocess.TimeoutExpired:
            browser.kill(); browser.wait()
raise SystemExit(browser.returncode if not stop.is_set() else 0)
