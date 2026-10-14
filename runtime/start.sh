#!/bin/sh
set -eu
umask 077
mkdir -p /opt/data/workspace /opt/data/skills /opt/data/browser-profile /opt/data/logs /opt/data/discovery-api
chmod 700 /opt/data/discovery-api
cp -R /opt/browser-box/skills/. /opt/data/skills/
if [ ! -f /opt/data/.browser-box-configured ]; then
  hermes config set terminal.backend local
  hermes config set terminal.cwd /opt/data/workspace
  hermes config set browser.cdp_url http://127.0.0.1:9222
  hermes config set browser.backend off
  hermes config set browser.headed true
  hermes config set browser.restrict_evaluate true
  hermes config set security.redact_secrets true
  hermes config set approvals.mode manual
  touch /opt/data/.browser-box-configured
fi
exec /usr/bin/supervisord -n -c /opt/browser-box/supervisord.conf
