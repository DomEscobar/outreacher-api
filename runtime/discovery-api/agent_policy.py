"""Disable memory/background review and post-init auxiliary schemas locally."""
import functools
import inspect
from policy import ALLOWED_TOOLS


def install_agent_policy(agent_class):
    original = agent_class.__init__
    params = inspect.signature(original).parameters
    if not {'skip_memory', 'skip_background_review', 'enabled_toolsets'} <= set(params):
        raise RuntimeError('unsupported agent policy interface')

    @functools.wraps(original)
    def restricted_init(self, *args, **kwargs):
        kwargs['skip_memory'] = True
        kwargs['skip_background_review'] = True
        kwargs['enabled_toolsets'] = ['browser', 'web']
        original(self, *args, **kwargs)
        self.tools = [t for t in (self.tools or []) if t.get('function', {}).get('name') in ALLOWED_TOOLS]
        self.valid_tool_names = {t['function']['name'] for t in self.tools}
        if hasattr(self, '_context_engine_tool_names'):
            self._context_engine_tool_names &= ALLOWED_TOOLS
    agent_class.__init__ = restricted_init
