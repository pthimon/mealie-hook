#!/usr/bin/env bash
# Ship this repo to marvin, build the image there and (re)start the stack via Dockge.
#   ./deploy.sh            build + restart
#   ./deploy.sh --no-up    build only
set -euo pipefail

HOST=${HOST:-homelab}
DEST=${DEST:-compose/mealie-hook}   # relative to ~ on the host
cd "$(dirname "$0")"

rsync -a --delete \
  --exclude .git --exclude .env --exclude data/ --exclude export/ \
  --exclude __pycache__ --exclude .venv --exclude .pytest_cache --exclude '*.egg-info' \
  ./ "$HOST:$DEST/"

ssh "$HOST" "set -e
  cd ~/$DEST
  mkdir -p data
  test -f .env || { echo 'no .env on the host: copy .env.example to .env and set MEALIE_TOKEN'; exit 1; }
  chmod 600 .env
  podman build -q -t localhost/mealie-hook:latest -f Containerfile .
  if [ \"${1:-}\" != --no-up ]; then
    podman exec -w \$HOME/$DEST dockge docker compose up -d
  fi"
