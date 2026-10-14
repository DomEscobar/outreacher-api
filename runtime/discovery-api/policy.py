"""Process-local technical tool allowlist; never edits installed Hermes source.

The live `browser` bundle includes vault, click, CDP and arbitrary browser_exec.
Those are intentionally removed for this API, rather than relying on a prompt.
"""
import json
import os
import threading
from urllib.parse import urlsplit
from models import public_url

ALLOWED_TOOLS = frozenset({'web_search', 'web_extract', 'browser_navigate', 'browser_snapshot',
                           'browser_scroll', 'browser_back', 'browser_get_images'})


def allowed_call(name, args):
    if name not in ALLOWED_TOOLS or not isinstance(args, dict): return False
    urls = []
    if name == 'browser_navigate':
        urls = [args.get('url')]
    elif name == 'web_extract':
        urls = args.get('urls')
        if not isinstance(urls, list) or not 1 <= len(urls) <= 5: return False
    for url in urls:
        if not isinstance(url, str): return False
        try: public_url(url)
        except (ValueError, TypeError): return False
        p = urlsplit(url)
        host = p.hostname.lower()
        from urllib.parse import unquote
        decoded_path = unquote(p.path)
        if any(segment in {'.', '..'} for segment in decoded_path.split('/')):
            return False
        if host == 'linkedin.com' or host.endswith('.linkedin.com'):
            if host != 'www.linkedin.com': return False
            try:
                if p.path == '/search/results/people/':
                    from models import Criteria
                    Criteria(search_url=url)
                elif p.path.startswith('/in/'):
                    from models import profile_url
                    profile_url(url)
                else:
                    import re
                    if not re.fullmatch(r'/company/[\w\-]+/(?:about/)?', decoded_path) or p.query or p.fragment:
                        return False
            except (ValueError, TypeError):
                return False
    return True


def install_policy(model_tools, tool_budget=None):
    # Source-verified API names for the pinned appliance. Fail closed on incompatibility.
    select = model_tools._select_tool_names
    definitions = model_tools.get_tool_definitions
    execute = model_tools._execute_tool
    if tool_budget is None and 'DISCOVERY_TOOL_BUDGET' in os.environ:
        value = os.environ['DISCOVERY_TOOL_BUDGET']
        if not value.isdecimal() or not 1 <= int(value) <= 30:
            raise RuntimeError('invalid discovery tool budget')
        tool_budget = int(value)
    if tool_budget is not None and (type(tool_budget) is not int or not 1 <= tool_budget <= 30):
        raise RuntimeError('invalid discovery tool budget')
    remaining = tool_budget
    lock = threading.Lock()
    def restricted_select(*args, **kwargs):
        return set(select(*args, **kwargs)) & ALLOWED_TOOLS
    def restricted_definitions(*args, **kwargs):
        return [d for d in definitions(*args, **kwargs) if d.get('function', {}).get('name') in ALLOWED_TOOLS]
    def restricted_execute(function_name, function_args, *args, **kwargs):
        nonlocal remaining
        if not allowed_call(function_name, function_args):
            return json.dumps({'error': 'discovery_policy_denied'})
        with lock:
            if remaining is not None:
                if remaining <= 0:
                    return json.dumps({'error': 'discovery_tool_budget_exhausted'})
                remaining -= 1
        return execute(function_name, function_args, *args, **kwargs)
    model_tools._select_tool_names = restricted_select
    model_tools.get_tool_definitions = restricted_definitions
    model_tools._execute_tool = restricted_execute
    return ALLOWED_TOOLS
