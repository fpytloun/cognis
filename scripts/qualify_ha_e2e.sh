#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/.local/cognis-ha-e2e/current/compose.env"
PROJECT_NAME=${COGNIS_HA_E2E_PROJECT:-cognis-ha-e2e}
PUBLIC_PORT=${COGNIS_HA_E2E_PORT:-18080}
PUBLIC_URL="http://localhost:$PUBLIC_PORT"

uv run python "$ROOT/scripts/prepare_ha_e2e.py"

compose() {
  docker compose \
    --project-name "$PROJECT_NAME" \
    --env-file "$ENV_FILE" \
    -f "$ROOT/compose.local.yml" \
    -f "$ROOT/compose.e2e.yml" \
    -f "$ROOT/compose.ha-e2e.yml" \
    "$@"
}

cleanup() {
  compose down --volumes --remove-orphans
}

trap cleanup EXIT INT TERM

wait_ready() {
  attempts=0
  until curl --fail --silent "$PUBLIC_URL/api/readyz" >/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 60 ]; then
      compose ps
      compose logs cognis cognis-2 cognis-lb cognis-db-upgrade
      return 1
    fi
    sleep 3
  done
}

sustained_public_api_success() {
  iteration=1
  while [ "$iteration" -le 20 ]; do
    if ! curl --fail --silent --show-error "$PUBLIC_URL/api/readyz" >/dev/null ||
       ! curl --fail --silent --show-error "$PUBLIC_URL/api/livez" >/dev/null ||
       ! curl --fail --silent --show-error "$PUBLIC_URL/.well-known/jwks.json" >/dev/null; then
      echo "Public API failed during sustained controller-2 qualification at iteration $iteration"
      compose ps
      compose logs cognis cognis-2 cognis-lb
      return 1
    fi
    iteration=$((iteration + 1))
    sleep 1
  done
}

cleanup
compose up -d --build
compose up -d --force-recreate --no-deps cognis-lb
wait_ready
compose run --rm seed-e2e
compose up -d cognis-executor cognis-executor-2
uv run python "$ROOT/scripts/verify_ha_assembled.py" \
  --project "$PROJECT_NAME" \
  --env-file "$ENV_FILE" \
  --public-url "$PUBLIC_URL"
compose stop cognis
compose exec -T cognis-2 python -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/readyz', timeout=5)"
sustained_public_api_success
compose start cognis
wait_ready
compose run --rm --entrypoint /bin/sh minio-init -ec '
  mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
  mc ls local/cognis-artifacts >/dev/null
  mc ls local/cognis-tool-outputs >/dev/null
'
echo "HA E2E qualification passed."
