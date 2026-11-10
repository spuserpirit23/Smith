#!/bin/bash
# First-boot bootstrap for the Mitra EC2 instance (Ubuntu 24.04 arm64).
set -euxo pipefail
exec > >(tee -a /var/log/mitra-bootstrap.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-v2 git openssl rsync
snap install aws-cli --classic || true   # Ubuntu 24.04 ships the AWS CLI as a snap
systemctl enable --now docker

# 1 GB swap: cheap insurance against memory spikes during image builds / model loads.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p /opt/mitra
if [ -n "${repo_url}" ] && [ ! -d /opt/mitra/.git ]; then
  git clone --branch "${repo_ref}" "${repo_url}" /opt/mitra
fi

cat > /etc/mitra-bootstrap.env <<EOF
MITRA_DOMAIN=${domain}
ACME_EMAIL=${acme_email}
AWS_REGION=${region}
MITRA_SSM_PREFIX=${ssm_prefix}
MITRA_EXTRAS=${extras}
EOF

if [ -f /opt/mitra/deploy/scripts/bootstrap.sh ]; then
  bash /opt/mitra/deploy/scripts/bootstrap.sh /etc/mitra-bootstrap.env
else
  echo "No repo cloned. Run deploy/scripts/push_deploy.sh from your machine to ship the code."
fi
