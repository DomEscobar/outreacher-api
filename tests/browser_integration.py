"""Actual Hermes tool integration against a synthetic local page, never LinkedIn credentials."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tools.browser_tool import browser_navigate, browser_snapshot

phase = sys.argv[1]
from pathlib import Path
import uuid
marker_path=Path('/opt/data/workspace/synthetic-cookie-marker.txt')
if phase=='seed': marker_path.write_text(uuid.uuid4().hex)
marker=marker_path.read_text()
class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        retained = ('browser_box_test='+marker) in self.headers.get('Cookie','')
        self.send_response(200)
        self.send_header('Content-Type','text/html')
        if phase == 'seed':
            self.send_header('Set-Cookie',f'browser_box_test={marker}; Max-Age=3600; SameSite=Strict; Path=/')
        self.end_headers()
        text = 'Synthetic session retained' if retained else 'Synthetic session initialized'
        self.wfile.write(f'<html><head><title>Browser Box Integration</title></head><body><h1>{text}</h1><button>Review only</button></body></html>'.encode())
    def log_message(self,*args): pass
server = ThreadingHTTPServer(('127.0.0.1',9000),Fixture)
threading.Thread(target=server.serve_forever,daemon=True).start()
nav = json.loads(browser_navigate('http://127.0.0.1:9000',task_id='browser-box-test'))
assert nav.get('success'), nav
assert nav.get('title') == 'Browser Box Integration', nav
snapshot=json.loads(browser_snapshot(full=True,task_id='browser-box-test'))
assert snapshot.get('success'),snapshot
text=json.dumps(snapshot)
assert 'Review only' in text,text
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:9222/json/list') as response:
    targets=json.load(response)
assert any(t.get('url','').startswith('http://127.0.0.1:9000') for t in targets), 'Hermes used a different browser'
if phase=='verify': assert 'Synthetic session retained' in text,text
print('PASS: actual Hermes navigate/snapshot attached to shared Chromium; phase='+phase)
server.shutdown()
