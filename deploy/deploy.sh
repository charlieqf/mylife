#!/usr/bin/env bash
# Deploy the MyLife phase-0 demo to R760 (run from the repo root in Git Bash).
#   bash deploy/deploy.sh            # page + backend + configs
#   bash deploy/deploy.sh --secrets  # also push .secrets/mylife.env to /etc/mylife/secrets.env
# One-time prerequisites already done on the server (2026-11-21): system user `mylife`,
# /opt/mylife/{app,www}, /var/cache/mylife, /etc/mylife, and the Let's Encrypt cert
# (/var/lib/mylife is created by systemd: StateDirectory=mylife):
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
SERVER_FILES="mylife_server.py memory.py paipan.py voices.json calibrate.py"

# Pages left open on phones are told about a new deploy through /build.txt (see BUILD in the page).
BUILD=$(date -u +%Y%m%d-%H%M%S)
PAGE=$(mktemp)
trap 'rm -f "$PAGE"' EXIT
# The page's own CSS and scripts (prototype.css, scene.js, voice.js) live in a directory per build, /v/<build>/, and the
# page is switched only after they are up; the last few builds stay, so a page fetched just before a deploy still gets
# its own copies (review 2026-11-25: with one shared copy, an old page could load a new voice.js).
PAGE_FILES="prototype.css scene.js voice.js"
python tools/check_page.py || { echo "prototype.css: the forced-dark (.autodark) rules don't match the dark ones" >&2; exit 1; }
SUBST=(-e "s/^var BUILD='dev';/var BUILD='$BUILD';/")
for f in $PAGE_FILES; do SUBST+=(-e "s#\"$f?v=dev\"#\"v/$BUILD/$f\"#"); done
sed "${SUBST[@]}" prototype/mylife-prototype.html > "$PAGE"
grep -q "^var BUILD='$BUILD';" "$PAGE" || { echo "var BUILD='dev'; not found in the page" >&2; exit 1; }
for f in $PAGE_FILES; do
  grep -q "\"v/$BUILD/$f\"" "$PAGE" || { echo "\"$f?v=dev\" not found in the page" >&2; exit 1; }
done

ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
scp "${SCP_OPTS[@]}" $(printf 'server/%s ' $SERVER_FILES) deploy/nginx-mylife.conf deploy/mylife.service "$HOST:$STAGE/"
scp "${SCP_OPTS[@]}" "$PAGE" "$HOST:$STAGE/mylife-prototype.html"
scp "${SCP_OPTS[@]}" $(printf 'prototype/%s ' $PAGE_FILES) "$HOST:$STAGE/"

# Persona prompts (档案 + 风格示例 + 观点库) and everything in prompts/ (另一个你, 去AI味, 收尾一句, 彩虹卡).
# The service is restarted below, so the server picks them up (it caches them after the first read).
tar -czf - personas/*/人格档案.md personas/*/风格示例.md $(ls personas/*/观点库.json personas/*/路子.json 2>/dev/null) prompts | ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $STAGE/content && tar -xzf - -C $STAGE/content"

if [ "${1:-}" = "--secrets" ]; then
  ssh "${SSH_OPTS[@]}" "$HOST" 'umask 027; cat > /etc/mylife/secrets.env.new && chown root:mylife /etc/mylife/secrets.env.new && chmod 640 /etc/mylife/secrets.env.new && mv /etc/mylife/secrets.env.new /etc/mylife/secrets.env' < .secrets/mylife.env
fi

ssh "${SSH_OPTS[@]}" "$HOST" "set -e
(cd $STAGE && install -m 644 $SERVER_FILES /opt/mylife/app/)
rm -rf /opt/mylife/app/personas /opt/mylife/app/prompts
cp -r $STAGE/content/personas $STAGE/content/prompts /opt/mylife/app/
chmod -R a+rX /opt/mylife/app/personas /opt/mylife/app/prompts
mkdir -p /opt/mylife/www/v/$BUILD && (cd $STAGE && install -m 644 $PAGE_FILES /opt/mylife/www/v/$BUILD/)
chmod 755 /opt/mylife/www/v /opt/mylife/www/v/$BUILD
install -m 644 $STAGE/mylife-prototype.html /opt/mylife/www/index.html.new && mv /opt/mylife/www/index.html.new /opt/mylife/www/index.html
ls -1d /opt/mylife/www/v/*/ | sort -r | tail -n +6 | xargs -r rm -rf  # keep the newest five builds' files
echo $BUILD > /opt/mylife/www/build.txt.new && chmod 644 /opt/mylife/www/build.txt.new && mv /opt/mylife/www/build.txt.new /opt/mylife/www/build.txt
install -m 644 $STAGE/mylife.service /etc/systemd/system/mylife.service
install -m 644 $STAGE/nginx-mylife.conf /etc/nginx/sites-available/mylife.conf
ln -sfn /etc/nginx/sites-available/mylife.conf /etc/nginx/sites-enabled/mylife.conf
systemctl daemon-reload
# 2026-11-22: memory and logs moved from the cache dir to /var/lib/mylife (logs became the archive). One-time, idempotent.
if [ -d /var/cache/mylife/users ] && [ ! -e /var/lib/mylife/users ]; then
  systemctl stop mylife.service
  mkdir -p /var/lib/mylife/legacy
  mv /var/cache/mylife/users /var/lib/mylife/users
  for d in chatlog feedback; do [ -d /var/cache/mylife/\$d ] && mv /var/cache/mylife/\$d /var/lib/mylife/legacy/\$d; done
  chown -R mylife:mylife /var/lib/mylife
  echo 'migrated memory + old logs to /var/lib/mylife'
fi
systemctl enable --quiet mylife.service
systemctl restart mylife.service
nginx -t
systemctl reload nginx
sleep 1
systemctl is-active mylife.service
curl -fsS http://127.0.0.1:18960/api/health; echo
echo build \$(cat /opt/mylife/www/build.txt)
rm -rf $STAGE"
echo "Deployed: https://mylife.instmarket.com.au:1443/"
