#!/usr/bin/env bash
set -euo pipefail

# aws sso login --profile default
export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-eu-central-1}"
export MODEL_ID="${MODEL_ID:-eu.anthropic.claude-haiku-4-5-20251001-v1:0}"
export ATHENA_DATABASE="${ATHENA_DATABASE:-iris_demo}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="${ROOT_DIR}/agents/strands_agent"
CONFIG_FILE="${AGENT_DIR}/.bedrock_agentcore.yaml"
AGENTCORE_REQUIREMENTS_FILE="${AGENT_DIR}/requirements-agentcore.txt"
PACKAGING_REQUIREMENTS_FILE="${AGENT_DIR}/requirements.txt"

: "${AWS_REGION:?set AWS_REGION}"
: "${MODEL_ID:?set MODEL_ID}"
: "${ATHENA_DATABASE:?set ATHENA_DATABASE}"

AGENT_NAME="${AGENT_NAME:-agentcore_runtime}"
ATHENA_TABLE="${ATHENA_TABLE:-}"
ATHENA_WORKGROUP="${ATHENA_WORKGROUP:-primary}"
ATHENA_OUTPUT_S3="${ATHENA_OUTPUT_S3:-}"
AUTO_ATTACH_ATHENA_GLUE_POLICY="${AUTO_ATTACH_ATHENA_GLUE_POLICY:-1}"

if command -v agentcore >/dev/null 2>&1; then
  AGENTCORE_BIN="$(command -v agentcore)"
elif [[ -x "${ROOT_DIR}/.venv/bin/agentcore" ]]; then
  AGENTCORE_BIN="${ROOT_DIR}/.venv/bin/agentcore"
else
  echo "ERROR: 'agentcore' CLI not found. Activate your venv or install Bedrock AgentCore toolkit." >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: AWS CLI is required." >&2
  exit 1
fi

sync_packaging_requirements() {
  if [[ ! -f "${AGENTCORE_REQUIREMENTS_FILE}" ]]; then
    echo "ERROR: Missing requirements file: ${AGENTCORE_REQUIREMENTS_FILE}" >&2
    exit 1
  fi

  if [[ ! -f "${PACKAGING_REQUIREMENTS_FILE}" ]] || ! cmp -s "${AGENTCORE_REQUIREMENTS_FILE}" "${PACKAGING_REQUIREMENTS_FILE}"; then
    cp "${AGENTCORE_REQUIREMENTS_FILE}" "${PACKAGING_REQUIREMENTS_FILE}"
    echo "Updated ${PACKAGING_REQUIREMENTS_FILE} from ${AGENTCORE_REQUIREMENTS_FILE}"
  fi
}

clear_stale_runtime_metadata() {
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    return 0
  fi

  local runtime_id=""
  runtime_id="$(sed -nE 's/^[[:space:]]*agent_id:[[:space:]]*"?([^"[:space:]]+)"?[[:space:]]*$/\1/p' "${CONFIG_FILE}" | head -n 1)"

  if [[ -z "${runtime_id}" || "${runtime_id}" == "null" ]]; then
    return 0
  fi

  echo "Checking existing runtime in local config: ${runtime_id}"

  local check_output=""
  set +e
  check_output="$(aws bedrock-agentcore-control get-agent-runtime \
    --region "${AWS_REGION}" \
    --agent-runtime-id "${runtime_id}" \
    --no-cli-pager 2>&1)"
  local check_status=$?
  set -e

  if [[ ${check_status} -eq 0 ]]; then
    echo "[OK] Runtime exists in AWS, continuing with update deploy."
    return 0
  fi

  if grep -q "ResourceNotFoundException" <<< "${check_output}"; then
    echo "[WARN] Local config points to deleted runtime '${runtime_id}'."
    echo "    Clearing local runtime metadata so deploy creates a new runtime..."
    perl -i -pe \
      's/^(\s*agent_id:).*/$1 null/; s/^(\s*agent_arn:).*/$1 null/; s/^(\s*agent_session_id:).*/$1 null/;' \
      "${CONFIG_FILE}"
    return 0
  fi

  echo "ERROR: Failed to verify runtime ID '${runtime_id}' in AWS." >&2
  echo "${check_output}" >&2
  exit 1
}

attach_athena_glue_policy() {
  if [[ "${AUTO_ATTACH_ATHENA_GLUE_POLICY}" != "1" ]]; then
    return 0
  fi

  if [[ ! -f "${CONFIG_FILE}" ]]; then
    return 0
  fi

  local execution_role_arn=""
  execution_role_arn="$(sed -nE 's/^[[:space:]]*execution_role:[[:space:]]*"?([^"[:space:]]+)"?[[:space:]]*$/\1/p' "${CONFIG_FILE}" | head -n 1)"

  if [[ -z "${execution_role_arn}" || "${execution_role_arn}" == "null" ]]; then
    echo "[WARN] Could not determine execution role ARN from ${CONFIG_FILE}; skipping Athena/Glue policy attachment."
    return 0
  fi

  local role_name="${execution_role_arn##*/}"
  local policy_file
  policy_file="$(mktemp)"

  cat > "${policy_file}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GlueCatalogRead",
      "Effect": "Allow",
      "Action": [
        "glue:GetDatabase",
        "glue:GetDatabases",
        "glue:GetTable",
        "glue:GetTables",
        "glue:GetPartition",
        "glue:GetPartitions"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AthenaQueryExecution",
      "Effect": "Allow",
      "Action": [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution",
        "athena:GetWorkGroup",
        "athena:ListWorkGroups"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AthenaResultsS3",
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::*",
        "arn:aws:s3:::*/*"
      ]
    }
  ]
}
JSON

  aws iam put-role-policy \
    --role-name "${role_name}" \
    --policy-name "StrandsAthenaGlueAccess" \
    --policy-document "file://${policy_file}" \
    --no-cli-pager >/dev/null

  rm -f "${policy_file}"
  echo "[OK] Ensured Athena/Glue access policy on role ${role_name}"
}

cd "${AGENT_DIR}"

sync_packaging_requirements

"${AGENTCORE_BIN}" configure \
  --entrypoint "${ROOT_DIR}/agents/strands_agent/agentcore_runtime.py" \
  --name "${AGENT_NAME}" \
  --requirements-file "${AGENTCORE_REQUIREMENTS_FILE}" \
  --deployment-type direct_code_deploy \
  --region "${AWS_REGION}" \
  --non-interactive

clear_stale_runtime_metadata

"${AGENTCORE_BIN}" deploy \
  --auto-update-on-conflict \
  --force-rebuild-deps \
  --env AWS_REGION="${AWS_REGION}" \
  --env MODEL_ID="${MODEL_ID}" \
  --env ATHENA_DATABASE="${ATHENA_DATABASE}" \
  --env ATHENA_TABLE="${ATHENA_TABLE}" \
  --env ATHENA_WORKGROUP="${ATHENA_WORKGROUP}" \
  --env ATHENA_OUTPUT_S3="${ATHENA_OUTPUT_S3}"

attach_athena_glue_policy

echo
echo "Deployment finished."
echo "The AgentCore CLI output includes the runtime ARN."
echo "Save it for the LibreChat adapter:"
echo "  export AGENT_RUNTIME_ARN='arn:aws:bedrock-agentcore:...'"
echo
echo "Quick test:"
echo "  ${AGENTCORE_BIN} invoke '{\"prompt\":\"List the tables available in the configured database.\"}'"
