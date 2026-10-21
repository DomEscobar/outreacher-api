#!/usr/bin/env python3
"""Configure a stable residential/ISP egress for the browser box.

Buys nothing and stores no credentials beyond BROWSER_PROXY in the private .env.
Run once after you have a dedicated static residential / ISP endpoint.

  python3 setup_proxy.py                 # interactive, tests the endpoint, writes .env
  python3 setup_proxy.py --apply         # also recreate the session container
  python3 setup_proxy.py --proxy http://gw.example:1234 --apply

Provider categories (one dedicated, static IP per LinkedIn account; pick Germany):
  iproyal.com      "Royal ISP"        webshare.io     "ISP proxies"
  proxy-seller.com "ISP"              brightdata.com  "ISP Proxies"
  oxylabs.io       "ISP Proxies"      Mobile/4G: iproyal.com, proxy-seller.com
Ask the provider for IP whitelisting ("IP authentication") so no password is needed.
Chromium cannot use user:password in --proxy-server.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
HOSTING = ('hosting', 'cloud', 'datacenter', 'data center', 'digitalocean', 'hetzner', 'ovh', 'amazon',
           'google', 'microsoft', 'azure', 'contabo', 'vultr', 'linode', 'leaseweb', 'scaleway', 'netcup')


def fetch_json(url, proxy=None, timeout=15):
    """JSON via the proxy, or strictly direct when proxy is None (ignores HTTP(S)_PROXY env)."""
    handler = urllib.request.ProxyHandler({} if proxy is None else {'http': proxy, 'https': proxy})
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(url, headers={'User-Agent': 'browser-box-setup/1.0'})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def public_ip(proxy=None, timeout=15):
    for url in ('https://ipinfo.io/json', 'https://ipapi.co/json/'):
        try:
            data = fetch_json(url, proxy, timeout)
            ip = data.get('ip') or data.get('query')
            if ip:
                return {'ip': ip, 'org': data.get('org') or data.get('asn') or '',
                        'city': data.get('city') or '', 'country': data.get('country_name') or data.get('country') or ''}
        except Exception:
            continue
    return None


def normalize(value):
    value = (value or '').strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'http://' + value
    return value


def mask(value):
    parts = urlsplit(value)
    if not (parts.username or parts.password):
        return value
    host = parts.hostname or ''
    port = f':{parts.port}' if parts.port else ''
    return urlunsplit((parts.scheme, f'{parts.username or ""}:***@{host}{port}', parts.path, parts.query, parts.fragment))


def validate(proxy):
    parts = urlsplit(proxy)
    if parts.scheme not in ('http', 'https', 'socks5', 'socks5h'):
        raise SystemExit('Proxy must start with http://, https://, socks5:// or socks5h://')
    if not parts.hostname or not parts.port:
        raise SystemExit('Proxy needs host:port, e.g. http://gateway.provider.example:1234')
    if parts.username or parts.password:
        raise SystemExit('Chromium cannot use user:password in --proxy-server.\n'
                         'Whitelist this VPS IP at the provider instead, or run a local forwarder '
                         '(gost/3proxy) and point BROWSER_PROXY at 127.0.0.1.')


def read_env(path):
    if not path.exists():
        raise SystemExit('No .env found. Run: python3 provision.py configure')
    return path.read_text(encoding='utf-8').splitlines()


def write_env(path, lines):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def set_values(lines, updates):
    seen = set()
    result = []
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in updates:
            result.append(f'{key}={updates[key]}')
            seen.add(key)
        else:
            result.append(line)
    for key, value in updates.items():
        if key not in seen:
            result.append(f'{key}={value}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--proxy', help='http(s)://host:port or socks5://host:port (no user:password)')
    parser.add_argument('--env', default=str(ROOT / '.env'), help='path to .env (default: repo .env)')
    parser.add_argument('--apply', action='store_true', help='recreate the session container and verify')
    parser.add_argument('--skip-test', action='store_true', help='write .env without testing the endpoint')
    parser.add_argument('--timeout', type=int, default=15)
    args = parser.parse_args()

    env_path = Path(args.env)
    direct = public_ip(timeout=args.timeout)
    print('This VPS public IP:', direct['ip'] if direct else '(could not detect)')
    if direct:
        print(f"  ASN/org: {direct['org'] or '?'}  {direct['city']} {direct['country']}".rstrip())
    print('\nWhitelist this IP at the proxy provider (IP authentication), then continue.')
    print('Buy a dedicated static residential / ISP endpoint in Germany, one per LinkedIn account.\n')

    proxy = normalize(args.proxy)
    if not proxy:
        proxy = normalize(input('Proxy URL (e.g. http://gateway.provider.example:1234): '))
    if not proxy:
        raise SystemExit('No proxy URL given.')
    validate(proxy)

    result = None
    if not args.skip_test:
        if urlsplit(proxy).scheme.startswith('socks'):
            print('SOCKS: stdlib cannot test it here; Chromium supports it, verify in the browser.')
        else:
            print('Testing through the proxy ...')
            result = public_ip(proxy, args.timeout)
            if not result:
                raise SystemExit('Proxy test failed. Check host/port and that this VPS IP is whitelisted.')
            print(f"Proxy egress IP: {result['ip']}  {result['city']} {result['country']}".rstrip())
            print(f"  ASN/org: {result['org'] or '?'}")
            if direct and result['ip'] == direct['ip']:
                raise SystemExit('The proxy returns this VPS IP: traffic is NOT going through it.')
            if any(word in (result['org'] or '').lower() for word in HOSTING):
                print('WARNING: the egress org looks like hosting/cloud, not residential/ISP.')
                print('         Prefer a dedicated ISP/mobile endpoint in Germany.')

    lines = read_env(env_path)
    updates = {'BROWSER_PROXY': proxy}
    existing = {line.split('=', 1)[0]: line.split('=', 1)[1] for line in lines if '=' in line and not line.lstrip().startswith('#')}
    updates.setdefault('BROWSER_TZ', existing.get('BROWSER_TZ') or 'Europe/Berlin')
    updates.setdefault('BROWSER_LANG', existing.get('BROWSER_LANG') or 'de-DE')
    write_env(env_path, set_values(lines, updates))
    print(f'\nWrote {env_path} (0600): BROWSER_PROXY={mask(proxy)}')

    if not args.apply:
        print('\nNext:')
        print('  docker compose up -d --force-recreate session')
        print('  docker compose exec session sh -c \'curl -s -x "$BROWSER_PROXY" https://ifconfig.me\'')
        print('Then open https://browserleaks.com/ip and https://dnsleaktest.com in the noVNC browser.')
        return 0

    print('\nRecreating the session container ...')
    try:
        subprocess.run(['docker', 'compose', 'up', '-d', '--force-recreate', 'session'], cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Could not recreate the container: {exc}') from None
    print('Verifying from inside the container ...')
    check = subprocess.run(['docker', 'compose', 'exec', '-T', 'session', 'sh', '-lc',
                            'printf "BROWSER_PROXY=%s\\n" "$BROWSER_PROXY"; '
                            'command -v curl >/dev/null && curl -s -x "$BROWSER_PROXY" https://ifconfig.me || true'],
                           cwd=ROOT, text=True, capture_output=True)
    print(check.stdout.strip() or '(no output)')
    if check.returncode != 0:
        print(check.stderr.strip(), file=sys.stderr)
        print('Container check failed; verify manually in the noVNC browser.', file=sys.stderr)
        return 2
    print('\nNow check browserleaks.com/ip and dnsleaktest.com in the noVNC browser; '
          'the IP must be the residential/ISP one and stay the same after a restart.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
