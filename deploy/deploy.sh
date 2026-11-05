#!/usr/bin/env bash
# Deploy SolarCast UA to a cloud VM over SSH. The VM needs Docker with the compose
# plugin and ports 80 and 443 open.
#
#   ./deploy/deploy.sh <ssh-host> [site-address]
#   ./deploy/deploy.sh solarcast                         # https://solarcast.<ip>.sslip.io
#   ./deploy/deploy.sh solarcast solarcast.example.com   # your own domain (A record -> the VM)
#
# Copies the sources and .env to /opt/solarcast-ua, builds the image there and
# (re)starts two containers: the app and Caddy, which obtains the HTTPS certificate.
set -euo pipefail
HOST="${1:?usage: deploy.sh <ssh-host> [site-address]}"
IP="$(ssh -G "$HOST" | awk '/^hostname /{print $2}')"
# sslip.io resolves <anything>.<ip-with-dashes>.sslip.io to that IP: HTTPS without owning a domain
SITE="${2:-solarcast.${IP//./-}.sslip.io}"
DIR=/opt/solarcast-ua
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here: copy .env.example and put the API key in it"; exit 1; }

ssh "$HOST" "docker compose version >/dev/null && mkdir -p $DIR"
rsync -az --delete --exclude .env --exclude Caddyfile --exclude compose.yml \
  app static requirements.txt Dockerfile .dockerignore "$HOST:$DIR/"
rsync -az deploy/compose.yml deploy/Caddyfile "$HOST:$DIR/"
ssh "$HOST" "umask 077 && cat > $DIR/.env" < .env            # the key: readable by root only
ssh "$HOST" "cd $DIR && SITE_ADDRESS=$SITE docker compose up -d --build 2>&1 | tail -5"

echo "waiting for https://$SITE (the first certificate takes up to a minute)..."
for _ in $(seq 1 40); do
  if curl -fsS --max-time 5 "https://$SITE/api/health" 2>/dev/null; then
    echo; echo "deployed: https://$SITE"; exit 0
  fi
  sleep 3
done
echo "not reachable yet. Container state and proxy log:"
ssh "$HOST" "docker ps --format '{{.Names}}: {{.Status}}'; docker logs --tail 25 solarcast-caddy 2>&1"
exit 1
