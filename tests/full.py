import json
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]
mode=json.loads((ROOT/'.state/mode.json').read_text())
assert mode['mode']=='local' and mode['test'], 'Full test only uses synthetic local setup'
def cmd(*args, **kwargs):
    return subprocess.run(args,cwd=ROOT,check=True,**kwargs)
cmd('python3','tests/smoke.py')
try:
    cmd('docker','compose','exec','-T','session','hermes','config','set','browser.allow_private_urls','true')
    with (ROOT/'tests/browser_integration.py').open() as f:
        cmd('docker','compose','exec','-T','session','python3','-','seed',stdin=f)
    cmd('docker','compose','restart','session')
    cmd('docker','compose','--profile','local','up','-d','--wait','--wait-timeout','120')
    with (ROOT/'tests/browser_integration.py').open() as f:
        cmd('docker','compose','exec','-T','session','python3','-','verify',stdin=f)
finally:
    cmd('docker','compose','exec','-T','session','hermes','config','set','browser.allow_private_urls','false')
cmd('python3','tests/smoke.py')
print('PASS: full local integration including browser session persistence')
