"""LinkedIn connection-request sender — the only module in this box that clicks in the browser.

It opens the target profile in a BACKGROUND tab of the shared, already logged-in Chromium via CDP,
clicks "Vernetzen"/"Connect", optionally adds the connection note from the outreach record and clicks
"Send". One call sends at most one request. It never solves a challenge: any wall, rate limit or
CAPTCHA is reported as `blocked` and the service pauses all automated browser work.

The same read-only classification helpers are used by outreach.py for the acceptance check.
"""
from contextlib import contextmanager
import json
import os
import random
import re
import time
from urllib.parse import urlsplit


def _dwell(value):
    """Parse "3-9" into (low, high) seconds; unusable values fall back to 3-9 s."""
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*', value or '')
    if not match: return (3.0, 9.0)
    low, high = float(match.group(1)), float(match.group(2))
    return (low, high) if 0 <= low <= high else (3.0, 9.0)


# Dwell on the profile (reading) before clicking Connect: never an instant load-to-click jump.
DWELL = _dwell(os.environ.get('OUTREACH_CONNECT_DWELL', '3-9'))

LOGIN_PATHS = ('/login', '/authwall', '/checkpoint', '/uas/', '/mcheck')
# Hard block signals: URLs and visible text LinkedIn uses for challenges, limits and restrictions.
BLOCK_PATHS = ('/checkpoint/challenge', '/checkpoint/lg', '/restricted', '/rate-limit', '/mcheck')
BLOCK_TEXT = ('unusual activity', 'unusual traffic', 'unusual login', 'rate limit', 'too many requests',
              'verify you are human', 'security verification', 'account restricted', 'temporarily restricted',
              'ungewöhnliche aktivität', 'zu viele anfragen', 'sicherheitsüberprüfung', 'konto eingeschränkt',
              'bestätigen sie, dass sie ein mensch sind')
# An exhausted invitation allowance needs a much longer stop than a transient challenge.
LIMIT_TEXT = ('invitation limit', 'einladungslimit', 'wöchentliches limit', 'wochenlimit',
              'limit für einladungen', 'einladungen limit', 'weekly invitation limit',
              'you have reached the weekly invitation limit', 'limit erreicht')

CONNECT_LABELS = ['vernetzen', 'connect', 'einladung senden', 'invite', 'invite to connect']
NOTE_LABELS = ['notiz hinzufügen', 'eine nachricht hinzufügen', 'add a note', 'add note']
SEND_LABELS = ['senden', 'send', 'einladung senden', 'send without a note', 'ohne notiz senden',
               'send invitation', 'ohne notiz']


def login_wall(url):
    return any(p in urlsplit(url or '').path for p in LOGIN_PATHS)


def blocked_page(url, text=''):
    """A challenge, rate-limit or restricted page instead of normal content."""
    path = urlsplit(url or '').path
    return any(p in path for p in BLOCK_PATHS) or any(marker in (text or '').casefold() for marker in BLOCK_TEXT)


def limited_page(text=''):
    """The invitation allowance is used up (weekly/monthly limit): stop for days, not hours."""
    return any(marker in (text or '').casefold() for marker in LIMIT_TEXT)


def classify_connection(text, url):
    """Map the visible profile top card (DE/EN UI) to a connection state. Order matters."""
    if login_wall(url): return 'login_required'
    t = ' '.join((text or '').split())[:4000].casefold()
    if blocked_page(url, t): return 'login_required'
    if re.search(r'(?<!\w)(ausstehend|pending)(?!\w)', t): return 'pending'
    if re.search(r'·\s*(1st|1\.)(?!\w)|1st degree connection|kontakt 1\. grades', t): return 'connected'
    if re.search(r'·\s*(2nd|3rd|3rd\+|2\.|3\.)|(?<!\w)(vernetzen|connect)(?!\w)', t): return 'not_connected'
    return 'unknown'


class BrowserBlocked(Exception):
    """The page was a challenge/rate-limit page, not content."""


def _array(values):
    return '[' + ','.join(json.dumps(v) for v in values) + ']'


BLOCK_JS = ('const blockedText = ' + _array(BLOCK_TEXT) + ';'
            'const pageText = (((document.body || {}).innerText) || "").toLowerCase();'
            'const blocked = blockedText.some((x) => pageText.includes(x));')

READ_EXPRESSION = ('(() => {' + BLOCK_JS + ' return {href: location.href, blocked, text: '
                   '((document.querySelector("main section") || document.body || {}).innerText || "").slice(0, 4000)}; })()')


