#!/usr/bin/env bash
# Run by nms-update.timer as root: fast-forward /opt/nms to origin/<branch> and, only if
# something changed, reinstall and restart. Database migrations run when the app starts.
set -euo pipefail

APP_DIR=/opt/nms
BRANCH="${NMS_BRANCH:-main}"
g() { runuser -u nms -- git -C "$APP_DIR" "$@"; }

g fetch -q origin "$BRANCH"
OLD="$(g rev-parse HEAD)"
NEW="$(g rev-parse "origin/$BRANCH")"
[[ "$OLD" == "$NEW" ]] && exit 0

echo "updating ${OLD:0:8} -> ${NEW:0:8}"
g merge -q --ff-only "origin/$BRANCH"
runuser -u nms -- "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

# pick up changed unit files
changed=0
for u in nms.service nms-update.service nms-update.timer; do
    if ! cmp -s "$APP_DIR/deploy/$u" "/etc/systemd/system/$u"; then
        install -m 644 "$APP_DIR/deploy/$u" "/etc/systemd/system/$u"
        changed=1
    fi
done
[[ $changed -eq 1 ]] && systemctl daemon-reload

systemctl restart nms.service
for _ in $(seq 1 15); do
    sleep 2
    if curl -fsS -m 3 http://127.0.0.1:8080/health >/dev/null 2>&1; then
        echo "healthy at ${NEW:0:8}"
        exit 0
    fi
done
echo "nms did not become healthy after update to ${NEW:0:8}; see: journalctl -u nms" >&2
exit 1
