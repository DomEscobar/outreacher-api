"""Close Chromium through CDP on container shutdown so profile changes reach disk."""
import json
import signal
import subprocess
import threading
import urllib.request
from websockets.sync.client import connect
from pathlib import Path
import fcntl

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

browser=subprocess.Popen([
    '/usr/bin/chromium','--no-sandbox','--disable-dev-shm-usage',
    '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9222',
    '--user-data-dir=/opt/data/browser-profile','--no-first-run',
    '--password-store=basic','--start-maximized','https://www.linkedin.com/login',
])
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
