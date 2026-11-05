#!/usr/bin/env bash
# Deploy SolarCast UA to a VM over SSH. The VM needs Docker with the compose plugin.
#
#   ./deploy/deploy.sh <ssh-host> [port]        e.g.  ./deploy/deploy.sh minidrive 8088
#
# Copies the sources and .env to /opt/solarcast-ua, builds the image there and
# (re)starts one container. Other containers and services on the VM are not touched.
set -euo pipefail
HOST="${1:?usage: deploy.sh <ssh-host> [port]}"
PORT="${2:-8088}"
DIR=/opt/solarcast-ua
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here: copy .env.example and put the API key in it"; exit 1; }

ssh "$HOST" "docker compose version >/dev/null && mkdir -p $DIR"
# refuse to take a port that something else already listens on
if ssh "$HOST" "ss -ltn | grep -q ':$PORT ' && ! docker ps --format '{{.Names}}' | grep -qx solarcast-ua"; then
  echo "port $PORT is already in use on $HOST: pick another one"; exit 1
fi
rsync -az --delete --exclude .env app static requirements.txt Dockerfile .dockerignore "$HOST:$DIR/"
rsync -az deploy/compose.yml "$HOST:$DIR/compose.yml"
rsync -az --chmod=600 .env "$HOST:$DIR/.env"                 # the key: readable by root only
ssh "$HOST" "cd $DIR && SOLARCAST_PORT=$PORT docker compose up -d --build 2>&1 | tail -4"

echo "waiting for the service..."
for _ in $(seq 1 20); do
  if ssh "$HOST" "curl -fsS http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
    echo; echo "deployed: http://$(ssh -G "$HOST" | awk '/^hostname /{print $2}'):$PORT"; exit 0
  fi
  sleep 2
done
echo "the container did not become healthy:"; ssh "$HOST" "docker logs --tail 30 solarcast-ua"; exit 1
