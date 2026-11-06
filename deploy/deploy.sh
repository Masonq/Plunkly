#!/usr/bin/env bash
# Plunkly one-shot deploy. Usage (as root): curl -fsSL https://raw.githubusercontent.com/Masonq/Plunkly/main/deploy/deploy.sh | BOT_TOKEN=... GH_PAT=... bash
set -uo pipefail
APP=/opt/plunkly; DOMAIN=plunkly.89-208-113-147.sslip.io; PORT=8417; EMAIL=maxsim_kolesnikov@icloud.com
mkdir -p /var/log/plunkly; LOG=/var/log/plunkly/deploy.txt; : > "$LOG"
say(){ echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
run(){ say "\$ $*"; "$@" >>"$LOG" 2>&1; local c=$?; [ $c -ne 0 ] && say "  -> exit $c"; return $c; }
ENVF=/etc/plunkly.env
if [ -n "${BOT_TOKEN:-}" ]; then
  SECRET=$(grep -s '^WEBHOOK_SECRET=' $ENVF | cut -d= -f2); [ -z "$SECRET" ] && SECRET=$(openssl rand -hex 16)
  printf 'BOT_TOKEN=%s\nBASE_URL=https://%s\nWEBHOOK_SECRET=%s\nGH_PAT=%s\n' "$BOT_TOKEN" "$DOMAIN" "$SECRET" "${GH_PAT:-}" > $ENVF; chmod 600 $ENVF
fi
set -a; . $ENVF; set +a
REPO="https://x-access-token:${GH_PAT}@github.com/Masonq/Plunkly.git"
say "== packages"; export DEBIAN_FRONTEND=noninteractive
command -v nginx >/dev/null && command -v certbot >/dev/null && dpkg -s python3-venv >/dev/null 2>&1 || { run apt-get update -y; run apt-get install -y git nginx python3-venv certbot python3-certbot-nginx; }
say "== code"
if [ -d $APP/.git ]; then run git -C $APP remote set-url origin "$REPO"; run git -C $APP fetch origin main; run git -C $APP reset --hard origin/main; else run git clone "$REPO" $APP; fi
VER=$(git -C $APP rev-parse --short HEAD); say "version $VER"
grep -q '^APP_VERSION=' $ENVF && sed -i "s/^APP_VERSION=.*/APP_VERSION=$VER/" $ENVF || echo "APP_VERSION=$VER" >> $ENVF
say "== python"; [ -x $APP/.venv/bin/python ] || run python3 -m venv $APP/.venv; run $APP/.venv/bin/pip install -q -r $APP/requirements.txt
say "== service"
cat > /etc/systemd/system/plunkly.service <<UNIT
[Unit]
Description=Plunkly game + bot
After=network.target
[Service]
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$APP/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --proxy-headers
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
run systemctl daemon-reload; run systemctl enable plunkly; run systemctl restart plunkly
say "== nginx"
if [ ! -f /etc/nginx/sites-available/plunkly ]; then
cat > /etc/nginx/sites-available/plunkly <<NG
server {
  listen 80;
  server_name $DOMAIN;
  client_max_body_size 2m;
  location / { proxy_pass http://127.0.0.1:$PORT; proxy_set_header Host \$host; proxy_set_header X-Forwarded-Proto \$scheme; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; }
}
NG
ln -sf /etc/nginx/sites-available/plunkly /etc/nginx/sites-enabled/plunkly
fi
run nginx -t && run systemctl reload nginx
grep -q "ssl_certificate" /etc/nginx/sites-available/plunkly || run certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m $EMAIL --redirect
run systemctl reload nginx
sleep 3
say "== checks"
say "local: $(curl -s -m 5 http://127.0.0.1:$PORT/healthz)"
say "https: $(curl -s -m 10 https://$DOMAIN/healthz)"
say "index head: $(curl -sI -m 10 https://$DOMAIN/ | tr -d '\r' | grep -iE '^(HTTP|cache-control)' | tr '\n' ' ')"
say "asset: $(curl -sI -m 10 "https://$DOMAIN/assets/hero_0.webp?v=$VER" | tr -d '\r' | grep -iE '^(HTTP|cache-control)' | tr '\n' ' ')"
say "webhook: $(curl -s -m 10 https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo | sed "s/$BOT_TOKEN/***/g" | sed -E 's#webhook/[0-9a-f]+#webhook/***#')"
say "service: $(systemctl is-active plunkly)"
journalctl -u plunkly -n 30 --no-pager >> "$LOG" 2>&1
sed -i "s/${BOT_TOKEN}/***/g; s/${GH_PAT}/***/g; s/${WEBHOOK_SECRET}/***/g" "$LOG"
say "== report"
TMP=$(mktemp -d); cd $TMP && git init -q && git checkout -q -b reports && mkdir logs && cp "$LOG" logs/deploy.txt && git add logs && git -c user.name=plunkly-server -c user.email=server@plunkly -c commit.gpgsign=false commit -qm "deploy $VER $(date -u +%FT%TZ)" && git push -qf "$REPO" reports >/dev/null 2>&1 && echo "Report pushed. Open: https://$DOMAIN" || echo "Report push failed, see $LOG"
rm -rf $TMP
