#!/bin/sh
# Runs on the server, in /opt/hawkeye. Creates .env with fresh secrets the first
# time and leaves an existing .env alone, so redeploys never rotate secrets.
#
#   ./bootstrap-env.sh <public-url> <host> "<allowed client ips, space separated>"
set -eu

URL="$1"
HOST="$2"
ALLOW="$3"

cd /opt/hawkeye
if [ -f .env ]; then
    echo ".env already exists; leaving it alone"
    exit 0
fi

umask 077
PG_PASSWORD="$(openssl rand -hex 24)"
SECRET="$(openssl rand -hex 40)"

cat > .env <<ENV
# Generated $(date -u +%Y-%m-%dT%H:%MZ). Holds secrets: keep mode 600, never commit.
POSTGRES_PASSWORD=${PG_PASSWORD}
DATABASE_URL=postgres://hawkeye:${PG_PASSWORD}@postgres:5432/hawkeye
VALKEY_URL=redis://valkey:6379
SECRET_KEY=${SECRET}

DEBUG=false
GLITCHTIP_URL=${URL}
ALLOWED_HOSTS=${HOST},localhost
CSRF_TRUSTED_ORIGINS=${URL}
LOG_LEVEL=INFO

# Caddy only lets these addresses reach the UI, API, sign-in and admin.
ALLOW_IPS=${ALLOW}

MEDIA_ROOT=/code/uploads

# Invite-only. The first user on an empty instance can still sign up.
ENABLE_USER_REGISTRATION=false
ENABLE_OPENAPI=false

# Email, billing and social sign-in are intentionally not configured yet.
ENV

chmod 600 .env
echo "created .env"
