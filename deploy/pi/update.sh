#!/bin/sh
# Pull the latest code from GitHub. Reinstall when dependencies changed and restart the dashboard when anything
# changed. Claude sessions pick up new code, skills and CLAUDE.md on their own (the MCP server starts per session).
# $1: the systemd instance (the checkout's folder name), for the coach-web@ unit to restart.
set -eu
cd "$(dirname "$0")/../.."
instance=${1:-$(basename "$PWD")}
before=$(git rev-parse HEAD)
git pull --ff-only --quiet
after=$(git rev-parse HEAD)
[ "$before" = "$after" ] && exit 0
echo "updated ${before%"${before#???????}"} -> ${after%"${after#???????}"}"
if git diff --name-only "$before" "$after" | grep -qx pyproject.toml; then
  .venv/bin/pip install --quiet -e .
fi
systemctl --user restart "coach-web@$instance.service"
