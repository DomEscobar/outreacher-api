#!/bin/sh
set -eu
cd /opt/data/workspace
exec hermes chat --toolsets browser,web,terminal,file,skills -s session-safety,decision-maker-discovery "$@"
