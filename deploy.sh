#!/usr/bin/env bash
# Deploy this checkout to the live install at /opt/bambuddy and restart the service.
# Local-only script (not upstreamed).
#
# Update flow:
#   git fetch upstream main:upstream-main && git merge upstream-main   # on branch `main`
#   ./deploy.sh
set -euo pipefail

REPO=/app/bambuddy
DEST=/opt/bambuddy
SERVICE=bambuddy

cd "$REPO"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: uncommitted changes in $REPO — commit or stash first." >&2
    exit 1
fi

REV=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "Deploying $BRANCH @ ${REV:0:8} -> $DEST"

sudo rsync -a --delete \
    --exclude venv --exclude data --exclude logs \
    --exclude .env --exclude .git --exclude node_modules \
    "$REPO/" "$DEST/"
echo "$BRANCH $REV $(date -Iseconds)" | sudo tee "$DEST/.deployed-rev" >/dev/null
sudo chown -R bambuddy:bambuddy "$DEST"

sudo systemctl restart "$SERVICE"

echo -n "Waiting for HTTP"
for _ in $(seq 1 24); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ || true)
    [ "$code" = "200" ] && break
    echo -n "."
    sleep 5
done
echo
if [ "${code:-}" != "200" ]; then
    echo "ERROR: service did not become healthy (last HTTP code: ${code:-none})." >&2
    echo "Check: sudo journalctl -u $SERVICE -n 50" >&2
    exit 1
fi
echo "OK: $SERVICE healthy, deployed $(cat "$DEST/.deployed-rev" 2>/dev/null || sudo cat "$DEST/.deployed-rev")"
