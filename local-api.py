#!/usr/bin/env python3
"""One explicit authenticated local API request; no polling, schedules or messages."""
import argparse
import base64
import getpass
import json
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings
from network_defaults import url_for

ROOT = Path(__file__).resolve().parent

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials or replay a POST to another URL.

def request_once(base, ca, password, payload=None, job=None):
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme!='https' or parsed.hostname not in ('localhost','127.0.0.1','::1') or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('Use the local HTTPS origin only, without credentials, path or query.')
    if job and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',job): raise ValueError('Invalid job ID')
    context=ssl.create_default_context(cafile=str(ca) if ca else None)
    path='discoveries/'+job if job else 'discoveries' if payload is not None else 'openapi.json'
    headers={'Authorization':'Basic '+base64.b64encode(('dom:'+password).encode()).decode(),'Accept':'application/json'}
    data=json.dumps(payload).encode() if payload is not None else None
    if data is not None: headers['Content-Type']='application/json'
    req=urllib.request.Request(base.rstrip('/')+'/api/v1/'+path,data=data,headers=headers,method='POST' if data is not None else 'GET')
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=context),urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=30) as response:
        return json.load(response)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',help='Local HTTPS origin; defaults to your .state/mode.json URL')
    parser.add_argument('--ca',type=Path,default=ROOT/'.state/local-ca.crt',help='Exact appliance public CA certificate')
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--request',type=Path,help='Submit this criteria JSON once (may cost provider credits)')
    action.add_argument('--job',help='GET this job once; run again manually to check progress')
    args=parser.parse_args()
    if not sys.stdin.isatty(): parser.error('Use an interactive terminal; passwords are never read from pipes or arguments.')
    base=args.base
    if not base:
        mode=json.loads((ROOT/'.state/mode.json').read_text())
        if mode['mode']!='local': parser.error('This launcher only supports the local PC endpoint.')
        base=url_for(mode)
    payload=json.loads(args.request.read_text()) if args.request else None
    if payload is not None and not isinstance(payload,dict): parser.error('Request JSON must be an object')
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        password=getpass.getpass('Appliance password for dom (masked): ')
    try:
        result=request_once(base,args.ca,password,payload,args.job)
    finally:
        del password
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try: main()
    except urllib.error.HTTPError as error:
        print(f'API returned HTTP {error.code}; no success claimed.',file=sys.stderr); sys.exit(1)
    except (OSError,ValueError,getpass.GetPassWarning) as error:
        print(f'Local request failed: {error}',file=sys.stderr); sys.exit(1)
