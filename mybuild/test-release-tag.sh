#!/usr/bin/env bash
set -euo pipefail
test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$test_dir/release-tag.sh"
require_development_tag architecture-v2-dev
require_development_tag candidate-a1b2c3
for tag in 1.0.0 2.0.0; do
    for script in build-image.sh push-image.sh rebuild-and-run.sh; do
        if output="$(bash "$test_dir/$script" --tag "$tag" 2>&1)"; then
            printf 'Unexpected success: %s %s\n' "$script" "$tag" >&2
            exit 1
        fi
        [[ "$output" == *"Protected release tag: $tag"* ]]
    done
done
printf '%s\n' 'PASS: release tags rejected before registry access; development tags accepted'
