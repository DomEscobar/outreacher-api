import contextlib
import io
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import wizard

class WizardTests(unittest.TestCase):
    def test_plan_makes_no_changes(self):
        with patch.object(sys,'argv',['wizard.py','--plan']), patch.object(wizard,'call') as call, patch.object(wizard,'docker_ready') as docker, contextlib.redirect_stdout(io.StringIO()):
            wizard.main()
        call.assert_not_called(); docker.assert_not_called()

    def test_public_tls_uses_default_validation(self):
        error=urllib.error.HTTPError('https://browser.unit.invalid',401,'Unauthorized',None,None)
        with patch.object(wizard.urllib.request,'urlopen',side_effect=error) as request:
            url=wizard.verify_url({'mode':'public','host':'browser.unit.invalid','port':19443})
        self.assertEqual(url,'https://browser.unit.invalid/')
        self.assertIsNone(request.call_args.kwargs['context'])

    def test_unprotected_url_aborts(self):
        with patch.object(wizard.ssl,'create_default_context'), patch.object(wizard.urllib.request,'urlopen'):
            with self.assertRaises(SystemExit): wizard.verify_url({'mode':'local','host':'localhost','port':19443})

    def test_taken_port_aborts(self):
        with socket.socket() as sock:
            sock.bind(('0.0.0.0',0)); sock.listen()
            with self.assertRaises(SystemExit): wizard.check_port(sock.getsockname()[1])

    def test_existing_docker_is_not_replaced(self):
        with patch.object(wizard.shutil,'which',return_value='/usr/bin/docker'), patch.object(wizard,'call') as call:
            with self.assertRaises(SystemExit): wizard.install_docker()
        call.assert_not_called()

    def test_no_tty_rejects_secret_input(self):
        with patch.object(sys,'argv',['wizard.py']), patch.object(sys.stdin,'isatty',return_value=False), patch.object(wizard,'call') as call:
            with self.assertRaises(SystemExit): wizard.main()
        call.assert_not_called()

    def test_setup_resume_wires_tools_without_fake_model_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.state').mkdir()
            (root/'.state/mode.json').write_text('{"mode":"local","host":"localhost","port":19443,"test":false}')
            with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(wizard,'ROOT',root), patch.object(sys,'argv',['wizard.py']), patch.object(sys.stdin,'isatty',return_value=True), patch.object(sys.stdout,'isatty',return_value=True), patch.object(wizard,'docker_ready',return_value=True), patch.object(wizard.shutil,'disk_usage',return_value=type('Disk',(),{'free':20*1024**3})()), patch.object(wizard,'yes',side_effect=[True,False,False,False,False]), patch.object(wizard,'call') as call, patch.object(wizard,'verify_url',return_value='https://localhost:19443/'):
                wizard.main()
            commands=[c.args for c in call.call_args_list]
            self.assertTrue(any('up-local' in c for c in commands))
            self.assertFalse(any('setup' in c for c in commands))
            self.assertTrue(any('enable' in c and 'browser' in c and 'skills' in c for c in commands))
            self.assertIn('Modell-Login übersprungen',output.getvalue())

    def test_openrouter_key_goes_to_container_on_stdin_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.state').mkdir()
            (root/'.state/mode.json').write_text('{"mode":"local","host":"localhost","port":19443,"test":false}')
            secret='sk-or-synthetic-not-a-key'
            done=type('Done',(),{'returncode':0})()
            with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(wizard,'ROOT',root), patch.object(sys,'argv',['wizard.py']), patch.object(sys.stdin,'isatty',return_value=True), patch.object(sys.stdout,'isatty',return_value=True), patch.object(wizard,'docker_ready',return_value=True), patch.object(wizard.shutil,'disk_usage',return_value=type('Disk',(),{'free':20*1024**3})()), patch.object(wizard,'yes',side_effect=[True,True,False,True,False,False]), patch.object(wizard,'answer',side_effect=['1',wizard.DEFAULT_MODEL]), patch.object(wizard.getpass,'getpass',side_effect=[secret,'synthetic.treg.token']), patch.object(wizard.subprocess,'run',return_value=done) as run, patch.object(wizard,'call') as call, patch.object(wizard,'verify_url',return_value='https://localhost:19443/'):
                wizard.main()
            configured=[c for c in run.call_args_list if 'configure_provider.py' in ' '.join(c.args[0])]
            self.assertEqual([c.args[0][-1] if c.args[0][-1]=='treg' else c.args[0][-3:] for c in configured],
                             [['openrouter','--model',wizard.DEFAULT_MODEL],'treg'])
            self.assertEqual(configured[0].kwargs['input'],(secret+'\n').encode())
            everything=' '.join(str(c) for c in call.call_args_list)+' '.join(' '.join(c.args[0]) for c in run.call_args_list)+output.getvalue()
            self.assertNotIn(secret,everything)  # never in argv or printed output
            self.assertFalse(any('setup' in c.args for c in call.call_args_list))  # no interactive Hermes menu

if __name__=='__main__': unittest.main()
