#!/opt/hermes/.venv/bin/python
"""Non-interactive provider setup used by the wizard. The secret arrives on stdin only
(never argv, env or output) and is stored in HERMES_HOME/.env (mode 0600).

  configure_provider.py openrouter --model <id>   # key on stdin; sets Hermes' default model
  configure_provider.py treg                       # treg.to token on stdin (fast lead search)
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.append('/opt/browser-box/discovery-api')  # installed location; never shadows a local checkout
from search_backend import save_env_value, treg_org  # noqa: E402

OPENROUTER_BASE = 'https://openrouter.ai/api/v1'


def check_openrouter(key):
    """True = key accepted, False = rejected, None = could not check (network)."""
    req = urllib.request.Request(OPENROUTER_BASE + '/key', headers={'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return bool(json.load(r).get('data') is not None)
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def model_exists(model):
    try:
        with urllib.request.urlopen(OPENROUTER_BASE + '/models', timeout=20) as r:
            return any(m.get('id') == model for m in json.load(r).get('data', []))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def hermes_config(key, value):
    subprocess.run(['hermes', 'config', 'set', key, value], check=True, stdout=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('provider', choices=['openrouter', 'treg'])
    ap.add_argument('--model', default='deepseek/deepseek-v4.1-flash')
    a = ap.parse_args()
    secret = sys.stdin.read().strip()
    if a.provider == 'treg':
        if not treg_org(secret):
            print('treg-Token nicht erkannt (kein Team im Token); nichts gespeichert.', file=sys.stderr); return 1
        save_env_value('TREG_TOKEN', secret)
        print(f'treg-Token gespeichert (Team {treg_org(secret)}).')
        return 0
    if not secret.startswith('sk-or-'):
        print('Das sieht nicht nach einem OpenRouter-Key aus (beginnt mit sk-or-); nichts gespeichert.', file=sys.stderr); return 1
    valid = check_openrouter(secret)
    if valid is False:
        print('OpenRouter hat den Key abgelehnt; nichts gespeichert.', file=sys.stderr); return 1
    if model_exists(a.model) is False:
        print(f'Modell {a.model} gibt es bei OpenRouter nicht; nichts gespeichert.', file=sys.stderr); return 1
    save_env_value('OPENROUTER_API_KEY', secret)
    for key, value in (('model.provider', 'openrouter'), ('model.default', a.model),
                       ('model.base_url', OPENROUTER_BASE), ('model.api_mode', 'chat_completions')):
        hermes_config(key, value)
    print(f'OpenRouter eingerichtet, Standardmodell {a.model}' +
          ('' if valid else ' (Key konnte gerade nicht online geprüft werden)') + '.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
