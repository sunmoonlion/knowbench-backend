#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_CONFIG:-${CONFIG_DIR}/postgresql.external.env}"
REDIS_CONFIG="${REDIS_CONFIG:-${CONFIG_DIR}/redis.external.env}"
MONGO_CONFIG="${MONGO_CONFIG:-${CONFIG_DIR}/mongodb.external.env}"

log() {
  printf '[db-provision-template] %s\n' "$*"
}

die() {
  printf '[db-provision-template][error] %s\n' "$*" >&2
  exit 1
}

bool_true() {
  case "${1:-false}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

require_file() {
  [[ -f "$1" ]] || die "Missing file: $1"
}

source "${BOOTSTRAP_DIR}/env-values.sh"

run_postgresql() {
  require_file "${PG_CONFIG}"
  "${DBCTL_BIN}" --config "${PG_CONFIG}" --target external --action provision

  # shellcheck disable=SC1090
  source "${PG_CONFIG}"
  export PGPASSWORD="${APP_DB_PASSWORD}"
  psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${APP_DB_USER}" -d "${APP_DB_NAME}" -c "select current_user, current_database();" >/dev/null
  replace_env_value "${OUT_ENV}" "DATABASE_URL" "$(db_uri postgresql "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')"
}

run_mongodb() {
  require_file "${MONGO_CONFIG}"
  "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target external --action provision

  # shellcheck disable=SC1090
  source "${MONGO_CONFIG}"
  if command -v mongosh >/dev/null 2>&1; then
    mongosh "$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')" --quiet --eval "db.adminCommand({ ping: 1 }).ok" >/dev/null
  fi
  replace_env_value "${OUT_ENV}" "MONGODB_URI" "$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')"
}

run_redis() {
  require_file "${REDIS_CONFIG}"
  # shellcheck disable=SC1090
  source "${REDIS_CONFIG}"
  REDIS_HOST="$REDIS_HOST" REDIS_PORT="$REDIS_PORT" REDIS_PASSWORD="$REDIS_PASSWORD" python3 - <<'PY'
import os
import socket
host=os.environ['REDIS_HOST']
port=int(os.environ['REDIS_PORT'])
pwd=os.environ['REDIS_PASSWORD']
def resp(*parts):
    s='*%d\r\n'%len(parts)
    for p in parts:
        p=str(p)
        s += '$%d\r\n%s\r\n'%(len(p.encode()),p)
    return s.encode()
with socket.create_connection((host,port),timeout=5) as c:
    c.sendall(resp('AUTH',pwd)); c.recv(256)
    c.sendall(resp('PING')); ans=c.recv(256).decode(errors='ignore')
    if '+PONG' not in ans:
        raise SystemExit('Redis ping failed: ' + ans)
PY
  replace_env_value "${OUT_ENV}" "REDIS_HOST" "${REDIS_HOST}"
  replace_env_value "${OUT_ENV}" "REDIS_PORT" "${REDIS_PORT}"
  replace_env_value "${OUT_ENV}" "REDIS_DB" "${REDIS_DB}"
  replace_env_value "${OUT_ENV}" "REDIS_PASSWORD" "${REDIS_PASSWORD}"
}

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"
  bool_true "${STRICT_FILE_CHECK:-true}" && require_file "${BASE_ENV_TEMPLATE}"

  local destination="${OUT_ENV}"
  umask 077
  OUT_ENV="$(mktemp -- "${destination}.tmp.XXXXXX")"
  trap 'rm -f -- "${OUT_ENV}"' EXIT
  cat -- "${BASE_ENV_TEMPLATE}" > "${OUT_ENV}"

  if bool_true "${ENABLE_POSTGRESQL:-false}"; then run_postgresql; fi
  if bool_true "${ENABLE_MONGODB:-false}"; then run_mongodb; fi
  if bool_true "${ENABLE_REDIS:-false}"; then run_redis; fi

  mv -fT -- "${OUT_ENV}" "${destination}"
  trap - EXIT
  log "Provision complete. env ready: ${destination}"
}

main "$@"
