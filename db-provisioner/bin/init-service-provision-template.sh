#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SELF_DIR}/.." && pwd)"
TEMPLATE_DIR="${ROOT_DIR}/templates/db-access-bootstrap-template"

TARGET_DIR=""
SERVICE_NAME=""
NAMESPACE=""
PG_DB_NAME=""
PG_DB_USER=""
PG_DB_PASSWORD=""
MONGO_DB_NAME=""
MONGO_DB_USER=""
MONGO_DB_PASSWORD=""

usage() {
  cat <<'EOF'
init-service-provision-template.sh - scaffold db-access-bootstrap for one service

Usage:
  init-service-provision-template.sh \
    --target-dir /home/zym/app/your-service \
    --service-name your-service \
    --namespace your-k8s-namespace \
    --pg-db-name your_db \
    --pg-db-user your_user \
    --pg-db-password your_password

Optional mongo placeholders:
  --mongo-db-name xxx --mongo-db-user xxx --mongo-db-password xxx
EOF
}

die() {
  printf '[init-template][error] %s\n' "$*" >&2
  exit 1
}

require_non_empty() {
  [[ -n "${2:-}" ]] || die "Missing required arg: $1"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --target-dir) TARGET_DIR="${2:-}"; shift 2 ;;
      --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
      --namespace) NAMESPACE="${2:-}"; shift 2 ;;
      --pg-db-name) PG_DB_NAME="${2:-}"; shift 2 ;;
      --pg-db-user) PG_DB_USER="${2:-}"; shift 2 ;;
      --pg-db-password) PG_DB_PASSWORD="${2:-}"; shift 2 ;;
      --mongo-db-name) MONGO_DB_NAME="${2:-}"; shift 2 ;;
      --mongo-db-user) MONGO_DB_USER="${2:-}"; shift 2 ;;
      --mongo-db-password) MONGO_DB_PASSWORD="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown arg: $1" ;;
    esac
  done
}

replace_tokens() {
  SERVICE_DIR="$TARGET_DIR" SERVICE_NAME="$SERVICE_NAME" NAMESPACE="${NAMESPACE:-default}" \
    PG_DB_NAME="$PG_DB_NAME" PG_DB_USER="$PG_DB_USER" PG_DB_PASSWORD="$PG_DB_PASSWORD" \
    MONGO_DB_NAME="${MONGO_DB_NAME:-${SERVICE_NAME}_db}" MONGO_DB_USER="${MONGO_DB_USER:-${SERVICE_NAME}}" \
    MONGO_DB_PASSWORD="${MONGO_DB_PASSWORD:-change_me}" python3 - "$1" <<'PYTHON'
import os
import re
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
keys = "SERVICE_DIR SERVICE_NAME NAMESPACE PG_DB_NAME PG_DB_USER PG_DB_PASSWORD MONGO_DB_NAME MONGO_DB_USER MONGO_DB_PASSWORD".split()
values = {f"__{key}__": os.environ[key] for key in keys}
pattern = re.compile("|".join(values))
lines = []
for line in path.read_text().splitlines(keepends=True):
    if path.suffix == ".env" and "=" in line and pattern.search(line):
        key, original = line.rstrip("\n").split("=", 1)
        if original in values:
            lines.append(key + "=" + shlex.quote(values[original]) + "\n")
            continue
    lines.append(pattern.sub(lambda match: values[match.group()], line))
path.write_text("".join(lines))
PYTHON
}

main() {
  parse_args "$@"
  require_non_empty "--target-dir" "${TARGET_DIR}"
  require_non_empty "--service-name" "${SERVICE_NAME}"
  require_non_empty "--pg-db-name" "${PG_DB_NAME}"
  require_non_empty "--pg-db-user" "${PG_DB_USER}"
  require_non_empty "--pg-db-password" "${PG_DB_PASSWORD}"

  [[ -d "${TARGET_DIR}" ]] || die "Target dir not found: ${TARGET_DIR}"
  [[ -d "${TEMPLATE_DIR}" ]] || die "Template dir not found: ${TEMPLATE_DIR}"

  local out="${TARGET_DIR}/db-access-bootstrap"
  [[ ! -e "$out" && ! -L "$out" ]] || die "Bootstrap already exists: $out"
  umask 077
  stage="$(mktemp -d -- "${TARGET_DIR}/.db-access-bootstrap.XXXXXX")"
  trap 'rm -rf -- "$stage"' EXIT
  cp -R -- "${TEMPLATE_DIR}/." "$stage/"

  while IFS= read -r -d '' file; do
    replace_tokens "$file"
  done < <(find "${stage}" -type f -print0)

  chmod +x "$stage"/{setup,teardown}-{external,k8s}-db-access.sh
  mv -Tn -- "$stage" "$out"
  [[ ! -d "$stage" ]] || die "Bootstrap already exists: $out"
  trap - EXIT

  printf '[init-template] created: %s\n' "${out}"
  printf '[init-template] next step: cd "%s" && ./db-access-bootstrap/setup-external-db-access.sh\n' "${TARGET_DIR}"
}

main "$@"
