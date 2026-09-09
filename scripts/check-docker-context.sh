#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

secret_fixture=".env.TRAN-29-context-test"
frontend_secret_fixture="frontend/.env.TRAN-29-context-test"
root_envrc_fixture=".envrc"
root_envrc_local_fixture=".envrc.local"
frontend_envrc_fixture="frontend/.envrc"
frontend_envrc_local_fixture="frontend/.envrc.local"
public_env_example_fixture=".env.example"
frontend_env_example_fixture="frontend/.env.example"
caption_fixture_dir="captions/TRAN-29-context-test"
caption_upload_fixture="$caption_fixture_dir/upload.video"
caption_subtitle_fixture="$caption_fixture_dir/generated.srt"
transcript_fixture="transcripts/TRAN-29-context-test.jsonl"
audio_fixture="transcripts/TRAN-29-context-test.wav"
excluded_fixtures=(
  "$secret_fixture"
  "$frontend_secret_fixture"
  "$root_envrc_fixture"
  "$root_envrc_local_fixture"
  "$frontend_envrc_fixture"
  "$frontend_envrc_local_fixture"
  "$caption_upload_fixture"
  "$caption_subtitle_fixture"
  "$transcript_fixture"
  "$audio_fixture"
)
included_fixtures=(
  "$public_env_example_fixture"
  "$frontend_env_example_fixture"
)
file_fixtures=(
  "$secret_fixture"
  "$frontend_secret_fixture"
  "$root_envrc_fixture"
  "$root_envrc_local_fixture"
  "$frontend_envrc_fixture"
  "$frontend_envrc_local_fixture"
  "$transcript_fixture"
  "$audio_fixture"
)
export_dir="$(mktemp -d "${TMPDIR:-/tmp}/translator-docker-context.XXXXXX")"
fixtures_created=0
caption_root_created=0
transcripts_root_created=0
public_env_example_created=0
frontend_env_example_created=0

cleanup() {
  if ((fixtures_created)); then
    rm -f -- "${file_fixtures[@]}"
    rm -rf -- "$caption_fixture_dir"
    if ((caption_root_created)); then
      rmdir -- captions 2>/dev/null || true
    fi
    if ((transcripts_root_created)); then
      rmdir -- transcripts 2>/dev/null || true
    fi
  fi
  if ((public_env_example_created)); then
    rm -f -- "$public_env_example_fixture"
  fi
  if ((frontend_env_example_created)); then
    rm -f -- "$frontend_env_example_fixture"
  fi
  rm -rf -- "$export_dir"
}
trap cleanup EXIT

if [[ -e "$caption_fixture_dir" || -L "$caption_fixture_dir" ]]; then
  echo "Refusing to overwrite existing context-test directory: $caption_fixture_dir" >&2
  exit 1
fi

for fixture in "${excluded_fixtures[@]}"; do
  if [[ -e "$fixture" || -L "$fixture" ]]; then
    echo "Refusing to overwrite existing context-test fixture: $fixture" >&2
    exit 1
  fi
done

for fixture in "${included_fixtures[@]}"; do
  if [[ -e "$fixture" || -L "$fixture" ]]; then
    if [[ ! -f "$fixture" || -L "$fixture" ]]; then
      echo "Public environment template must be a regular file: $fixture" >&2
      exit 1
    fi
  fi
done

if [[ ! -d captions ]]; then
  caption_root_created=1
fi
if [[ ! -d transcripts ]]; then
  transcripts_root_created=1
fi
fixtures_created=1
mkdir -p -- "$caption_fixture_dir" transcripts
printf 'TRANSLATOR_TEST_SECRET=not-a-real-secret\n' > "$secret_fixture"
printf 'VITE_TEST_SECRET=not-a-real-secret\n' > "$frontend_secret_fixture"
printf 'export TRANSLATOR_TEST_SECRET=not-a-real-secret\n' > "$root_envrc_fixture"
printf 'export TRANSLATOR_TEST_SECRET=not-a-real-secret\n' > "$root_envrc_local_fixture"
printf 'export VITE_TEST_SECRET=not-a-real-secret\n' > "$frontend_envrc_fixture"
printf 'export VITE_TEST_SECRET=not-a-real-secret\n' > "$frontend_envrc_local_fixture"
if [[ ! -e "$public_env_example_fixture" ]]; then
  printf 'TRANSLATOR_PUBLIC_EXAMPLE=example\n' > "$public_env_example_fixture"
  public_env_example_created=1
fi
if [[ ! -e "$frontend_env_example_fixture" ]]; then
  printf 'VITE_PUBLIC_EXAMPLE=example\n' > "$frontend_env_example_fixture"
  frontend_env_example_created=1
fi
printf 'fake upload\n' > "$caption_upload_fixture"
printf '1\n00:00:00,000 --> 00:00:01,000\nfake subtitle\n' > "$caption_subtitle_fixture"
printf '{"text":"fake transcript"}\n' > "$transcript_fixture"
printf 'fake audio\n' > "$audio_fixture"

failed=0

for fixture in "${excluded_fixtures[@]}"; do
  if ! git check-ignore --quiet -- "$fixture"; then
    echo "Git would include runtime fixture: $fixture" >&2
    failed=1
  fi
done

for fixture in "${included_fixtures[@]}"; do
  if git check-ignore --quiet -- "$fixture"; then
    echo "Git would exclude public environment template: $fixture" >&2
    failed=1
  fi
done

docker buildx build \
  --file tests/docker-context.Dockerfile \
  --output "type=local,dest=$export_dir" \
  .

for fixture in "${excluded_fixtures[@]}"; do
  if [[ -e "$export_dir/$fixture" ]]; then
    echo "Docker context includes runtime fixture: $fixture" >&2
    failed=1
  fi
done

for fixture in "${included_fixtures[@]}"; do
  if [[ ! -f "$export_dir/$fixture" ]]; then
    echo "Docker context omitted public environment template: $fixture" >&2
    failed=1
  fi
done

required_files=(
  app.py
  Dockerfile
  frontend/package.json
  templates/index.html
  translator_runtime.py
)

for required_file in "${required_files[@]}"; do
  if [[ ! -f "$export_dir/$required_file" ]]; then
    echo "Docker context omitted required application file: $required_file" >&2
    failed=1
  fi
done

if ((failed)); then
  exit 1
fi

echo "Docker context excludes local secrets and runtime media."
