"""configure_provider.py (wizard helper): EXPLICIT SYNTHETIC keys only, network and Hermes CLI mocked."""
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('configure_provider', Path(__file__).resolve().parents[2] / 'configure_provider.py')
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class ConfigureProviderTests(unittest.TestCase):
    def run_cli(self, argv, secret, valid=True, model=True):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HERMES_HOME': home}), \
             patch('sys.argv', ['configure_provider.py', *argv]), patch('sys.stdin', io.StringIO(secret + '\n')), \
             patch.object(cp, 'check_openrouter', return_value=valid), patch.object(cp, 'model_exists', return_value=model), \
             patch.object(cp, 'hermes_config') as config, patch('sys.stdout', io.StringIO()) as out, patch('sys.stderr', io.StringIO()) as err:
            code = cp.main()
            env = Path(home, '.env').read_text() if Path(home, '.env').exists() else ''
        return code, env, [c.args for c in config.call_args_list], out.getvalue() + err.getvalue()

    def test_openrouter_key_and_default_model(self):
        code, env, config, text = self.run_cli(['openrouter', '--model', 'synthetic/model'], 'sk-or-synthetic')
        self.assertEqual(code, 0)
        self.assertIn('OPENROUTER_API_KEY=sk-or-synthetic', env)
        self.assertIn(('model.provider', 'openrouter'), config)
        self.assertIn(('model.default', 'synthetic/model'), config)
        self.assertNotIn('sk-or-synthetic', text)  # key never printed

    def test_rejected_inputs_store_nothing(self):
        for argv, secret, valid, model in ((['openrouter'], 'not-a-key', True, True),
                                           (['openrouter'], 'sk-or-synthetic', False, True),
                                           (['openrouter', '--model', 'missing/model'], 'sk-or-synthetic', True, False),
                                           (['treg'], 'no-team-token', True, True)):
            with self.subTest(argv=argv, secret=secret):
                code, env, config, _ = self.run_cli(argv, secret, valid, model)
                self.assertEqual((code, env, config), (1, '', []))

    def test_offline_check_still_stores_with_notice(self):
        code, env, _, text = self.run_cli(['openrouter'], 'sk-or-synthetic', valid=None, model=None)
        self.assertEqual(code, 0)
        self.assertIn('nicht online geprüft', text)
