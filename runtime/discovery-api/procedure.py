"""Load the shipped procedure, never a mutable user/profile skill or caller path."""
from pathlib import Path
from research import BackendError

VERSION = '2.0.0'
APPLIANCE_SKILL = Path('/opt/browser-box/skills/linkedin-discovery/SKILL.md')
HOST_SKILL = Path(__file__).resolve().parents[2] / 'skills/linkedin-discovery/SKILL.md'


def trusted_procedure():
    path = APPLIANCE_SKILL if APPLIANCE_SKILL.exists() else HOST_SKILL
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise BackendError('procedure_unavailable') from None
    parts = text.split('---', 2)
    if len(text.encode()) > 16000 or not text.startswith('---\n') or len(parts) != 3 or f'\nversion: {VERSION}\n' not in parts[1] or '\nname: linkedin-discovery\n' not in parts[1]:
        raise BackendError('procedure_unavailable')
    return f'TRUSTED_DISCOVERY_PROCEDURE_VERSION={VERSION}\n' + text
