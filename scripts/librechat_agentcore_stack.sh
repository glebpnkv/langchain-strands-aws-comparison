#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIBRECHAT_DIR="${ROOT_DIR}/vendor/LibreChat"
ADAPTER_START_SCRIPT="${ROOT_DIR}/scripts/start_agentcore_openai_adapter.sh"

ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}"
ADAPTER_PORT="${ADAPTER_PORT:-8800}"
ADAPTER_API_KEY="${AGENTCORE_ADAPTER_API_KEY:-agentcore-local}"
ADAPTER_PID_FILE="${ADAPTER_PID_FILE:-/tmp/agentcore_openai_adapter.pid}"
ADAPTER_LOG_FILE="${ADAPTER_LOG_FILE:-${ROOT_DIR}/runs/agentcore_openai_adapter.log}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <up|start|down|stop|status|logs> [--no-follow]

Commands:
  up      Start adapter + LibreChat. Follows LibreChat API logs by default.
  start   Start adapter + LibreChat without following logs.
  down    Stop LibreChat and the adapter started via this script.
  stop    Stop adapter started via this script.
  status  Show adapter health and docker compose status.
  logs    Follow LibreChat API logs.

Options:
  --no-follow  Do not follow logs after \`up\`.
EOF
}

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: ${cmd}" >&2
    exit 1
  fi
}

adapter_healthcheck() {
  curl -fsS \
    -H "Authorization: Bearer ${ADAPTER_API_KEY}" \
    "http://${ADAPTER_HOST}:${ADAPTER_PORT}/v1/models" >/dev/null 2>&1
}

is_pid_running() {
  local pid="$1"
  kill -0 "${pid}" >/dev/null 2>&1
}

start_adapter() {
  require_command curl

  if adapter_healthcheck; then
    echo "[OK] Adapter already healthy at http://${ADAPTER_HOST}:${ADAPTER_PORT}"
    return 0
  fi

  if [[ -f "${ADAPTER_PID_FILE}" ]]; then
    local old_pid
    old_pid="$(cat "${ADAPTER_PID_FILE}" || true)"
    if [[ -n "${old_pid}" ]] && is_pid_running "${old_pid}"; then
      echo "[WARN] Stopping stale adapter PID ${old_pid}"
      kill "${old_pid}" >/dev/null 2>&1 || true
      sleep 1
    fi
    rm -f "${ADAPTER_PID_FILE}"
  fi

  mkdir -p "$(dirname "${ADAPTER_LOG_FILE}")"

  echo "Starting adapter..."
  nohup "${ADAPTER_START_SCRIPT}" >"${ADAPTER_LOG_FILE}" 2>&1 &
  local pid=$!
  echo "${pid}" > "${ADAPTER_PID_FILE}"

  local waited=0
  until adapter_healthcheck; do
    waited=$((waited + 1))
    if [[ ${waited} -ge 30 ]]; then
      echo "ERROR: Adapter failed health check after 30s." >&2
      echo "Last adapter logs:" >&2
      tail -n 80 "${ADAPTER_LOG_FILE}" >&2 || true
      exit 1
    fi
    sleep 1
  done

  echo "[OK] Adapter started (PID ${pid})"
  echo "     Log file: ${ADAPTER_LOG_FILE}"
}

stop_adapter() {
  if [[ ! -f "${ADAPTER_PID_FILE}" ]]; then
    echo "[OK] No adapter PID file found (${ADAPTER_PID_FILE})"
    return 0
  fi

  local pid
  pid="$(cat "${ADAPTER_PID_FILE}" || true)"
  if [[ -z "${pid}" ]]; then
    rm -f "${ADAPTER_PID_FILE}"
    echo "[OK] Cleared empty adapter PID file"
    return 0
  fi

  if is_pid_running "${pid}"; then
    echo "Stopping adapter PID ${pid}..."
    kill "${pid}" >/dev/null 2>&1 || true
    sleep 1
    if is_pid_running "${pid}"; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
    echo "[OK] Adapter stopped"
  else
    echo "[OK] Adapter PID ${pid} was not running"
  fi

  rm -f "${ADAPTER_PID_FILE}"
}

start_librechat() {
  require_command docker
  echo "Starting LibreChat stack..."
  (
    cd "${LIBRECHAT_DIR}"
    docker compose up -d
  )
  echo "[OK] LibreChat started"
}

stop_librechat() {
  require_command docker
  echo "Stopping LibreChat stack..."
  (
    cd "${LIBRECHAT_DIR}"
    docker compose down
  )
  echo "[OK] LibreChat stopped"
}

show_logs() {
  require_command docker
  (
    cd "${LIBRECHAT_DIR}"
    docker compose logs -f api
  )
}

show_status() {
  require_command docker
  if adapter_healthcheck; then
    echo "[OK] Adapter healthy at http://${ADAPTER_HOST}:${ADAPTER_PORT}"
  else
    echo "[WARN] Adapter not healthy at http://${ADAPTER_HOST}:${ADAPTER_PORT}"
  fi

  if [[ -f "${ADAPTER_PID_FILE}" ]]; then
    local pid
    pid="$(cat "${ADAPTER_PID_FILE}" || true)"
    if [[ -n "${pid}" ]] && is_pid_running "${pid}"; then
      echo "[OK] Adapter PID file: ${ADAPTER_PID_FILE} (pid=${pid})"
    else
      echo "[WARN] Adapter PID file exists but process not running: ${ADAPTER_PID_FILE}"
    fi
  else
    echo "[INFO] Adapter PID file not present: ${ADAPTER_PID_FILE}"
  fi

  (
    cd "${LIBRECHAT_DIR}"
    docker compose ps
  )
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi

  local cmd="$1"
  shift

  local follow_logs=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-follow)
        follow_logs=0
        ;;
      *)
        echo "ERROR: Unknown option: $1" >&2
        usage
        exit 1
        ;;
    esac
    shift
  done

  case "${cmd}" in
    up)
      start_adapter
      start_librechat
      if [[ ${follow_logs} -eq 1 ]]; then
        show_logs
      fi
      ;;
    start)
      start_adapter
      start_librechat
      ;;
    down)
      stop_librechat
      stop_adapter
      ;;
    stop)
      stop_adapter
      ;;
    status)
      show_status
      ;;
    logs)
      show_logs
      ;;
    *)
      echo "ERROR: Unknown command: ${cmd}" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
