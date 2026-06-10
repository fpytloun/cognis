#!/usr/bin/env bash
set -euo pipefail

DEFAULT_HOME=/home/cognis
DEFAULT_USER=cognis
DEFAULT_UID=1000
DEFAULT_GID=1000

export HOME="${HOME:-$DEFAULT_HOME}"
export COGNIS_DATA_DIR="${COGNIS_DATA_DIR:-$HOME/.cognis}"
export COGNIS_EXECUTOR_SHELL="${COGNIS_EXECUTOR_SHELL:-/bin/bash}"
export SHELL="${SHELL:-/bin/bash}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"

if [[ "${1:-}" == -* ]]; then
  set -- cognis-executor "$@"
fi

setup_nss_wrapper() {
  local uid gid wrapper_lib candidate
  uid="$(id -u)"
  gid="$(id -g)"

  if getent passwd "$uid" >/dev/null 2>&1; then
    return 0
  fi

  wrapper_lib=""
  for candidate in /usr/lib/*/libnss_wrapper.so /usr/lib/libnss_wrapper.so; do
    if [[ -r "$candidate" ]]; then
      wrapper_lib="$candidate"
      break
    fi
  done

  if [[ -z "$wrapper_lib" ]]; then
    return 0
  fi

  export NSS_WRAPPER_PASSWD=/tmp/cognis-passwd
  export NSS_WRAPPER_GROUP=/tmp/cognis-group
  printf '%s:x:%s:%s:Cognis Executor:%s:/bin/bash\n' "$DEFAULT_USER" "$uid" "$gid" "$HOME" > "$NSS_WRAPPER_PASSWD"
  if ! getent group "$gid" >/dev/null 2>&1; then
    printf '%s:x:%s:\n' "$DEFAULT_USER" "$gid" > "$NSS_WRAPPER_GROUP"
  else
    getent group "$gid" > "$NSS_WRAPPER_GROUP"
  fi
  export LD_PRELOAD="$wrapper_lib${LD_PRELOAD:+:$LD_PRELOAD}"
}

ensure_writable_home() {
  if [[ ! -d "$HOME" ]]; then
    mkdir -p "$HOME" 2>/dev/null || true
  fi

  if [[ ! -d "$HOME" || ! -w "$HOME" ]]; then
    cat >&2 <<EOF
ERROR: HOME is not writable: $HOME

Mount a writable volume at $HOME or set HOME to a writable path.
For Kubernetes, set runAsUser/runAsGroup to match the volume owner or use fsGroup.
Recommended securityContext:
  runAsNonRoot: true
  runAsUser: $DEFAULT_UID
  runAsGroup: $DEFAULT_GID
  fsGroup: $DEFAULT_GID
  fsGroupChangePolicy: OnRootMismatch
EOF
    exit 1
  fi
}

initialize_home() {
  ensure_writable_home

  mkdir -p \
    "$COGNIS_DATA_DIR/cache/browser" \
    "$COGNIS_DATA_DIR/cache/lsp" \
    "$HOME/.cache" \
    "$HOME/.config" \
    "$HOME/.local/bin" \
    "$HOME/workspace"

  if [[ ! -f "$HOME/.profile" ]]; then
    cat > "$HOME/.profile" <<'EOF'
if [ -f "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
EOF
  fi

  touch "$HOME/.bashrc"
  if ! grep -q 'Cognis executor managed environment' "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" <<'EOF'

# Cognis executor managed environment
export COGNIS_DATA_DIR="${COGNIS_DATA_DIR:-$HOME/.cognis}"
export COGNIS_EXECUTOR_SHELL="${COGNIS_EXECUTOR_SHELL:-/bin/bash}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
export SHELL="${SHELL:-/bin/bash}"
if [ -d "$HOME/workspace" ]; then
  cd "$HOME/workspace" 2>/dev/null || true
fi
EOF
  fi
}

load_local_compose_token() {
  local token_file wait_seconds deadline
  token_file="${COGNIS_EXECUTOR_TOKEN_FILE:-/run/cognis-local/executor.env}"
  wait_seconds="${COGNIS_EXECUTOR_TOKEN_WAIT_SECONDS:-0}"

  if [[ -n "${COGNIS_EXECUTOR_TOKEN:-}" ]]; then
    return 0
  fi

  if [[ -f "$token_file" ]]; then
    load_executor_env_file "$token_file"
    return 0
  fi

  if [[ "$wait_seconds" == "0" ]]; then
    return 0
  fi

  deadline=$((SECONDS + wait_seconds))
  while [[ SECONDS -lt deadline ]]; do
    if [[ -f "$token_file" ]]; then
      load_executor_env_file "$token_file"
      return 0
    fi
    sleep 2
  done
}

load_executor_env_file() {
  local env_file line key value
  env_file="$1"

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      COGNIS_CONTROLLER_URL|COGNIS_EXECUTOR_TOKEN|COGNIS_EXECUTOR_WORKDIR|COGNIS_EXECUTOR_ALLOW_INSECURE_WS)
        export "$key=$value"
        ;;
    esac
  done < "$env_file"
}

if [[ "$(id -u)" == "0" ]]; then
  mkdir -p "$DEFAULT_HOME"
  if [[ "${COGNIS_SKIP_HOME_CHOWN:-}" != "1" ]]; then
    chown -R "$DEFAULT_UID:$DEFAULT_GID" "$DEFAULT_HOME" 2>/dev/null || true
  fi
  exec gosu "$DEFAULT_USER" /usr/local/bin/cognis-executor-entrypoint "$@"
fi

setup_nss_wrapper
initialize_home
load_local_compose_token

if [[ "$#" -eq 0 ]]; then
  set -- cognis-executor
fi

exec "$@"
