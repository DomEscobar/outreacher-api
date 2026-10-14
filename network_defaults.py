"""Discover VPS address and locally bindable ports; never alter firewall/services."""
import ipaddress
import json
import re
import socket
import subprocess
import urllib.request


def is_ip(value):
    try: ipaddress.ip_address(value); return True
    except ValueError: return False


def valid_host(value):
    if is_ip(value): return ipaddress.ip_address(value).is_global
    labels=value.split('.')
    return (4<=len(value)<=253 and len(labels)>=2 and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels))


def public_addresses():
    found=set()
    try:
        rows=json.loads(subprocess.check_output(['ip','-j','address','show','scope','global'],text=True,timeout=3))
        for row in rows:
            for addr in row.get('addr_info',[]):
                value=addr.get('local','')
                if is_ip(value) and ipaddress.ip_address(value).is_global: found.add(value)
    except (OSError,ValueError,subprocess.SubprocessError): pass
    # NAT fallback is used only when no public interface address exists.
    if not found:
        try:
            with urllib.request.urlopen('https://api.ipify.org',timeout=5) as response:
                value=response.read(128).decode().strip()
            if is_ip(value) and ipaddress.ip_address(value).is_global: found.add(value)
        except (OSError,ValueError): pass
    return sorted(found,key=lambda value:(ipaddress.ip_address(value).version,value))


def discover_host():
    addresses=public_addresses()
    try:
        candidate=subprocess.check_output(['hostname','-f'],text=True,timeout=3).strip().lower().rstrip('.')
    except (OSError,subprocess.SubprocessError): candidate=socket.getfqdn().lower().rstrip('.')
    if valid_host(candidate) and not is_ip(candidate):
        try:
            resolved={row[4][0] for row in socket.getaddrinfo(candidate,None)}
            if resolved.intersection(addresses): return {'host':candidate,'addresses':addresses,'kind':'domain'}
        except socket.gaierror: pass
    if addresses: return {'host':addresses[0],'addresses':addresses,'kind':'ip'}
    return {'host':'','addresses':[],'kind':'unknown'}


def port_available(port):
    if not 1<=port<=65535: return False
    sockets=[]
    try:
        v4=socket.socket(socket.AF_INET,socket.SOCK_STREAM); sockets.append(v4)
        v4.bind(('0.0.0.0',port))
        if socket.has_ipv6:
            try:
                v6=socket.socket(socket.AF_INET6,socket.SOCK_STREAM); sockets.append(v6)
                v6.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                v6.bind(('::',port))
            except OSError as error:
                # IPv6 absent on the host is not an occupied-port result.
                if error.errno not in (97,99): raise
        return True
    except OSError: return False
    finally:
        for sock in sockets: sock.close()


def suggest_port(preferred=8443):
    candidates=[preferred,8443,9443,10443,*range(8444,8500)]
    for port in dict.fromkeys(candidates):
        if port_available(port): return port
    raise RuntimeError('No locally bindable test port found; choose a host port manually')


def url_for(mode):
    host=mode['host'] if mode['mode']=='public' else 'localhost'
    if ':' in host: host='['+host+']'
    port=mode.get('port',443) if mode['mode']=='local' or 'tls_mode' in mode else 443
    return 'https://'+host+(':'+str(port) if port!=443 else '')+'/'


def edge_profile(mode):
    if mode['mode']=='local': return 'local'
    return 'acme' if mode.get('tls_mode','acme')=='acme' else 'public'
