"""Synthetic unit tests for process-local Hermes tool restrictions."""
import json
from types import SimpleNamespace
import unittest
from policy import ALLOWED_TOOLS, allowed_call, install_policy


class PolicyTests(unittest.TestCase):
    def test_forbidden_tools(self):
        for name in ['browser_vault_fill', 'browser_vault_list', 'browser_exec', 'browser_click',
                     'browser_type', 'browser_press', 'browser_cdp', 'browser_console', 'terminal',
                     'execute_code', 'send_message', 'cronjob', 'tool_call', 'delegate_task']:
            with self.subTest(name=name): self.assertFalse(allowed_call(name, {}))

    def test_safe_navigation(self):
        self.assertTrue(allowed_call('browser_navigate', {'url': 'https://www.linkedin.com/in/synthetic-fixture-person'}))
        self.assertTrue(allowed_call('browser_navigate', {'url': 'https://www.example.com/team'}))
        for url in ['https://www.linkedin.com/messaging/', 'https://www.linkedin.com/login',
                    'https://www.linkedin.com/checkpoint/x', 'https://www.linkedin.com/mynetwork/',
                    'https://127.0.0.1/', 'file:///tmp/a', 'http://example.com', 'https://example.com:9222/']:
            with self.subTest(url=url): self.assertFalse(allowed_call('browser_navigate', {'url': url}))

    def test_extract_urls(self):
        self.assertTrue(allowed_call('web_extract', {'urls': ['https://www.example.com/team']}))
        for values in [None, [], ['https://localhost/x'], ['https://www.example.com'] * 6]:
            self.assertFalse(allowed_call('web_extract', {'urls': values}))

    def test_selection_and_execution_fail_closed(self):
        calls = []
        def execute(name, args, *other, **kwargs): calls.append(name); return 'synthetic-tool-result'
        stub = SimpleNamespace(_select_tool_names=lambda *a, **k: {'web_search', 'terminal', 'browser_vault_fill'},
            get_tool_definitions=lambda *a, **k: [{'function': {'name': n}} for n in ['web_search', 'terminal']],
            _execute_tool=execute)
        install_policy(stub)
        self.assertEqual(stub._select_tool_names(), {'web_search'})
        self.assertEqual(stub.get_tool_definitions(), [{'function': {'name': 'web_search'}}])
        self.assertEqual(json.loads(stub._execute_tool('terminal', {}, {}, None))['error'], 'discovery_policy_denied')
        self.assertEqual(stub._execute_tool('web_search', {'query': 'synthetic'}, {}, None), 'synthetic-tool-result')
        self.assertEqual(calls, ['web_search'])
        self.assertNotIn('browser_exec', ALLOWED_TOOLS)


class AgentPolicyTests(unittest.TestCase):
    def test_no_memory_review_or_auxiliary_schemas(self):
        from agent_policy import install_agent_policy
        class SyntheticAgent:
            def __init__(self, skip_memory=False, skip_background_review=False, enabled_toolsets=None):
                self.values = (skip_memory, skip_background_review, enabled_toolsets)
                self.tools = [{'function': {'name': n}} for n in ['web_search', 'memory', 'terminal']]
                self.valid_tool_names = {'web_search', 'memory', 'terminal'}
                self._context_engine_tool_names = {'memory'}
        install_agent_policy(SyntheticAgent)
        agent = SyntheticAgent(enabled_toolsets=['terminal'])
        self.assertEqual(agent.values, (True, True, ['browser', 'web']))
        self.assertEqual(agent.valid_tool_names, {'web_search'})
        self.assertEqual(agent._context_engine_tool_names, set())

    def test_incompatible_agent_fails_closed(self):
        from agent_policy import install_agent_policy
        class UnsupportedAgent:
            def __init__(self): pass
        with self.assertRaises(RuntimeError): install_agent_policy(UnsupportedAgent)


if __name__ == '__main__': unittest.main()
