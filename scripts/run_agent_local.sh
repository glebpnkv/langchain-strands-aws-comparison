#!/usr/bin/env bash
# Run an agent's FastAPI service locally for development.
#
# Usage: ./scripts/run_agent_local.sh <agent_dir_name> [uvicorn args...]
# Example: ./scripts/run_agent_local.sh strands_glue_pipeline_agent --port 18080
#
# Sets PYTHONPATH so both `agent_server` (repo root) and the agent's
# sibling modules (`agent`, `server`) resolve correctly.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <agent_dir_name> [uvicorn args...]" >&2
  exit 2
fi

AGENT_NAME="$1"
shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="${REPO_ROOT}/agents/${AGENT_NAME}"

if [[ ! -d "${AGENT_DIR}" ]]; then
  echo "ERROR: agent dir not found: ${AGENT_DIR}" >&2
  exit 1
fi

if [[ ! -f "${AGENT_DIR}/server/main.py" ]]; then
  echo "ERROR: ${AGENT_DIR}/server/main.py not found — agent has no FastAPI service?" >&2
  exit 1
fi

export PYTHONPATH="${REPO_ROOT}:${AGENT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

exec uv run uvicorn server.main:app \
  --app-dir "${AGENT_DIR}" \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8080}" \
  --workers 1 \
  "$@"
