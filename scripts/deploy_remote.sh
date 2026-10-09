#!/usr/bin/env bash
set -Eeuo pipefail

# Production paths are intentionally explicit. Override only for a controlled migration.
DEPLOY_ROOT="${MATZPEN_DEPLOY_ROOT:-/www/wwwroot/Mazpen}"
PYTHON_BIN="${MATZPEN_PYTHON_BIN:-/www/server/pyproject_env/Mazen/bin/python3.12}"
PID_FILE="${MATZPEN_PID_FILE:-$DEPLOY_ROOT/src/gunicorn.pid}"
LIVE_URL="${MATZPEN_LIVE_URL:-http://127.0.0.1:8080/health/live}"
READY_URL="${MATZPEN_READY_URL:-http://127.0.0.1:8080/health/ready}"
BACKUP_ROOT="${MATZPEN_BACKUP_ROOT:-/www/backup/Mazpen}"

ARCHIVE="${1:-}"
REVISION="${2:-}"

if [[ "$DEPLOY_ROOT" != "/www/wwwroot/Mazpen" ]]; then
  echo "Refusing unexpected deployment root: $DEPLOY_ROOT" >&2
  exit 2
fi
if [[ ! "$ARCHIVE" =~ ^/tmp/matzpen-[0-9a-f]{40}\.tar\.gz$ ]] || [[ ! -f "$ARCHIVE" ]]; then
  echo "Invalid or missing deployment archive" >&2
  exit 2
fi
if [[ ! "$REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid revision" >&2
  exit 2
fi
if [[ ! -x "$PYTHON_BIN" ]] || [[ ! -f "$PID_FILE" ]]; then
  echo "Production Python or Gunicorn pid file is missing" >&2
  exit 2
fi

install -d -m 700 "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/deploy.lock"
if ! flock -n 9; then
  echo "Another deployment is already running" >&2
  exit 3
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING="$(mktemp -d /tmp/matzpen-deploy.XXXXXX)"
BACKUP="$BACKUP_ROOT/$STAMP-$REVISION"
RELEASE="$STAGING/matzpen"
RESTARTED=0

cleanup() {
  rm -rf -- "$STAGING"
  rm -f -- "$ARCHIVE" /tmp/deploy_remote.sh
}

rollback() {
  local exit_code=$?
  trap - ERR
  echo "Deployment failed; restoring $BACKUP" >&2
  if [[ -f "$BACKUP/application.tar.gz" ]]; then
    tar -xzf "$BACKUP/application.tar.gz" -C "$DEPLOY_ROOT"
  fi
  if [[ -f "$BACKUP/gunicorn_conf.py" ]]; then
    install -o www -g www -m 0644 "$BACKUP/gunicorn_conf.py" "$DEPLOY_ROOT/src/gunicorn_conf.py"
  fi
  if [[ "$RESTARTED" -eq 1 ]]; then
    systemctl restart matzpen.service || true
  fi
  cleanup
  exit "$exit_code"
}

trap rollback ERR
trap cleanup EXIT

mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
tar -xzf "$ARCHIVE" -C "$STAGING"

for required in pyproject.toml alembic.ini src/personal_agent/main.py scripts/deploy_remote.sh; do
  if [[ ! -f "$RELEASE/$required" ]]; then
    echo "Release is missing $required" >&2
    false
  fi
done

"$PYTHON_BIN" -m compileall -q "$RELEASE/src/personal_agent"

# Preserve the complete application tree but never copy runtime secrets or databases off-host.
tar -czf "$BACKUP/application.tar.gz" \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='*.sqlite' \
  --exclude='*.sqlite3' \
  --exclude='data' \
  --exclude='logs' \
  --exclude='__pycache__' \
  -C "$DEPLOY_ROOT" .
cp -a "$DEPLOY_ROOT/src/gunicorn_conf.py" "$BACKUP/gunicorn_conf.py"
printf '%s\n' "$REVISION" > "$BACKUP/revision.txt"

# Install dependencies before replacing source. Existing workers keep serving until the HUP below.
"$PYTHON_BIN" -m pip install --disable-pip-version-check --quiet "$RELEASE"

rsync -a --exclude='.env' --exclude='src/gunicorn_conf.py' "$RELEASE/" "$DEPLOY_ROOT/"
chown -R root:root "$DEPLOY_ROOT/src/personal_agent" "$DEPLOY_ROOT/migrations"

cd "$DEPLOY_ROOT"
"$PYTHON_BIN" -m alembic upgrade head
"$PYTHON_BIN" -m compileall -q "$DEPLOY_ROOT/src/personal_agent"

# Telegram long polling must have exactly one process. Multiple Gunicorn workers race for updates.
if grep -Eq '^workers = [0-9]+$' "$DEPLOY_ROOT/src/gunicorn_conf.py"; then
  sed -i -E 's/^workers = [0-9]+$/workers = 1/' "$DEPLOY_ROOT/src/gunicorn_conf.py"
else
  echo "Gunicorn worker setting was not recognized" >&2
  false
fi

if ! systemctl cat matzpen.service >/dev/null 2>&1; then
  echo "matzpen.service is not installed; run the one-time bootstrap first" >&2
  false
fi

# A full restart avoids overlapping Telegram pollers and scheduler instances.
systemctl restart matzpen.service
RESTARTED=1

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 3 "$LIVE_URL" >/dev/null; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error --max-time 5 "$LIVE_URL" >/dev/null
curl --fail --silent --show-error --max-time 8 "$READY_URL" >/dev/null

systemctl is-active --quiet matzpen.service
MASTER_PID="$(systemctl show --property=MainPID --value matzpen.service)"
WORKER_COUNT="$(pgrep -P "$MASTER_PID" | wc -l | tr -d ' ')"
if [[ "$WORKER_COUNT" != "1" ]]; then
  echo "Expected one Gunicorn worker, found $WORKER_COUNT" >&2
  false
fi

printf '%s\n' "$REVISION" > "$DEPLOY_ROOT/.deployed-revision"
chown root:root "$DEPLOY_ROOT/.deployed-revision"

trap - ERR
echo "Deployment $REVISION completed successfully"
