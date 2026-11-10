#!/usr/bin/env bash
# One-time bootstrap for CentOS Stream 9. Safe to re-run.
#
#   sudo bash install.sh git@github.com:<owner>/<repo>.git
#
# Installs Python 3.12, PostgreSQL 16 and Grafana, clones the repo to /opt/nms with a
# read-only deploy key, and enables:
#   nms.service        collectors + REST API (127.0.0.1:8080)
#   nms-update.timer   pulls origin/main every 2 minutes and restarts on change
set -euo pipefail

REPO_URL="${1:-}"
BRANCH="${NMS_BRANCH:-main}"
APP_DIR=/opt/nms
ETC_DIR=/etc/nms

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -n "$REPO_URL" ]] || { echo "usage: $0 git@github.com:<owner>/<repo>.git"; exit 1; }

echo "==> packages"
dnf -y module enable postgresql:16
dnf -y install git python3.12 python3.12-pip postgresql-server openssl curl

if [[ ! -f /etc/yum.repos.d/grafana.repo ]]; then
cat > /etc/yum.repos.d/grafana.repo <<'EOF'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
EOF
fi
dnf -y install grafana

echo "==> service account"
id nms &>/dev/null || useradd --system --create-home --home-dir /var/lib/nms --shell /sbin/nologin nms
install -d -o root -g nms -m 750 "$ETC_DIR"

echo "==> PostgreSQL"
[[ -f /var/lib/pgsql/data/PG_VERSION ]] || postgresql-setup --initdb
HBA=/var/lib/pgsql/data/pg_hba.conf
HBA_LINE="host    nms    nms_grafana    127.0.0.1/32    scram-sha-256"
grep -qF "nms_grafana" "$HBA" || sed -i "1i $HBA_LINE" "$HBA"
systemctl enable --now postgresql
systemctl reload postgresql

pg() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qtA "$@"; }
[[ "$(pg -c "SELECT 1 FROM pg_roles WHERE rolname='nms'")" == 1 ]] || pg -c "CREATE ROLE nms LOGIN"
[[ "$(pg -c "SELECT 1 FROM pg_database WHERE datname='nms'")" == 1 ]] || pg -c "CREATE DATABASE nms OWNER nms"

# Read-only role for Grafana. The password is kept in $ETC_DIR so re-runs reuse it.
GRAFANA_PW_FILE="$ETC_DIR/grafana_db_password"
if [[ ! -s "$GRAFANA_PW_FILE" ]]; then
    (umask 077; openssl rand -hex 24 > "$GRAFANA_PW_FILE")
fi
GRAFANA_PW="$(cat "$GRAFANA_PW_FILE")"
[[ "$(pg -c "SELECT 1 FROM pg_roles WHERE rolname='nms_grafana'")" == 1 ]] || pg -c "CREATE ROLE nms_grafana LOGIN"
pg -c "ALTER ROLE nms_grafana PASSWORD '$GRAFANA_PW'"
pg -d nms -c "GRANT CONNECT ON DATABASE nms TO nms_grafana"
pg -d nms -c "GRANT USAGE ON SCHEMA public TO nms_grafana"
pg -d nms -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO nms_grafana"
pg -d nms -c "ALTER DEFAULT PRIVILEGES FOR ROLE nms IN SCHEMA public GRANT SELECT ON TABLES TO nms_grafana"

echo "==> deploy key"
KEY="$ETC_DIR/deploy_key"
if [[ ! -f "$KEY" ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "nms-deploy@$(hostname -s)" -f "$KEY"
fi
# also covers a key created by hand before the first run (see README)
chown nms:nms "$KEY" "$KEY.pub"
chmod 600 "$KEY"
touch "$ETC_DIR/known_hosts" && chown nms:nms "$ETC_DIR/known_hosts"
SSH_CMD="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$ETC_DIR/known_hosts"

if [[ ! -d "$APP_DIR/.git" ]]; then
    echo
    echo "Add this as a READ-ONLY deploy key on the GitHub repo"
    echo "(repo -> Settings -> Deploy keys -> Add deploy key; leave 'Allow write access' off):"
    echo
    cat "$KEY.pub"
    echo
    read -r -p "Press Enter once the key is added... "
    install -d -o nms -g nms -m 755 "$APP_DIR"
    runuser -u nms -- env GIT_SSH_COMMAND="$SSH_CMD" git clone -q -b "$BRANCH" "$REPO_URL" "$APP_DIR"
    runuser -u nms -- git -C "$APP_DIR" config core.sshCommand "$SSH_CMD"
fi

echo "==> application"
[[ -x "$APP_DIR/.venv/bin/python" ]] || runuser -u nms -- python3.12 -m venv "$APP_DIR/.venv"
runuser -u nms -- "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
runuser -u nms -- "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

if [[ ! -f "$ETC_DIR/nms.env" ]]; then
    install -o root -g nms -m 640 "$APP_DIR/.env.example" "$ETC_DIR/nms.env"
fi

install -m 644 "$APP_DIR/deploy/nms.service" "$APP_DIR/deploy/nms-update.service" \
    "$APP_DIR/deploy/nms-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nms.service nms-update.timer
ln -sfn "$APP_DIR/deploy/nmsupdate" /usr/bin/nmsupdate
ln -sfn "$APP_DIR/deploy/nms" /usr/bin/nms

echo "==> Grafana"
cat > /etc/grafana/provisioning/datasources/nms.yaml <<EOF
apiVersion: 1
datasources:
  - name: NMS
    uid: nms-pg
    type: postgres
    access: proxy
    url: 127.0.0.1:5432
    user: nms_grafana
    isDefault: true
    jsonData:
      database: nms
      sslmode: disable
      postgresVersion: 1600
    secureJsonData:
      password: "$GRAFANA_PW"
EOF
chown root:grafana /etc/grafana/provisioning/datasources/nms.yaml
chmod 640 /etc/grafana/provisioning/datasources/nms.yaml
cat > /etc/grafana/provisioning/dashboards/nms.yaml <<EOF
apiVersion: 1
providers:
  - name: NMS
    folder: NMS
    type: file
    allowUiUpdates: false
    updateIntervalSeconds: 60
    options:
      path: $APP_DIR/grafana/dashboards
EOF
systemctl enable grafana-server
systemctl restart grafana-server

if systemctl is-active -q firewalld; then
    firewall-cmd -q --permanent --add-port=3000/tcp
    firewall-cmd -q --reload
fi

cat <<EOF

Done. To pull and deploy the latest code at any time, run:  nmsupdate

Next:
  1. Edit $ETC_DIR/nms.env  -> set NMS_UNIFI_URL and NMS_UNIFI_API_KEY
  2. Check the console answers:
       nms check-unifi
  3. sudo systemctl restart nms && journalctl -u nms -f
  4. Grafana: http://$(hostname -I | awk '{print $1}'):3000  (first login admin / admin, then change it)
EOF
