#!/opt/hermes/.venv/bin/python
"""Standalone restricted CLI entrypoint; install mode 0755, run as session user."""
import json
import sys
from pathlib import Path

# Load local policy first so local module names cannot be shadowed by Hermes.
from policy import ALLOWED_TOOLS, install_policy
from agent_policy import install_agent_policy


def main():
    sys.path.insert(0, '/opt/hermes')
    try:
        import model_tools
        install_policy(model_tools)
        from run_agent import AIAgent
        install_agent_policy(AIAgent)
    except Exception:
        print(json.dumps({'type': 'result', 'exit_code': 1, 'text': '', 'error': 'policy_initialization_failed'}))
        return 1
    if sys.argv[1:] == ['--check-policy']:
        definitions = model_tools.get_tool_definitions(enabled_toolsets=['browser', 'web'], quiet_mode=True)
        names = sorted(d['function']['name'] for d in definitions)
        if not set(names) <= ALLOWED_TOOLS:
            return 1
        print(json.dumps({'allowed_tools': names, 'policy': 'discovery-read-only-v1'}))
        return 0
    from hermes_cli.main import main as hermes_main
    return hermes_main()


if __name__ == '__main__':
    sys.exit(main())
