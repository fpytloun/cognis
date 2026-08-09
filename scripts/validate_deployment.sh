#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CHART="$ROOT/deploy/helm/cognis"

helm lint "$CHART"
for values in "$CHART"/examples/*.yaml; do
  helm lint "$CHART" -f "$values"
  helm template cognis "$CHART" --namespace cognis -f "$values" >/dev/null
done

uv run python "$ROOT/scripts/prepare_ha_e2e.py"
docker compose \
  --project-name cognis-ha-e2e-validate \
  --env-file "$ROOT/.local/cognis-ha-e2e/current/compose.env" \
  -f "$ROOT/compose.local.yml" \
  -f "$ROOT/compose.e2e.yml" \
  -f "$ROOT/compose.ha-e2e.yml" \
  config --quiet

uv run pytest "$ROOT/tests/deployment" -q
uv run python "$ROOT/scripts/check_docs_links.py"
