#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_CONFIG_FILE="${ROOT_DIR}/agents/strands_agent/.bedrock_agentcore.yaml"
ADAPTER_SCRIPT="${ROOT_DIR}/scripts/agentcore_openai_adapter.py"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-eu-central-1}"
export ADAPTER_HOST="${ADAPTER_HOST:-0.0.0.0}"
export ADAPTER_PORT="${ADAPTER_PORT:-8800}"
export AGENTCORE_ADAPTER_MODEL_ID="${AGENTCORE_ADAPTER_MODEL_ID:-agentcore-runtime}"
export AGENTCORE_ADAPTER_API_KEY="${AGENTCORE_ADAPTER_API_KEY:-agentcore-local}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Python not found at ${PYTHON_BIN}. Run: uv sync" >&2
  exit 1
fi

if [[ -z "${AGENT_RUNTIME_ARN:-}" ]]; then
  if [[ ! -f "${AGENT_CONFIG_FILE}" ]]; then
    echo "ERROR: AGENT_RUNTIME_ARN is not set and config file is missing: ${AGENT_CONFIG_FILE}" >&2
    exit 1
  fi
  AGENT_RUNTIME_ARN="$(sed -nE 's/^[[:space:]]*agent_arn:[[:space:]]*"?([^"[:space:]]+)"?[[:space:]]*$/\1/p' "${AGENT_CONFIG_FILE}" | head -n 1)"
  if [[ -z "${AGENT_RUNTIME_ARN}" ]]; then
    echo "ERROR: Could not read agent_arn from ${AGENT_CONFIG_FILE}" >&2
    exit 1
  fi
  export AGENT_RUNTIME_ARN
fi

echo "Starting AgentCore OpenAI adapter..."
echo "  AWS_PROFILE: ${AWS_PROFILE}"
echo "  AWS_REGION: ${AWS_REGION}"
echo "  AGENT_RUNTIME_ARN: ${AGENT_RUNTIME_ARN}"
echo "  ADAPTER_HOST: ${ADAPTER_HOST}"
echo "  ADAPTER_PORT: ${ADAPTER_PORT}"
echo "  MODEL_ID: ${AGENTCORE_ADAPTER_MODEL_ID}"
echo

exec "${PYTHON_BIN}" "${ADAPTER_SCRIPT}"