def action_expression(note):
    """A single async in-page script: click Connect, optionally add the note, click Send, report."""
    return ('(async () => {' + BLOCK_JS +
            ' const sleep = (ms) => new Promise((r) => setTimeout(r, ms));'
            ' const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();'
            ' const labels = (el) => [el.innerText, el.getAttribute("aria-label"), el.getAttribute("title")]'
            '   .map(norm).filter(Boolean);'
            ' const find = (scope, wanted) => [...scope.querySelectorAll("button, a[role=\\"button\\"], a, [role=\\"button\\"]")]'
            '   .find((el) => !el.disabled && labels(el).some((l) => wanted.includes(l)));'
            ' const main = document.querySelector("main") || document.body;'
            ' const scope = document.querySelector(".pvs-profile-actions") || document.querySelector("main section") || main;'
            ' let button = find(scope, ' + _array(CONNECT_LABELS) + ') || find(main, ' + _array(CONNECT_LABELS) + ');'
            ' if (!button) return {sent: false, reason: "no_button", href: location.href,'
            '   text: ((main || {}).innerText || "").slice(0, 4000)};'
            ' button.click();'
            ' let dialog = null;'
            ' for (let i = 0; i < 14; i++) { await sleep(500);'
            '   const now = (((document.body || {}).innerText) || "").toLowerCase();'
            '   if (blockedText.some((x) => now.includes(x))) return {sent: false, reason: "blocked",'
            '     href: location.href, text: now.slice(0, 4000)};'
            '   dialog = document.querySelector(".artdeco-modal, [role=\\"dialog\\"]"); if (dialog) break; }'
            ' const box = dialog || main;'
            f' const note = {json.dumps(note or "")};'
            ' if (note) { const add = find(box, ' + _array(NOTE_LABELS) + '); if (add) { add.click(); await sleep(800);'
            '   const field = box.querySelector("textarea") || document.querySelector(".artdeco-modal textarea, [role=\\"dialog\\"] textarea");'
            '   if (field) { const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;'
            '     setter.call(field, note); field.dispatchEvent(new Event("input", {bubbles: true}));'
            '     field.dispatchEvent(new Event("change", {bubbles: true})); await sleep(500); } } }'
            ' let sent = false;'
            ' for (let i = 0; i < 12; i++) { const b = find(box, ' + _array(SEND_LABELS) + ') || find(document, ' + _array(SEND_LABELS) + ');'
            '   if (b) { b.click(); sent = true; break; } await sleep(500); }'
            ' await sleep(1800);'
            ' return {sent, reason: sent ? "sent" : "no_send", href: location.href,'
            '   text: (((document.body || {}).innerText) || "").slice(0, 4000)};'
            '})()')


class LinkedInConnector:
    """Send one connection request (with an optional note) through the shared browser profile."""
    def __init__(self, cdp='http://127.0.0.1:9222', timeout=45):
        self.cdp, self.timeout = cdp, timeout

    def send_request(self, url, note=None):
        """Return {'state': 'sent'|'pending'|'connected'|'blocked'|'no_button'|'no_send'|'unknown'}."""
        with self._target() as (send, session):
            send('Page.navigate', {'url': url}, session)
            value = self._read(send, session, url)
            text = value.get('text') or ''
            if limited_page(text): return {'state': 'limit'}
            state = classify_connection(text, value.get('href') or url)
            if value.get('blocked') or blocked_page(value.get('href') or url, text):
                return {'state': 'blocked'}
            if state == 'login_required': return {'state': 'blocked'}
            if state in ('connected', 'pending'):
                return {'state': state}
            if state != 'not_connected':
                return {'state': 'unknown'}
            # Read the profile for a few seconds before acting: no instant page-load-to-click jump.
            time.sleep(random.uniform(*DWELL))
            result = send('Runtime.evaluate', {'returnByValue': True, 'awaitPromise': True,
                                               'expression': action_expression(note)}, session)
            out = result.get('result', {}).get('value') or {}
            reason = out.get('reason')
            if reason == 'blocked':
                return {'state': 'blocked'}
            text, href = out.get('text') or '', out.get('href') or url
            if limited_page(text): return {'state': 'limit'}
            if blocked_page(href, text): return {'state': 'blocked'}
            observed = classify_connection(text, href)
            if observed == 'login_required': return {'state': 'blocked'}
            if observed == 'connected': return {'state': 'connected'}
            if observed == 'pending': return {'state': 'sent'}
            if out.get('sent'): return {'state': 'sent_unconfirmed'}
            return {'state': reason or 'unknown'}

    def _read(self, send, session, url):
        deadline, value = time.monotonic() + self.timeout, {}
        while time.monotonic() < deadline:
            time.sleep(1.2)
            try:
                value = send('Runtime.evaluate', {'returnByValue': True, 'expression': READ_EXPRESSION},
                             session).get('result', {}).get('value') or {}
            except RuntimeError:
                value = {}  # execution context was replaced during navigation
                continue
            text = value.get('text') or ''
            if value.get('blocked') or (len(text) > 80 and classify_connection(text, value.get('href') or url) != 'unknown'):
                break
        return value

    @contextmanager
    def _target(self):
        import urllib.request
        from websockets.sync.client import connect
        version = json.load(urllib.request.urlopen(self.cdp + '/json/version', timeout=5))
        ids = iter(range(1, 10_000))
        with connect(version['webSocketDebuggerUrl'], max_size=8_000_000, open_timeout=10) as ws:
            def send(method, params=None, session=None):
                ident = next(ids)
                message = {'id': ident, 'method': method, 'params': params or {}}
                if session: message['sessionId'] = session
                ws.send(json.dumps(message))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    reply = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                    if reply.get('id') == ident:
                        if 'error' in reply: raise RuntimeError('cdp_error')
                        return reply.get('result', {})
                raise TimeoutError('cdp_timeout')
            target = send('Target.createTarget', {'url': 'about:blank', 'background': True})['targetId']
            try:
                session = send('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
                yield send, session
            finally:
                try: send('Target.closeTarget', {'targetId': target})
                except Exception: pass
