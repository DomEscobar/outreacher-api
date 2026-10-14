"""Synthetic/mocked local provisioning tests; no model, live browser or Docker writes."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import provision
import wizard
spec=importlib.util.spec_from_file_location('local_api',ROOT/'local-api.py')
api=importlib.util.module_from_spec(spec); spec.loader.exec_module(api)

class TtyCapture(io.StringIO):
    def isatty(self): return True

class LocalTests(unittest.TestCase):
    def args(self,**changes):
        values=dict(domain=None,port=19443,tls_mode='auto',test=False,reconfigure=False,project_name=None,hermes_image=wizard.PIN)
        values.update(changes); return argparse.Namespace(**values)

    def test_unique_generated_names(self):
        names=[provision.default_project() for _ in range(50)]
        self.assertEqual(len(set(names)),50)
        self.assertTrue(all(re.fullmatch(r'hermes-browser-v6-[a-f0-9]{12}',n) for n in names))

    def test_local_configuration_never_acme(self):
        mode=provision.configuration(self.args(tls_mode='acme'))
        self.assertEqual((mode['host'],mode['mode'],mode['tls_mode']),('localhost','local','internal'))

    def test_plan_local_no_reads_changes_network(self):
        with patch.object(sys,'argv',['wizard.py','--local','--plan']), patch.object(wizard,'call') as call, patch.object(wizard,'docker_ready') as docker, patch.object(wizard,'discover_host') as discover, contextlib.redirect_stdout(TtyCapture()) as output:
            wizard.main()
        call.assert_not_called(); docker.assert_not_called(); discover.assert_not_called()
        self.assertIn('localhost HTTPS only',output.getvalue())
        self.assertNotIn('Domain wählen',output.getvalue())

    def test_local_missing_docker_installs_nothing(self):
        with patch.object(sys,'argv',['wizard.py','--local']), patch.object(sys.stdin,'isatty',return_value=True), patch.object(sys.stdout,'isatty',return_value=True), patch.object(wizard,'docker_ready',return_value=False), patch.object(wizard,'install_docker') as install, patch.object(wizard,'yes') as yes, contextlib.redirect_stdout(TtyCapture()):
            with self.assertRaisesRegex(SystemExit,'Local mode installs nothing'): wizard.main()
        install.assert_not_called(); yes.assert_not_called()

    def test_local_existing_public_config_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'.state').mkdir()
            (root/'.state/mode.json').write_text(json.dumps(dict(mode='public',host='unit.invalid',test=False)))
            with patch.object(wizard,'ROOT',root), patch.object(sys,'argv',['wizard.py','--local']), patch.object(sys.stdin,'isatty',return_value=True), patch.object(sys.stdout,'isatty',return_value=True), patch.object(wizard,'docker_ready',return_value=True), patch.object(wizard.shutil,'disk_usage',return_value=type('Disk',(),{'free':20*1024**3})()), patch.object(wizard,'call') as call, contextlib.redirect_stdout(TtyCapture()):
                with self.assertRaisesRegex(SystemExit,'refuses existing public'): wizard.main()
            call.assert_not_called()

    def test_local_fresh_wizard_on_macos_no_public_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); seen=[]; commands=[]
            def answer(prompt,default=''):
                seen.append(prompt); return default
            def call(*args,**kwargs):
                commands.append(args)
                if 'configure' in args:
                    (root/'.state').mkdir()
                    (root/'.state/mode.json').write_text(json.dumps(dict(mode='local',host='localhost',port=19443,tls_mode='internal',test=False)))
            with patch.object(wizard,'ROOT',root), patch.object(sys,'argv',['wizard.py','--local']), patch.object(sys.stdin,'isatty',return_value=True), patch.object(sys.stdout,'isatty',return_value=True), patch.object(wizard.platform,'system',return_value='Darwin'), patch.object(wizard,'docker_ready',return_value=True), patch.object(wizard.shutil,'disk_usage',return_value=type('Disk',(),{'free':20*1024**3})()), patch.object(wizard,'discover_host') as discover, patch.object(wizard,'select_public_access') as public, patch.object(wizard,'answer',side_effect=answer), patch.object(wizard,'yes',return_value=False), patch.object(wizard,'port_available',return_value=True), patch.object(wizard,'suggest_port',return_value=19443), patch.object(wizard.subprocess,'check_output',return_value=''), patch.object(wizard,'call',side_effect=call), patch.object(wizard,'verify_url',return_value='https://localhost:19443/'), contextlib.redirect_stdout(TtyCapture()):
                wizard.main()
            discover.assert_not_called(); public.assert_not_called()
            self.assertFalse(any('Domain' in s for s in seen))
            self.assertTrue(any('up-local' in c for c in commands))
            self.assertTrue(any('export-ca' in c for c in commands))
            self.assertFalse(any('up-public' in c or '--host' in c for c in commands))

    def test_arch_matrix_accepts_both_native_platforms(self):
        manifest=json.dumps({'manifests':[{'platform':{'os':'linux','architecture':arch}} for arch in ('amd64','arm64')]})
        for arch in ('x86_64','aarch64'):
            with patch.object(provision.subprocess,'check_output',side_effect=[json.dumps({'OSType':'linux','Architecture':arch}),manifest]), contextlib.redirect_stdout(TtyCapture()):
                provision.check_architecture(wizard.PIN)

    def test_arch_mismatch_explicit(self):
        with patch.object(provision.subprocess,'check_output',side_effect=[json.dumps({'OSType':'linux','Architecture':'arm64'}),json.dumps({'manifests':[{'platform':{'os':'linux','architecture':'amd64'}}]})]):
            with self.assertRaisesRegex(SystemExit,'architecture mismatch'): provision.check_architecture(wizard.PIN)

    def test_windows_container_engine_refused(self):
        with patch.object(provision.subprocess,'check_output',return_value=json.dumps({'OSType':'windows','Architecture':'amd64'})):
            with self.assertRaisesRegex(SystemExit,'Linux'): provision.check_architecture(wizard.PIN)

    def test_local_configure_stores_loopback_isolation_and_no_password(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            def check(cmd,**kwargs):
                return '$2b$synthetic-only-hash\n' if 'hash-password' in cmd else ''
            with patch.object(sys.stdin,'isatty',return_value=True), patch.object(provision,'ROOT',root), patch.object(provision,'STATE',root/'.state'), patch.object(provision,'check_architecture'), patch.object(provision,'port_available',return_value=True), patch.object(provision.subprocess,'check_output',side_effect=check), patch.object(provision,'pinned',side_effect=lambda image:image), patch.object(provision.getpass,'getpass',return_value='synthetic-password-not-real'), contextlib.redirect_stdout(TtyCapture()):
                provision.configure(self.args())
            env=(root/'.env').read_text()
            self.assertIn('PUBLIC_BIND_PREFIX=127.0.0.1:',env)
            self.assertIn('COMPOSE_PROJECT_NAME=hermes-browser-v6-',env)
            self.assertNotIn('synthetic-password',env)
            self.assertFalse((root/'.state/test-auth.json').exists())
            self.assertEqual((root/'.env').stat().st_mode & 0o777,0o600)

    def test_configure_no_tty_refuses_unmasked_fallback(self):
        with patch.object(sys.stdin,'isatty',return_value=False), patch.object(provision.getpass,'getpass') as password:
            with self.assertRaisesRegex(SystemExit,'masked password'): provision.configure(self.args())
        password.assert_not_called()

    def test_orphan_volume_not_adopted(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sys.stdin,'isatty',return_value=True), patch.object(provision,'ROOT',Path(temp)), patch.object(provision,'STATE',Path(temp)/'.state'), patch.object(provision.subprocess,'check_output',side_effect=['','synthetic-orphan-volume']), patch.object(provision,'check_architecture') as arch:
                with self.assertRaisesRegex(SystemExit,'Nothing adopted'): provision.configure(self.args(project_name='unit-fixture'))
            arch.assert_not_called()

    def test_digest_pin_keeps_index_not_first_inspected_alias(self):
        with patch.object(provision,'run'), patch.object(provision.subprocess,'check_output',return_value='["synthetic-other-alias@sha256:0000"]'):
            self.assertEqual(provision.pinned(wizard.PIN),wizard.PIN)

    def test_tls_readiness_uses_scoped_ca(self):
        import urllib.error
        error=urllib.error.HTTPError('https://localhost:19443/',401,'Unauthorized',None,None)
        with patch.object(wizard.ssl,'create_default_context') as context, patch.object(wizard.urllib.request,'urlopen',side_effect=error):
            wizard.verify_url(dict(mode='local',host='localhost',port=19443,tls_mode='internal'))
        context.assert_called_once_with(cafile=str(wizard.ROOT/'.state/local-ca.crt'))

    def test_api_nonlocal_and_secret_urls_refused(self):
        for base in ('http://localhost:19443','https://unit.invalid','https://dom:secret@localhost:19443','https://localhost/api','https://localhost/?x=1'):
            with self.assertRaises(ValueError): api.request_once(base,None,'synthetic-password')

    def test_api_exactly_one_get_or_post_scoped_tls(self):
        for payload,job,method,path in [(None,None,'GET','openapi.json'),({'limit':3},None,'POST','discoveries'),(None,'synthetic-job','GET','discoveries/synthetic-job')]:
            opener=MagicMock(); opener.open.return_value.__enter__.return_value.read.return_value=b'{"synthetic_fixture":true}'
            with patch.object(api.ssl,'create_default_context') as context, patch.object(api.urllib.request,'build_opener',return_value=opener):
                result=api.request_once('https://localhost:19443/',Path('synthetic-ca.crt'),'synthetic-password',payload,job)
            self.assertTrue(result['synthetic_fixture'])
            context.assert_called_once_with(cafile='synthetic-ca.crt')
            opener.open.assert_called_once()
            request=opener.open.call_args.args[0]
            self.assertEqual(request.get_method(),method)
            self.assertEqual(request.full_url,'https://localhost:19443/api/v1/'+path)
            self.assertNotIn('synthetic-password',request.full_url)

    def test_api_real_loopback_tls_transport_synthetic_only(self):
        import base64
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import shutil
        import ssl
        import subprocess
        import threading
        if not shutil.which('openssl'): self.skipTest('openssl unavailable for synthetic TLS certificate')
        observed=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.command,self.path))
                expected='Basic '+base64.b64encode(b'dom:synthetic-password').decode()
                if self.headers.get('Authorization')!=expected:
                    self.send_response(401); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(b'{"synthetic_fixture":true}')
            def log_message(self,*args): pass
        with tempfile.TemporaryDirectory() as temp:
            cert=Path(temp)/'cert.pem'; key=Path(temp)/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            server=HTTPServer(('127.0.0.1',0),Handler)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key)
            server.socket=context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            try:
                result=api.request_once(f'https://localhost:{server.server_port}',cert,'synthetic-password',job='synthetic-job')
                self.assertEqual(result,{'synthetic_fixture':True})
                self.assertEqual(observed,[('GET','/api/v1/discoveries/synthetic-job')])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_api_redirects_not_followed(self):
        self.assertIsNone(api.NoRedirect().redirect_request(None,None,302,'',{},'https://unit.invalid'))

    def test_api_no_tty_rejects_password_input(self):
        with patch.object(sys,'argv',['local-api.py']), patch.object(sys.stdin,'isatty',return_value=False), patch.object(api.getpass,'getpass') as secret, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): api.main()
        secret.assert_not_called()

if __name__=='__main__': unittest.main()
