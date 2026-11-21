#!/usr/bin/env bash
# Deploy the MyLife phase-0 demo to R760 (run from the repo root in Git Bash).
#   bash deploy/deploy.sh            # page + backend + configs
#   bash deploy/deploy.sh --secrets  # also push .secrets/mylife.env to /etc/mylife/secrets.env
# One-time prerequisites already done on the server (2026-11-21): system user `mylife`,
# /opt/mylife/{app,www}, /var/cache/mylife, /etc/mylife, and the Let's Encrypt cert:
#   certbot certonly --dns-cloudflare --dns-cloudflare-credentials /root/.secrets/certbot-cloudflare.ini \
#     --dns-cloudflare-propagation-seconds 30 --cert-name mylife.instmarket.com.au -d mylife.instmarket.com.au
set -euo pipefail
cd "$(dirname "$0")/.."

# Deploy target lives outside the repo: .secrets/deploy.env (gitignored), e.g.
#   MYLIFE_DEPLOY_HOST=root@<server-ip>
#   MYLIFE_DEPLOY_PORT=<ssh-port>
#   MYLIFE_DEPLOY_KEY=$HOME/.ssh/id_ed25519
[ -f .secrets/deploy.env ] && . .secrets/deploy.env
: "${MYLIFE_DEPLOY_HOST:?set MYLIFE_DEPLOY_HOST in .secrets/deploy.env}"
: "${MYLIFE_DEPLOY_PORT:=22}"
: "${MYLIFE_DEPLOY_KEY:=$HOME/.ssh/id_ed25519}"
HOST=$MYLIFE_DEPLOY_HOST
SSH_OPTS=(-p "$MYLIFE_DEPLOY_PORT" -i "$MYLIFE_DEPLOY_KEY" -o IdentitiesOnly=yes -o BatchMode=yes)
SCP_OPTS=(-q -P "$MYLIFE_DEPLOY_PORT" -i "$MYLIFE_DEPLOY_KEY" -o IdentitiesOnly=yes)
STAGE=/tmp/mylife-deploy
SERVER_FILES="mylife_server.py memory.py voices.json calibrate.py"

ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
scp "${SCP_OPTS[@]}" $(printf 'server/%s ' $SERVER_FILES) \
  prototype/mylife-prototype.html deploy/nginx-mylife.conf deploy/mylife.service "$HOST:$STAGE/"

# Persona prompts (档案 + 风格示例) and everything in prompts/ (另一个你, 去AI味, 收尾一句, 彩虹卡).
# The service is restarted below, so the server picks them up (it caches them after the first read).
tar -czf - personas/*/人格档案.md personas/*/风格示例.md prompts | ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $STAGE/content && tar -xzf - -C $STAGE/content"

if [ "${1:-}" = "--secrets" ]; then
  ssh "${SSH_OPTS[@]}" "$HOST" 'umask 027; cat > /etc/mylife/secrets.env.new && chown root:mylife /etc/mylife/secrets.env.new && chmod 640 /etc/mylife/secrets.env.new && mv /etc/mylife/secrets.env.new /etc/mylife/secrets.env' < .secrets/mylife.env
fi

ssh "${SSH_OPTS[@]}" "$HOST" "set -e
(cd $STAGE && install -m 644 $SERVER_FILES /opt/mylife/app/)
rm -rf /opt/mylife/app/personas /opt/mylife/app/prompts
cp -r $STAGE/content/personas $STAGE/content/prompts /opt/mylife/app/
chmod -R a+rX /opt/mylife/app/personas /opt/mylife/app/prompts
install -m 644 $STAGE/mylife-prototype.html /opt/mylife/www/index.html
install -m 644 $STAGE/mylife.service /etc/systemd/system/mylife.service
install -m 644 $STAGE/nginx-mylife.conf /etc/nginx/sites-available/mylife.conf
ln -sfn /etc/nginx/sites-available/mylife.conf /etc/nginx/sites-enabled/mylife.conf
systemctl daemon-reload
systemctl enable --quiet mylife.service
systemctl restart mylife.service
nginx -t
systemctl reload nginx
sleep 1
systemctl is-active mylife.service
curl -fsS http://127.0.0.1:18960/api/health; echo
rm -rf $STAGE"
echo "Deployed: https://mylife.instmarket.com.au:1443/"
