#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
set -a
# shellcheck disable=SC1091
source .env
set +a
N8N_DOMAIN="${N8N_DOMAIN:?N8N_DOMAIN required}"
DUCKDNS_NAME="${DUCKDNS_NAME:?DUCKDNS_NAME required}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:?DUCKDNS_TOKEN required}"

echo "==> [1/9] Safe cleanup (remove ~/200 and ~/404 junk only)"
rm -rf "$HOME/200" "$HOME/404"

echo "==> [2/9] Swapfile 2G (VM has <1G RAM)"
if ! swapon --show | grep -q swapfile; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> [3/9] Docker + nginx + certbot"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io nginx certbot curl >/dev/null
systemctl enable --now docker >/dev/null 2>&1 || true
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif apt-get install -y -qq docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "FATAL: no docker compose available" >&2
  exit 1
fi
echo "    using: $COMPOSE"

echo "==> [4/9] DuckDNS update + 6h refresh cron"
curl -fsS "https://duckdns.org/update?domains=${DUCKDNS_NAME}&token=${DUCKDNS_TOKEN}&ip=" || echo "    DuckDNS update returned non-zero (check token)"
( crontab -l 2>/dev/null | grep -v duckdns || true
  echo "0 */6 * * * curl -fsS \"https://duckdns.org/update?domains=${DUCKDNS_NAME}&token=${DUCKDNS_TOKEN}&ip=\" >/dev/null 2>&1 || true"
) | crontab -

echo "==> [5/9] Prepare dirs + bootstrap HTTP nginx site (no cert yet)"
mkdir -p /var/www/certbot
mkdir -p shared
chmod 777 shared
cat > /etc/nginx/sites-available/agenticflow1.conf <<EOF
server {
    listen 80;
    server_name ${N8N_DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / {
        proxy_pass http://127.0.0.1:5678;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/agenticflow1.conf /etc/nginx/sites-enabled/agenticflow1.conf
nginx -t
systemctl reload nginx

echo "==> [6/9] Certbot certificate for ${N8N_DOMAIN}"
if [ ! -d "/etc/letsencrypt/live/${N8N_DOMAIN}" ]; then
  certbot certonly --webroot -w /var/www/certbot -d "$N8N_DOMAIN" \
    --non-interactive --agree-tos --register-unsafely-without-email
fi

echo "==> [7/9] Install final nginx config (80->443 + n8n + /media/)"
cp nginx/agenticflow1.conf /etc/nginx/sites-available/agenticflow1.conf
nginx -t
systemctl reload nginx

echo "==> [8/9] Build and start containers"
$COMPOSE up -d --build

echo "==> [9/9] Import workflow into n8n (public_origin substituted)"
sed "s|__PUBLIC_ORIGIN__|https://${N8N_DOMAIN}|g" workflows/meesho-reels-pipeline.json > /tmp/meesho-reels-pipeline.json
for i in $(seq 1 12); do
  if docker exec n8n n8n import:workflow --input=/tmp/meesho-reels-pipeline.json; then
    echo "    workflow imported"
    break
  fi
  echo "    n8n not ready yet (attempt $i), retrying in 10s..."
  sleep 10
done

echo "==> Verification"
sleep 3
echo -n "    https://${N8N_DOMAIN}/ -> "
curl -s -o /dev/null -w "%{http_code}\n" "https://${N8N_DOMAIN}/" || echo "curl failed"
echo -n "    https://skopaq.duckdns.org/ -> "
curl -s -o /dev/null -w "%{http_code}\n" "https://skopaq.duckdns.org/" || echo "curl failed"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo "==> Done. Next: open https://${N8N_DOMAIN}/ , create owner account, attach credentials, review imported workflow."
