#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
local_mode=false
for arg in "$@"; do
  if [ "$arg" = --local ]; then local_mode=true; fi
done
if ! command -v python3 >/dev/null 2>&1; then
  if "$local_mode"; then
    printf 'Local mode installs nothing. Install Python 3.10+ in this terminal environment first (WSL Ubuntu on Windows). See LOCAL-PC.md.\n' >&2
    exit 1
  fi
  printf 'Python 3 fehlt. Auf Debian/Ubuntu jetzt installieren? [j/N] '
  read -r answer
  case "$answer" in
    j|J|ja|yes)
      if [ "$(id -u)" != 0 ] || ! command -v apt-get >/dev/null 2>&1; then
        printf 'Bitte Python 3 installieren oder auf Debian/Ubuntu mit sudo bash setup.sh starten.\n' >&2
        exit 1
      fi
      apt-get update
      apt-get install -y python3 ca-certificates
      ;;
    *) printf 'Abgebrochen; keine Änderungen.\n'; exit 1 ;;
  esac
fi
python3 -c 'import sys; sys.exit("Python 3.10+ required; see LOCAL-PC.md") if sys.version_info < (3,10) else None'
exec python3 wizard.py "$@"
