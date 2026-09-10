#!/usr/bin/env bash
set -euo pipefail

base_sha=${1:?base SHA is required}
head_sha=${2:?head SHA is required}
message_file=$(mktemp)
trap 'rm -f "$message_file"' EXIT

while read -r commit_sha; do
    git log -1 --format=%B "$commit_sha" > "$message_file"
    uv run --no-sync pre-commit run conventional-pre-commit \
        --hook-stage commit-msg \
        --commit-msg-filename "$message_file"
done < <(git rev-list --reverse "$base_sha..$head_sha")
