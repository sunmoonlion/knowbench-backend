#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/build.conf"
source "$script_dir/release-tag.sh"
tag="${BACKEND_TAG:-architecture-v2-dev}"
if (( $# )); then
    if [[ $# != 2 || $1 != --tag || -z $2 ]]; then
        echo "Usage: $0 [--tag TAG]" >&2
        exit 2
    fi
    tag=$2
fi
require_development_tag "$tag"
if [[ ! $tag =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "Invalid image tag: $tag" >&2
    exit 2
fi

container=knowbench-backend
image="$container:$tag"
cd "$script_dir/.."
docker build -t "$image" -f mybuild/Dockerfile .
docker stop "$container" >/dev/null 2>&1 || true
docker rm "$container" >/dev/null 2>&1 || true
docker run --rm -d -p 8000:8000 --name "$container" "$image"
