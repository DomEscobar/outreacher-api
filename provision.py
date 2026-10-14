#!/usr/bin/env python3
"""Provision an isolated browser appliance; never deploy public implicitly."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import uuid
import time
import sys
import warnings
from network_defaults import valid_host, is_ip, port_available, suggest_port, url_for, edge_profile

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'.state'

def run(args,**kwargs): return subprocess.run(args,cwd=ROOT,check=True,**kwargs)

def private_write(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream: stream.write(value)
    os.chmod(path,0o600)

def pinned(image):
    run(['docker','pull',image])
    refs=json.loads(subprocess.check_output(['docker','image','inspect',image,'--format','{{json .RepoDigests}}'],text=True))
    if not refs: raise RuntimeError('Image has no registry digest; refusing unpinned deployment')
    return image if '@sha256:' in image else refs[0]

def compose(profile,*args): run(['docker','compose','--profile',profile,*args])

def configuration(args):
    host=args.domain or 'localhost'
    public=bool(args.domain)
    if public and not valid_host(host): raise SystemExit('Supply a valid public DNS name or public IP, without scheme, brackets, port or path.')
    if public and args.test: raise SystemExit('--test must remain local')
    tls=args.tls_mode
    if not public: tls='internal'
    elif tls=='auto': tls='acme' if not is_ip(host) and port_available(80) and port_available(443) else 'internal'
    if tls=='acme' and is_ip(host): raise SystemExit('This tested Caddy setup uses internal TLS for IP addresses.')
    port=args.port if args.port is not None else (443 if tls=='acme' else suggest_port(8443) if public else suggest_port(19443))
    if not 1<=port<=65535: raise SystemExit('Port must be 1..65535')
    if tls=='acme' and public and port!=443: raise SystemExit('ACME mode needs standard ports 80/443. Use --tls-mode internal for an isolated custom port.')
    if (not public or tls=='internal') and port<1024: raise SystemExit('Custom/local port must be 1024..65535')
    return {'mode':'public' if public else 'local','host':host,'port':port,'tls_mode':tls,'test':args.test}

def render_caddy(mode,hashed):
    host=mode['host']
    if ':' in host: host='['+host+']'
    tls='    tls internal\n' if mode['tls_mode']=='internal' else ''
    # Internal container port is always 443. Host custom port is a Compose mapping.
    return f'''https://{host} {{
{tls}    basic_auth {{
        dom {hashed}
    }}
    header {{
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        Cache-Control no-store
    }}
    # Inside handle so the redirect runs after basic_auth (bare redir is ordered before it).
    @root path /
    handle @root {{
        redir /vnc.html?autoconnect=1&resize=remote 302
    }}
    @discovery path /api/v1 /api/v1/* /ui /ui/*
    handle @discovery {{
        reverse_proxy session:8081
    }}
    handle {{
        reverse_proxy session:6080
    }}
}}
'''

def default_project():
    return 'hermes-browser-v6-' + uuid.uuid4().hex[:12]

def check_architecture(image):
    info=json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(info.get('Architecture'),info.get('Architecture'))
    if info.get('OSType')!='linux' or arch not in ('amd64','arm64'):
        raise SystemExit('Requires a Linux amd64/arm64 Docker engine; switch Docker Desktop to Linux containers.')
    manifest=json.loads(subprocess.check_output(['docker','manifest','inspect',image],text=True))
    platforms={(m.get('platform',{}).get('os'),m.get('platform',{}).get('architecture')) for m in manifest.get('manifests',[])}
    if ('linux',arch) not in platforms:
        raise SystemExit(f'Image index does not advertise linux/{arch}; refusing implicit emulation or architecture mismatch.')
    print(f'Image index verified for Docker engine linux/{arch}.')

def export_ca(profile):
    deadline=time.monotonic()+30
    while True:
        try:
            data=subprocess.check_output(['docker','compose','--profile',profile,'exec','-T',profile,'cat','/data/caddy/pki/authorities/local/root.crt'],cwd=ROOT,stderr=subprocess.DEVNULL)
            break
        except subprocess.CalledProcessError:
            if time.monotonic()>=deadline: raise SystemExit('CA not ready after 30 seconds; inspect Caddy logs and rerun export-ca. TLS verification is not disabled.')
            time.sleep(1)
    if b'BEGIN CERTIFICATE' not in data: raise SystemExit('Caddy CA export failed')
    private_write(STATE/'local-ca.crt',data.decode('ascii'))
    print('CA certificate exported to .state/local-ca.crt (public certificate only; no private key).')

def configure(args):
    if (ROOT/'.env').exists() and not args.reconfigure: raise SystemExit('Already configured. Use --reconfigure to explicitly replace configuration/password.')
    if not args.test and not sys.stdin.isatty(): raise SystemExit('Configure needs an interactive terminal for masked password input; no secrets via pipes.')
    mode=configuration(args)
    if args.project_name is None:
        if args.reconfigure and (ROOT/'.env').exists():
            values=dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if '=' in line)
            args.project_name=values.get('COMPOSE_PROJECT_NAME')
        args.project_name=args.project_name or default_project()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}',args.project_name): raise SystemExit('Invalid Docker project name')
    if not (ROOT/'.env').exists():
        for kind in ('container','volume','network'):
            existing=subprocess.check_output(['docker',kind,'ls','-q','--filter','label=com.docker.compose.project='+args.project_name],text=True).strip()
            if existing: raise SystemExit('Project already owns containers/volumes; use a fresh unique project name. Nothing adopted or deleted.')
    check_architecture(args.hermes_image)
    required=(80,443) if mode['mode']=='public' and mode['tls_mode']=='acme' else (mode['port'],)
    for port in required:
        if not port_available(port): raise SystemExit(f'Port {port} is unavailable. No service will be stopped; choose another port.')
    STATE.mkdir(mode=0o700,exist_ok=True); os.chmod(STATE,0o700)
    if args.test:
        password=(
            secrets.token_urlsafe(32)
        )
    else:
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            password=getpass.getpass('Remote desktop password (16+ characters): ')
            confirmation=getpass.getpass('Repeat password: ')
        if password!=confirmation or len(password)<16: raise SystemExit('Passwords must match and contain at least 16 characters.')
    caddy=pinned('caddy:2.10.2-alpine'); hermes=pinned(args.hermes_image)
    hashed=subprocess.check_output(['docker','run','--rm','-i',caddy,'caddy','hash-password'],input=password+'\n',text=True).strip().splitlines()[-1]
    if not hashed.startswith('$2'): raise SystemExit('Password hashing failed')
    private_write(STATE/'Caddyfile',render_caddy(mode,hashed))
    bind='127.0.0.1:' if mode['mode']=='local' or args.test else ''
    private_write(ROOT/'.env',f'COMPOSE_PROJECT_NAME={args.project_name}\nHERMES_IMAGE={hermes}\nCADDY_IMAGE={caddy}\nLOCAL_PORT={mode["port"]}\nPUBLIC_PORT={mode["port"]}\nPUBLIC_BIND_PREFIX={bind}\nDISCOVERY_ALLOWED_ORIGIN={url_for(mode).rstrip("/")}\n')
    private_write(STATE/'mode.json',json.dumps(mode))
    if args.test: private_write(STATE/'test-auth.json',json.dumps({'username':'dom','password':password}))
    del password
    print('Configured. No containers started. Images pinned by digest.')
    if mode['mode']=='public' and mode['tls_mode']=='internal': print('Custom-port HTTPS uses an internal CA: browser certificate warning; NOT publicly trusted TLS.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('configure')
    p.add_argument('--domain','--host',dest='domain')
    p.add_argument('--port',type=int)
    p.add_argument('--tls-mode',choices=['auto','acme','internal'],default='auto')
    p.add_argument('--project-name',help='Fresh isolated instance name; defaults to a generated v6 name')
    p.add_argument('--hermes-image',default='nousresearch/hermes-agent@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b')
    p.add_argument('--test',action='store_true')
    p.add_argument('--reconfigure',action='store_true')
    commands.add_parser('up-local')
    p=commands.add_parser('up-public'); p.add_argument('--confirm-public',action='store_true')
    for name in ('status','stop','shell','test','export-ca'): commands.add_parser(name)
    args=parser.parse_args()
    if args.command=='configure':
        configure(args); return
    if not (STATE/'mode.json').exists(): raise SystemExit('Run configure first')
    mode=json.loads((STATE/'mode.json').read_text()); profile=edge_profile(mode)
    if args.command=='up-public':
        if not args.confirm_public or mode['mode']!='public' or mode['test']: raise SystemExit('Public mode needs explicit --confirm-public. No test credentials.')
        print('Host authorization and inbound firewall/provider security-group rules required. No firewall changes are made.')
        compose(profile,'up','-d','--build','--wait','--wait-timeout','300')
        print('URL: '+url_for(mode)+' — username: dom')
        if mode.get('tls_mode')=='internal': print('TLS internal CA: encrypted, but certificate is not publicly trusted.')
    elif args.command=='up-local':
        if mode['mode']!='local': raise SystemExit('Reconfigure for local access first')
        compose('local','up','-d','--build','--wait','--wait-timeout','300')
        print('Local-only: '+url_for(mode)+' — username: dom. Local CA certificate.')
    elif args.command=='export-ca':
        if mode.get('tls_mode')!='internal': raise SystemExit('CA export is only for internal TLS')
        export_ca(profile)
    elif args.command=='status': compose(profile,'ps')
    elif args.command=='stop': compose(profile,'stop')
    elif args.command=='shell': compose(profile,'exec','session','bash')
    elif args.command=='test': run(['python3',str(ROOT/'tests/full.py')])

if __name__=='__main__': main()
