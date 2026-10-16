#!/usr/bin/env bash
# One-time setup and every redeploy on the GCP VM (Debian 12). Idempotent.
#   sudo bash vm-setup.sh <static-ip>
# Expects ~/monday-list.tar.gz (from `git archive HEAD`) next to this script.
set -euo pipefail

IP="${1:?usage: vm-setup.sh <static-ip>}"
APP_DIR=/opt/monday-list
TARBALL="$(dirname "$0")/monday-list.tar.gz"

# Docker Engine and the Compose plugin from Docker's apt repository (Debian's own packages
# ship a Compose too old for env_file "required: false").
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# 2 GB of swap: e2-small has 2 GB of RAM, and the first bootstrap trains on about 316k rows.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Code: replace everything except .env, which holds this VM's secrets.
mkdir -p "$APP_DIR"
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .env -exec rm -rf {} +
tar -xzf "$TARBALL" -C "$APP_DIR"
cd "$APP_DIR"

# Secrets are generated here, once, and never leave the VM.
rand() { openssl rand -hex 24; }
SECRET_KEYS="APP_OWNER_PASSWORD APP_BATCH_PASSWORD APP_PASSWORD SESSION_SECRET PII_HASH_SECRET"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i \
    -e "s|^APP_OWNER_PASSWORD=.*|APP_OWNER_PASSWORD=$(rand)|" \
    -e "s|^APP_BATCH_PASSWORD=.*|APP_BATCH_PASSWORD=$(rand)|" \
    -e "s|^APP_PASSWORD=.*|APP_PASSWORD=$(rand)|" \
    -e "s|^SESSION_SECRET=.*|SESSION_SECRET=$(rand)|" \
    -e "s|^BASIC_AUTH_PASSWORD=.*|BASIC_AUTH_PASSWORD=$(openssl rand -hex 8)|" \
    -e "s|^COOKIE_SECURE=.*|COOKIE_SECURE=1|" \
    -e "s|^PII_HASH_SECRET=.*|PII_HASH_SECRET=$(rand)|" \
    .env
  {
    echo "POSTGRES_PASSWORD=$(rand)"
    echo "SITE_ADDRESS=${IP//./-}.sslip.io"
  } >> .env
  chmod 600 .env
fi
# Keys added to .env.example after this VM's .env was written. A secret gets a fresh random
# value, never the example's dev placeholder; anything else takes the example's default.
while IFS='=' read -r key _; do
  [[ "$key" =~ ^[A-Z_]+$ ]] || continue
  grep -q "^${key}=" .env && continue
  if [[ " $SECRET_KEYS BASIC_AUTH_PASSWORD " == *" $key "* ]]; then
    echo "${key}=$(rand)" >> .env
  else
    grep "^${key}=" .env.example >> .env
  fi
done < .env.example

docker compose -f compose.yaml -f deploy/compose.gcp.yaml up -d --build
docker compose -f compose.yaml -f deploy/compose.gcp.yaml ps
echo "Site: https://${IP//./-}.sslip.io  (password: sudo grep BASIC_AUTH_PASSWORD $APP_DIR/.env)"
