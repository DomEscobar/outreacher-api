#!/bin/sh
# Runs leadpipe.py inside the session container. Keys never appear in the output:
# FIRECRAWL_API_KEY from .state/firecrawl.env (KEY=value), the treg token from .state/treg.env
# (raw token), OPENROUTER_API_KEY from the container's /opt/data/.env.
# Usage: [LEADPIPE_BACKEND=treg] [LEADPIPE_OFFLINE=1] sh prototypes/leadpipe/run.sh <outdir> A|B|C [options]
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$1; shift
FIRECRAWL_API_KEY=; TREG_TOKEN=
[ -f "$ROOT/.state/firecrawl.env" ] && . "$ROOT/.state/firecrawl.env"
[ -f "$ROOT/.state/treg.env" ] && TREG_TOKEN=$(tr -d ' \r\n' < "$ROOT/.state/treg.env")
S=$(cd "$ROOT" && docker compose ps -q session)
export MSYS_NO_PATHCONV=1
# Git Bash: hand docker a native Windows path (pwd -W); elsewhere plain pwd.
ROOT_NATIVE=$(cd "$ROOT" && (pwd -W 2>/dev/null || pwd))
docker cp "$ROOT_NATIVE/prototypes/leadpipe/leadpipe.py" "$S:/tmp/leadpipe.py" >/dev/null
docker exec -u 10000 -e FIRECRAWL_API_KEY="$FIRECRAWL_API_KEY" -e TREG_TOKEN="$TREG_TOKEN" \
  -e LEADPIPE_BACKEND="${LEADPIPE_BACKEND:-firecrawl}" -e LEADPIPE_CACHE=/opt/data/leadpipe-cache \
  -e LEADPIPE_OFFLINE="${LEADPIPE_OFFLINE:-}" "$S" sh -c \
  'export "$(grep "^OPENROUTER_API_KEY=" /opt/data/.env)"; python3 /tmp/leadpipe.py "$@"' sh "$@" \
  > "$OUT/leads_$1.jsonl" 2> "$OUT/stats_$1.txt"
tail -1 "$OUT/stats_$1.txt"
