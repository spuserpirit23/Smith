#!/usr/bin/env bash
# Ship the working tree to a VM over SSH and (re)start the stack. Works with any provider.
#
#   deploy/scripts/push_deploy.sh ubuntu@<host> [path/to/key.pem]
set -euo pipefail
HOST="$1"; KEY="${2:-}"
SSH=(ssh -o StrictHostKeyChecking=accept-new ${KEY:+-i "$KEY"})
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

rsync -az --delete \
  --exclude '.git' --exclude 'deploy/.env' --exclude '**/__pycache__' --exclude 'firmware/**/build' \
  --exclude '*.nvs.csv' --exclude '*.nvs.bin' \
  -e "${SSH[*]}" "$ROOT/" "$HOST:/tmp/mitra-src/"
"${SSH[@]}" "$HOST" 'sudo mkdir -p /opt/mitra && sudo rsync -a --exclude deploy/.env /tmp/mitra-src/ /opt/mitra/ \
  && sudo bash /opt/mitra/deploy/scripts/bootstrap.sh $( [ -f /etc/mitra-bootstrap.env ] && echo /etc/mitra-bootstrap.env )'
