#!/usr/bin/env bash
# Bring up the Mitra stack on any Linux VM with Docker (EC2, GCE, Azure VM, Hetzner, bare metal).
#
#   sudo deploy/scripts/bootstrap.sh [overrides.env]
#
# - Creates deploy/.env on first run with freshly generated secrets (never committed).
# - If MITRA_SSM_PREFIX is set and the AWS CLI works (EC2 instance role), the admin token is also
#   stored in SSM Parameter Store so operators can fetch it without shell access.
# - Re-running is safe: existing secrets are kept, the stack is rebuilt and restarted.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEPLOY="$ROOT/deploy"
ENV_FILE="$DEPLOY/.env"
OVERRIDES="${1:-}"

[ -n "$OVERRIDES" ] && set -a && . "$OVERRIDES" && set +a

gen() { openssl rand -hex "${1:-32}"; }

if [ ! -f "$ENV_FILE" ]; then
  echo "bootstrap: creating $ENV_FILE"
  cp "$DEPLOY/.env.example" "$ENV_FILE"
  sed -i \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(gen 24)|" \
    -e "s|^MITRA_ADMIN_TOKEN=.*|MITRA_ADMIN_TOKEN=$(gen 32)|" \
    -e "s|^MITRA_SIGNING_SECRET=.*|MITRA_SIGNING_SECRET=$(gen 32)|" \
    -e "s|^MITRA_MQTT_PASSWORD=.*|MITRA_MQTT_PASSWORD=$(gen 24)|" \
    "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

set_kv() {  # set_kv KEY VALUE  (only when VALUE is non-empty)
  [ -z "$2" ] && return 0
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
set_kv MITRA_DOMAIN "${MITRA_DOMAIN:-}"
set_kv ACME_EMAIL "${ACME_EMAIL:-}"
set_kv AWS_REGION "${AWS_REGION:-}"
set_kv MITRA_EXTRAS "${MITRA_EXTRAS:-}"

if [ -n "${MITRA_SSM_PREFIX:-}" ] && command -v aws >/dev/null; then
  token=$(grep '^MITRA_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2)
  aws ssm put-parameter --region "${AWS_REGION}" --name "${MITRA_SSM_PREFIX}/admin_token" \
    --type SecureString --value "$token" --overwrite >/dev/null \
    && echo "bootstrap: admin token stored at ${MITRA_SSM_PREFIX}/admin_token" \
    || echo "bootstrap: could not write SSM parameter (continuing)"
fi

cd "$DEPLOY"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null || true
echo "bootstrap: stack is up. Health: curl -fsS https://\$(grep ^MITRA_DOMAIN= .env | cut -d= -f2)/healthz"
