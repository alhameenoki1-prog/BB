#!/usr/bin/env bash
# One-command install on a fresh Ubuntu/Debian server with a domain pointing
# at it. Installs Docker, fetches the code, writes .env, starts the site
# behind Caddy with automatic HTTPS, and keeps it running across reboots.
#
#   curl -fsSL https://raw.githubusercontent.com/alhameenoki1-prog/BB/main/deploy/install.sh | sudo bash -s -- \
#       outreach.example.com you@example.com 'a-long-password'
set -euo pipefail
DOMAIN="${1:?usage: install.sh DOMAIN ADMIN_EMAIL ADMIN_PASSWORD}"
ADMIN_EMAIL="${2:?usage: install.sh DOMAIN ADMIN_EMAIL ADMIN_PASSWORD}"
ADMIN_PASSWORD="${3:?usage: install.sh DOMAIN ADMIN_EMAIL ADMIN_PASSWORD}"
REPO="${BB_REPO:-https://github.com/alhameenoki1-prog/BB.git}"
DIR="${BB_DIR:-/opt/bb}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"
if [ ! -f .env ]; then
  cat > .env <<ENV
BB_BASE_URL=https://$DOMAIN
BB_DOMAIN=$DOMAIN
BB_ADMIN_EMAIL=$ADMIN_EMAIL
BB_ADMIN_PASSWORD=$ADMIN_PASSWORD
ENV
  chmod 600 .env
  echo "Wrote $DIR/.env"
else
  echo "$DIR/.env exists; leaving it alone."
fi
# The container runs as uid 1000; a data folder created by Docker would be
# root's and the site could not write its database.
mkdir -p "$DIR/data" && chown -R 1000:1000 "$DIR/data"
docker compose --profile tls up -d --build
echo
echo "BB Outreach is up at https://$DOMAIN (the certificate takes a minute the first time)."
echo "Sign in as $ADMIN_EMAIL, open Users, and create a login for each person."
echo "Update later with: cd $DIR && git pull && docker compose --profile tls up -d --build"
