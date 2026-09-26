#!/usr/bin/env bash
# Ship this repo to the host, build the image there and (re)start the stack via Dockge.
# The ssh host comes from $HOST, or from a gitignored ./deploy.local (e.g. HOST=myserver).
#   ./deploy.sh            build + restart
#   ./deploy.sh --no-up    build only
set -euo pipefail

cd "$(dirname "$0")"
[ -f deploy.local ] && . ./deploy.local
HOST=${HOST:?set HOST to the ssh host that runs the stack, or put HOST=... in deploy.local}
DEST=${DEST:-compose/mealie-toolkit}   # relative to ~ on the host

rsync -a --delete \
  --exclude .git --exclude .env --exclude deploy.local --exclude data/ --exclude export/ \
  --exclude __pycache__ --exclude .venv --exclude .pytest_cache --exclude '*.egg-info' \
  ./ "$HOST:$DEST/"

ssh "$HOST" "set -e
  cd ~/$DEST
  mkdir -p data
  test -f .env || { echo 'no .env on the host: copy .env.example to .env and set MEALIE_TOKEN'; exit 1; }
  chmod 600 .env
  podman build -q -t localhost/mealie-toolkit:latest -f Containerfile .
  if [ \"${1:-}\" != --no-up ]; then
    podman exec -w \$HOME/$DEST dockge docker compose up -d
  fi"
