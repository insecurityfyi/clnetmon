#!/usr/bin/env bash
# Fast-forward /opt/nms to origin/<branch> and, if something changed, reinstall and
# restart. Run every 2 minutes by nms-update.timer, or by hand with the "nmsupdate"
# command. Database migrations run when the app starts.
#
#   nmsupdate           update if there is new code
#   nmsupdate --force   reinstall and restart even if nothing changed
set -euo pipefail

APP_DIR=/opt/nms
BRANCH="${NMS_BRANCH:-main}"
g() { runuser -u nms -- git -C "$APP_DIR" "$@"; }

# Everything lives in main() so bash has read the whole file before git replaces it.
main() {
    local force=0 old new changed=0 u
    [[ "${1:-}" == "--force" ]] && force=1

    g fetch -q origin "$BRANCH"
    old="$(g rev-parse HEAD)"
    new="$(g rev-parse "origin/$BRANCH")"
    if [[ "$old" == "$new" && $force -eq 0 ]]; then
        [[ -t 1 ]] && echo "already up to date at ${old:0:8}"
        return 0
    fi

    if [[ "$old" != "$new" ]]; then
        echo "updating ${old:0:8} -> ${new:0:8}"
        g merge -q --ff-only "origin/$BRANCH"
    else
        echo "reinstalling ${old:0:8}"
    fi
    runuser -u nms -- "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

    # pick up changed unit files and keep the nmsupdate command in place
    for u in nms.service nms-update.service nms-update.timer; do
        if ! cmp -s "$APP_DIR/deploy/$u" "/etc/systemd/system/$u"; then
            install -m 644 "$APP_DIR/deploy/$u" "/etc/systemd/system/$u"
            changed=1
        fi
    done
    [[ $changed -eq 1 ]] && systemctl daemon-reload
    ln -sfn "$APP_DIR/deploy/nmsupdate" /usr/local/bin/nmsupdate

    systemctl restart nms.service
    for _ in $(seq 1 15); do
        sleep 2
        if curl -fsS -m 3 http://127.0.0.1:8080/health >/dev/null 2>&1; then
            echo "healthy at ${new:0:8}"
            return 0
        fi
    done
    echo "nms did not become healthy at ${new:0:8}; see: journalctl -u nms" >&2
    return 1
}

main "$@"
exit $?
