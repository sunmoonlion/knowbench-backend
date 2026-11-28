#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config/common.env"

die() {
  printf '[search-access-bootstrap][backend][error] %s\n' "$*" >&2
  exit 1
}

main() {
  local action="${1:-}"
  [[ -f "$CONFIG_FILE" ]] || die "Missing config: $CONFIG_FILE"
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  case "$action" in
    validate|provision|status|rotate|revoke) ;;
    *) die "Usage: $0 <validate|provision|status|rotate|revoke>" ;;
  esac
  [[ -f "$ELASTICSEARCH_DECLARATION" ]] || die "Missing declaration"
  [[ -x "$ELASTICSEARCH_PROVISIONER_BIN" ]] || die "Provisioner is not executable"
  if [[ "$action" != "validate" && "$action" != "revoke" && "${ENABLE_SEARCH_ACCESS:-false}" != "true" ]]; then
    die "Search access is disabled until this Backend owns a search dataset"
  fi
  "$ELASTICSEARCH_PROVISIONER_BIN" --cluster "$ELASTICSEARCH_CLUSTER" \
    "$action" "$ELASTICSEARCH_DECLARATION"
}

main "$@"
