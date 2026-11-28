#!/usr/bin/env bash
# Usage: ./merge-and-generate-app-env.sh [external|k8s|merge-only]
set -euo pipefail
BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BOOTSTRAP_DIR}/config"
MODE="${1:-external}"
# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"
# external 模式用 .env.local.db（NodePort 地址）；k8s 模式用 .env.local.k8s.db（集群内 Service DNS）
case "${MODE}" in
  k8s) OUT="${K8S_OUT_ENV:-${BOOTSTRAP_DIR}/.env.local.k8s.db}" ;;
  *)   OUT="${OUT_ENV}" ;;
esac
APP="${BOOTSTRAP_DIR}/../app/.env"
REF="${BOOTSTRAP_DIR}/.env.reference"
usage() {
  printf 'Usage: %s [external|k8s|merge-only]\n' "$(basename "$0")" >&2
  printf '  external (default) — setup-external-db-access.sh + merge + .env.reference\n' >&2
  printf '  k8s — setup-k8s-db-access.sh + write .env.local.k8s.db + merge\n' >&2
  printf '  merge-only — merge + .env.reference only, no provision\n' >&2
  exit 1
}
case "${MODE}" in
  external) "${BOOTSTRAP_DIR}/setup-external-db-access.sh" ;;
  k8s) "${BOOTSTRAP_DIR}/setup-k8s-db-access.sh" ;;
  merge-only) : ;;
  -h|--help|help) usage ;;
  *) usage ;;
esac
[[ -f "${OUT}" ]] || { echo "[merge-and-generate-app-env] missing OUT: ${OUT}" >&2; exit 1; }
[[ -f "${APP}" ]] || { echo "[merge-and-generate-app-env] missing ${APP}" >&2; exit 1; }
umask 077
python3 - "${OUT}" "${APP}" "${REF}" "${MODE}" <<'PYTHON'
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

source, app, reference = map(Path, sys.argv[1:4])
keys = {"DATABASE_URL", "MONGODB_URI", "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", "REDIS_USER", "REDIS_URI"}
incoming = {}
for line in source.read_text().splitlines():
    key, equal, value = line.partition("=")
    if equal and key in keys | {"APP_ENV"}:
        incoming[key] = value

def write_private(path, lines):
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temp:
        try:
            temp.write("\n".join(lines) + "\n")
            temp.flush()
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)

merged = []
written = set()
for line in app.read_text().splitlines():
    key, equal, _ = line.partition("=")
    if equal and key in keys and key in incoming:
        if key not in written:
            merged.append(f"{key}={incoming[key]}")
            written.add(key)
    elif not (equal and key == "REDIS_USER"):
        merged.append(line)
merged.extend(f"{k}={v}" for k,v in incoming.items() if k in keys and k not in written)
header = [f"# merge-and-generate-app-env.sh mode={sys.argv[4]}",
          f"# Source: {source} UTC: {datetime.now(timezone.utc).isoformat()}", "# May contain secrets.", ""]
write_private(reference, header + [f"{k}={v}" for k,v in incoming.items()])
write_private(app, merged)
PYTHON
printf '[merge-and-generate-app-env] merged -> %s; wrote %s\n' "${APP}" "${REF}"
