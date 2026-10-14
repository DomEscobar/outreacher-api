import base64
import json
from pathlib import Path
import socket
import ssl
import subprocess
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
mode=json.loads((ROOT/'.state/mode.json').read_text())
assert mode['mode']=='local' and mode['test'], 'Smoke test only runs on local synthetic test setup'
auth=json.loads((ROOT/'.state/test-auth.json').read_text())
context=ssl.create_default_context(cafile=str(ROOT/'.state/local-ca.crt'))  # Exact synthetic appliance CA; not public ACME.
origin=f"https://localhost:{mode['port']}"
def request(path, auth_value=None):
    headers={} if auth_value is None else {'Authorization':'Basic '+auth_value}
    try:
        with urllib.request.urlopen(urllib.request.Request(origin+path,headers=headers),context=context,timeout=15) as r:
            return r.status,r.read(),r.headers
    except urllib.error.HTTPError as e:
        return e.code,e.read(),e.headers
for path in ('/','/vnc.html','/websockify','/json/version','/api/v1/docs','/api/v1/openapi.json'):
    assert request(path)[0]==401, f'Unauthenticated route exposed: {path}'
assert request('/vnc.html',base64.b64encode(b'dom:incorrect').decode())[0]==401
credential=base64.b64encode((auth['username']+':'+auth['password']).encode()).decode()
assert request('/api/v1/openapi.json',base64.b64encode(b'dom:incorrect').decode())[0]==401
status,schema,_=request('/api/v1/openapi.json',credential)
assert status==200 and '/api/v1/discoveries' in json.loads(schema)['paths']
print('PASS: authenticated private API OpenAPI; missing/wrong credentials 401; no model job submitted')
status,body,headers=request('/vnc.html',credential)
assert status==200 and b'noVNC' in body
assert headers.get('Cache-Control')=='no-store'
assert request('/json/version',credential)[0]==404, 'CDP exposed via public proxy'
# A real authenticated websocket upgrade through Caddy to the VNC bridge.
with context.wrap_socket(socket.create_connection(('127.0.0.1',mode['port']),timeout=10),server_hostname='localhost') as sock:
    key=base64.b64encode(b'0123456789abcdef').decode()
    message=f'GET /websockify HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Basic {credential}\r\n\r\n'
    sock.sendall(message.encode())
    response=sock.recv(4096)
    assert b'101 Switching Protocols' in response,response.split(b'\r\n')[0]
print('PASS: HTTPS, missing/wrong password 401, authenticated noVNC, authenticated WebSocket, no exposed CDP')
# Actual container checks; no model invocation or user login needed.
script="""import json, urllib.request, pathlib, subprocess
assert pathlib.Path('/opt/data/skills/decision-maker-discovery/SKILL.md').exists()
assert pathlib.Path('/opt/data/skills/session-safety/SKILL.md').exists()
assert subprocess.run(['hermes','--help'],capture_output=True).returncode==0
with urllib.request.urlopen('http://127.0.0.1:9222/json/version') as response:
    version=json.load(response)
assert 'webSocketDebuggerUrl' in version
print('PASS: Hermes CLI, installed skill bundle, live Chromium CDP')
"""
subprocess.run(['docker','compose','exec','-T','session','python3','-c',script],cwd=ROOT,check=True)
# A 101 upgrade can succeed while VNC is hung. Complete negotiation and
# receive a real frame through the WebSocket bridge; report counts only.
subprocess.run(['docker','compose','exec','-T','session','python3','/opt/browser-box/rfb_probe.py','--websocket'],cwd=ROOT,check=True)
print('PASS: complete RFB negotiation and non-empty desktop frame over WebSocket')
# End-to-end TLS + Basic Auth + WebSocket + RFB, using only this test's
# generated credentials. The guard at the start refuses live/public configs.
edge_ids=subprocess.check_output(['docker','compose','ps','-q'],cwd=ROOT,text=True).split()
edge_containers=json.loads(subprocess.check_output(['docker','inspect',*edge_ids],text=True))
edge_names=[c['Config']['Labels'].get('com.docker.compose.service') for c in edge_containers
            if c['State']['Running'] and c['Config']['Labels'].get('com.docker.compose.service') in ('local','public')]
assert len(edge_names)==1, 'Synthetic TLS fixture must have exactly one active edge'
edge_script="""
import json,socket,ssl,sys,os
from websockets.sync.client import connect
from rfb_probe import Reader,handshake_and_frame
context=ssl._create_unverified_context() # Isolated localhost self-signed test CA.
raw=socket.create_connection((os.environ['DISCOVERY_TEST_EDGE'],443),timeout=5)
with connect('wss://localhost/websockify',sock=raw,ssl=context,proxy=None,
             origin='https://localhost',additional_headers={'Authorization':sys.stdin.read().strip()},
             subprotocols=['binary'],compression=None,max_size=32*1024*1024,open_timeout=30) as ws:
    print(json.dumps(handshake_and_frame(Reader(lambda:ws.recv(timeout=30)),ws.send)))
"""
subprocess.run(['docker','compose','exec','-T','-e','DISCOVERY_TEST_EDGE='+edge_names[0],'session','python3','-c',
                "import sys; sys.path.insert(0,'/opt/browser-box');\n"+edge_script],
               cwd=ROOT,input='Basic '+credential+'\n',text=True,check=True)
print('PASS: actual desktop frame through authenticated TLS Caddy WebSocket')
# Only the localhost TLS port is published; VNC/CDP/noVNC/Docker socket stay private.
ids=subprocess.check_output(['docker','compose','ps','-q'],cwd=ROOT,text=True).split()
containers=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True))
for item in containers:
    for port,bindings in (item['NetworkSettings'].get('Ports') or {}).items():
        if bindings:
            assert port=='443/tcp' and all(b['HostIp']=='127.0.0.1' for b in bindings)
    assert all(m['Destination']!='/var/run/docker.sock' for m in item.get('Mounts',[]))
print('PASS: only loopback HTTPS published; no Docker socket mounted')
