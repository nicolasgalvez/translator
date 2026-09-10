#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

git_excluded=(
  .env
  .env.local
  frontend/.env.production
  .envrc
  frontend/.envrc.local
  .idea/settings.xml
  .vscode/settings.json
  scratch.swp
  dist/bundle.js
  build/artifact
)
git_included=(
  .env.example
  frontend/.env.example
)

failed=0
for fixture in "${git_excluded[@]}"; do
  if ! git check-ignore --no-index --quiet -- "$fixture"; then
    echo "Git would include local-only file: $fixture" >&2
    failed=1
  fi
done

for fixture in "${git_included[@]}"; do
  if git check-ignore --no-index --quiet -- "$fixture"; then
    echo "Git would exclude public environment template: $fixture" >&2
    failed=1
  fi
done

if ((failed)); then
  exit 1
fi

secret_fixture=".env.TRAN-70-context-test"
frontend_secret_fixture="frontend/.env.TRAN-70-context-test"
public_env_example=".env.example"
frontend_env_example="frontend/.env.example"
export_dir="$(mktemp -d "${TMPDIR:-/tmp}/translator-public-context.XXXXXX")"
public_example_created=0
frontend_example_created=0

cleanup() {
  rm -f -- "$secret_fixture" "$frontend_secret_fixture"
  if ((public_example_created)); then
    rm -f -- "$public_env_example"
  fi
  if ((frontend_example_created)); then
    rm -f -- "$frontend_env_example"
  fi
  rm -rf -- "$export_dir"
}
trap cleanup EXIT

for fixture in "$secret_fixture" "$frontend_secret_fixture"; do
  if [[ -e "$fixture" || -L "$fixture" ]]; then
    echo "Refusing to overwrite context fixture: $fixture" >&2
    exit 1
  fi
done

if [[ ! -e "$public_env_example" ]]; then
  printf 'TRANSLATOR_PUBLIC_EXAMPLE=example\n' > "$public_env_example"
  public_example_created=1
fi
if [[ ! -e "$frontend_env_example" ]]; then
  printf 'VITE_PUBLIC_EXAMPLE=example\n' > "$frontend_env_example"
  frontend_example_created=1
fi
printf 'TRANSLATOR_TEST_SECRET=not-a-real-secret\n' > "$secret_fixture"
printf 'VITE_TEST_SECRET=not-a-real-secret\n' > "$frontend_secret_fixture"

docker buildx build \
  --file tests/docker-context.Dockerfile \
  --output "type=local,dest=$export_dir" \
  .

for fixture in "$secret_fixture" "$frontend_secret_fixture"; do
  if [[ -e "$export_dir/$fixture" ]]; then
    echo "Docker context includes environment file: $fixture" >&2
    failed=1
  fi
done

for directory in frontend/node_modules frontend/dist; do
  if [[ -e "$export_dir/$directory" ]]; then
    echo "Docker context includes generated dependency/build directory: $directory" >&2
    failed=1
  fi
done

for fixture in "$public_env_example" "$frontend_env_example"; do
  if [[ ! -f "$export_dir/$fixture" ]]; then
    echo "Docker context omitted public environment template: $fixture" >&2
    failed=1
  fi
done

for required_file in app.py Dockerfile frontend/package.json templates/index.html; do
  if [[ ! -f "$export_dir/$required_file" ]]; then
    echo "Docker context omitted application file: $required_file" >&2
    failed=1
  fi
done

if ((failed)); then
  exit 1
fi

echo "Git and Docker contexts exclude local secrets and local-only files."
