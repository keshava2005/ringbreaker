#!/usr/bin/env bash
# Installs or updates RingBreaker on an Amazon Linux 2023 host. Idempotent: safe to re-run.
# Reads its settings from /etc/ringbreaker.env (written once by EC2 user data) and secrets from
# SSM Parameter Store. Nothing secret is ever written into Terraform state or this repo.
set -euo pipefail
exec > >(tee -a /var/log/ringbreaker-bootstrap.log) 2>&1
echo "=== bootstrap $(date -u +%FT%TZ) ==="
source /etc/ringbreaker.env   # REGION, BUCKET, KEY, PARAM_PREFIX
param() { aws ssm get-parameter --region "$REGION" --name "$PARAM_PREFIX/$1" --with-decryption \
            --query Parameter.Value --output text 2>/dev/null || true; }

# 1. swap: the model build needs more than 1 GB of RAM on a t3.micro
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile swap swap defaults 0 0' >> /etc/fstab
fi

# 2. docker + compose plugin
dnf install -y -q docker unzip >/dev/null
systemctl enable --now docker
if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

# 3. code from the private S3 bucket
aws s3 cp --region "$REGION" "s3://$BUCKET/$KEY" /tmp/ringbreaker-app.zip --only-show-errors
rm -rf /opt/ringbreaker.new && mkdir -p /opt/ringbreaker.new
unzip -q /tmp/ringbreaker-app.zip -d /opt/ringbreaker.new
mkdir -p /opt/ringbreaker-state && chown 1000:1000 /opt/ringbreaker-state
if [ -d /opt/ringbreaker ]; then rm -rf /opt/ringbreaker.old && mv /opt/ringbreaker /opt/ringbreaker.old; fi
mv /opt/ringbreaker.new /opt/ringbreaker
cd /opt/ringbreaker/deploy

# 4. settings and secrets
DOMAIN="$(param domain)";        [ "$DOMAIN" = "none" ] && DOMAIN=""
LIVE_USER="$(param live_user)";  LIVE_USER="${LIVE_USER:-analyst}"
LIVE_PW="$(param live_password)"
GROQ="$(param groq_api_key)"
TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
PUBLIC_IP=$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || true)

umask 077
cat > .env <<ENV
GROQ_API_KEY=${GROQ}
RINGBREAKER_ALLOWED_HOSTS=${DOMAIN}
ENV

# 5. Caddy config. Public: the read-only demo. /live: the real app, password-protected, HTTPS only.
if [ -n "$DOMAIN" ] && [ -n "$LIVE_PW" ]; then
  HASH=$(docker run --rm caddy:2.8.4 caddy hash-password --plaintext "$LIVE_PW")
  LIVE_BLOCK="redir /live /live/ 308
	handle_path /live/* {
		basic_auth {
			${LIVE_USER} ${HASH}
		}
		reverse_proxy app:8000 {
			flush_interval -1
		}
	}"
else
  LIVE_BLOCK="handle /live* {
		respond \"The live app needs the HTTPS domain and a password (SSM ${PARAM_PREFIX}/live_password).\" 403
	}"
fi
SITE_COMMON="encode zstd gzip
	header {
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		X-Frame-Options DENY
		-Server
	}
	${LIVE_BLOCK}
	handle {
		rewrite * /site{path}
		reverse_proxy app:8000 {
			header_up Host localhost
		}
	}"
{
  if [ -n "$DOMAIN" ]; then
    printf '%s {\n\theader Strict-Transport-Security "max-age=31536000"\n\t%s\n}\n\n' "$DOMAIN" "$SITE_COMMON"
  fi
  # plain-HTTP fallback by IP (before DNS exists): public demo only, /live is refused
  printf ':80 {\n\tencode zstd gzip\n\thandle /live* {\n\t\trespond "Use the HTTPS domain for the live app." 403\n\t}\n\thandle {\n\t\trewrite * /site{path}\n\t\treverse_proxy app:8000 {\n\t\t\theader_up Host localhost\n\t\t}\n\t}\n}\n'
} > Caddyfile
chmod 644 Caddyfile

# 6. build and (re)start
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
docker image prune -f >/dev/null
rm -rf /opt/ringbreaker.old
echo "=== done. domain='${DOMAIN:-<none>}' ip=${PUBLIC_IP} live=$([ -n "$DOMAIN" ] && [ -n "$LIVE_PW" ] && echo on || echo off) ==="
