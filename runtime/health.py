import socket
import urllib.request
from rfb_probe import Reader,handshake_and_frame
for url in ('http://127.0.0.1:6080/vnc.html','http://127.0.0.1:9222/json/version','http://127.0.0.1:8081/api/v1/openapi.json'):
    with urllib.request.urlopen(url,timeout=3) as response:
        assert response.status==200
# Complete a shared, None-security RFB handshake. Never count pre-auth
# disconnects as a healthy desktop; never request or log pixels in healthchecks.
with socket.create_connection(('127.0.0.1',5900),timeout=3) as vnc:
    vnc.settimeout(3)
    handshake_and_frame(Reader(lambda:vnc.recv(65536)),vnc.sendall,want_frame=False)
