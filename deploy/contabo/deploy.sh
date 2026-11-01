#!/usr/bin/env bash
# Deploy Hawkeye to the Contabo host. Run from the GlitchTip repo root:
#
#   deploy/contabo/deploy.sh [path-to-hawkeye-ui]
#
# Source is sent with `git archive` over SSH, so the server never holds GitHub
# credentials and only committed code is deployed. Secrets are generated on the
# server the first time (see bootstrap-env.sh) and never leave it.
set -euo pipefail

SERVER="${HAWKEYE_SERVER:-root@13.140.136.228}"
KEY="${HAWKEYE_SSH_KEY:-$HOME/.ssh/medsgh_contabo}"
PORT="${HAWKEYE_PORT:-8090}"
UI_REPO="${1:-../hawkeye-ui}"
DIR=/opt/hawkeye
SSH="ssh -i $KEY -o BatchMode=yes $SERVER"

HOST="${SERVER#*@}"
URL="http://${HOST}:${PORT}"

# The address the server sees us connect from: the only one let in at first.
MY_IP="$($SSH 'echo ${SSH_CLIENT%% *}')"
echo "Deploying to $URL (allowlist: $MY_IP)"

$SSH "mkdir -p $DIR/backend $DIR/ui"

echo "Sending backend source"
$SSH "find $DIR/backend -mindepth 1 -delete"
git -c core.autocrlf=false archive HEAD | $SSH "tar -x -C $DIR/backend"

echo "Sending UI source"
$SSH "find $DIR/ui -mindepth 1 -delete"
git -C "$UI_REPO" -c core.autocrlf=false archive HEAD | $SSH "tar -x -C $DIR/ui"

echo "Sending deployment files"
for f in compose.yml Caddyfile caddy.Dockerfile bootstrap-env.sh; do
    $SSH "cat > $DIR/$f" < "deploy/contabo/$f"
done
$SSH "chmod +x $DIR/bootstrap-env.sh && $DIR/bootstrap-env.sh '$URL' '$HOST' '$MY_IP'"

echo "Building and starting"
$SSH "cd $DIR && docker compose up -d --build"
$SSH "cd $DIR && docker compose ps"
