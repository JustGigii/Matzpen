#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_ROOT="/www/wwwroot/Mazpen"
UNIT_SOURCE="$DEPLOY_ROOT/deploy/matzpen.service"
UNIT_TARGET="/etc/systemd/system/matzpen.service"
PID_FILE="$DEPLOY_ROOT/src/gunicorn.pid"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this bootstrap as root" >&2
  exit 2
fi
if [[ ! -f "$UNIT_SOURCE" ]] || [[ ! -f "$DEPLOY_ROOT/src/gunicorn_conf.py" ]]; then
  echo "Matzpen service files are missing" >&2
  exit 2
fi

# Telegram long polling and the scheduler require one application worker.
if grep -Eq '^workers = [0-9]+$' "$DEPLOY_ROOT/src/gunicorn_conf.py"; then
  sed -i -E 's/^workers = [0-9]+$/workers = 1/' "$DEPLOY_ROOT/src/gunicorn_conf.py"
else
  echo "Gunicorn worker setting was not recognized" >&2
  exit 2
fi

install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
systemctl daemon-reload

# Stop the legacy aaPanel-launched master before systemd starts the single managed instance.
if [[ -f "$PID_FILE" ]]; then
  LEGACY_PID="$(cat "$PID_FILE")"
  if kill -0 "$LEGACY_PID" 2>/dev/null; then
    kill -TERM "$LEGACY_PID"
    for _ in $(seq 1 30); do
      kill -0 "$LEGACY_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$LEGACY_PID" 2>/dev/null; then
      echo "Legacy Gunicorn process did not stop cleanly" >&2
      exit 3
    fi
  fi
fi

systemctl enable --now matzpen.service

for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8080/health/live >/dev/null; then
    break
  fi
  sleep 2
done

systemctl is-active --quiet matzpen.service
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/health/live >/dev/null
curl --fail --silent --show-error --max-time 8 http://127.0.0.1:8080/health/ready >/dev/null

MASTER_PID="$(systemctl show --property=MainPID --value matzpen.service)"
WORKER_COUNT="$(pgrep -P "$MASTER_PID" | wc -l | tr -d ' ')"
if [[ "$WORKER_COUNT" != "1" ]]; then
  echo "Expected one Gunicorn worker, found $WORKER_COUNT" >&2
  exit 4
fi

echo "matzpen.service is active with one worker"
