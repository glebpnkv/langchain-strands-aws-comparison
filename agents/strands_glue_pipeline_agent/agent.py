import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import boto3
from mcp import StdioServerParameters, stdio_client
from strands import Agent, AgentSkills
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands_tools.code_interpreter import AgentCoreCodeInterpreter

from utils.hooks import GlueJobRunPollThrottleHook
from utils.prompts import SYSTEM_PROMPT
from utils.tools import make_athena_query_to_ci_csv, make_glue_job_run_diagnostics_tool

ALLOWED_MCP_TOOLS = [
    "manage_aws_athena_databases_and_tables",
    "manage_aws_athena_query_executions",
    "manage_aws_glue_jobs",
    "manage_aws_glue_crawlers",
    "manage_aws_glue_triggers",
    "manage_aws_glue_workflows",
    "list_s3_buckets",
    "upload_to_s3",
    "manage_aws_athena_named_queries",
    "manage_aws_athena_workgroups"
]

AWS_API_ALLOWED_MCP_TOOLS = [
    "call_aws",
]


def _get_glue_poll_interval_seconds(default_seconds: float = 20.0) -> float:
    """
    Resolve minimum poll interval for repeated Glue get-job-run calls.

    Reads `GLUE_GET_JOB_RUN_MIN_INTERVAL_SECONDS` from environment and falls back
    to `default_seconds` when unset or invalid.

    :param default_seconds: Default minimum interval in seconds
    :return: Non-negative interval in seconds
    """
    raw_value = (os.getenv("GLUE_GET_JOB_RUN_MIN_INTERVAL_SECONDS") or "").strip()
    if not raw_value:
        return max(0.0, default_seconds)

    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return max(0.0, default_seconds)


def _resolve_mcp_server_command() -> tuple[str, list[str]]:
    """
    Resolve how to launch the AWS data processing MCP server.

    Prefer the installed Python module for reproducible runtime deploys.
    Fall back to `uvx` when the package is not installed.
    """
    try:
        import awslabs.aws_dataprocessing_mcp_server.server  # noqa: F401

        return (
            sys.executable,
            [
                "-m",
                "awslabs.aws_dataprocessing_mcp_server.server",
                "--allow-write",
            ],
        )
    except Exception:
        return (
            "uvx",
            [
                "awslabs.aws-dataprocessing-mcp-server@latest",
                "--allow-write",
            ],
        )

def _resolve_api_mcp_server_command() -> tuple[str, list[str]]:
    try:
        import awslabs.aws_api_mcp_server.server  # noqa: F401

        return (
            sys.executable,
            [
                "-m",
                "awslabs.aws_api_mcp_server.server",
            ],
        )
    except Exception:
        return (
            "uvx",
            [
                "awslabs.aws-api-mcp-server@latest",
            ],
        )


def _build_mcp_env(profile: Optional[str], region: Optional[str]) -> dict[str, str]:
    """
    Build subprocess environment for the AWS MCP server.

    :param profile: Optional AWS profile
    :param region: Optional AWS region
    :return: Environment dictionary for stdio MCP subprocess
    """
    if region:
        os.environ["AWS_DEFAULT_REGION"] = region

    # Important for SSO profiles (they live in ~/.aws/config, not ~/.aws/credentials).
    os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
    # Avoid unexpected credential-provider fallbacks / delays.
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    # Safer for multi-region setups.
    os.environ.setdefault("AWS_STS_REGIONAL_ENDPOINTS", "regional")

    env = dict(os.environ)  # preserve PATH/HOME/etc.
    env["AWS_SDK_LOAD_CONFIG"] = "1"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_STS_REGIONAL_ENDPOINTS"] = "regional"
    env["FASTMCP_LOG_LEVEL"] = "INFO"
    if profile:
        env["AWS_PROFILE"] = profile
    if region:
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region
    return env


def make_mcp_client(allowed_tools: Optional[list[str]] = None) -> MCPClient:
    """
    Create an MCP client configured for Athena/Glue operations.

    :param allowed_tools: Optional override for allowed tool names
    :return: MCPClient instance
    """
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION")
    env = _build_mcp_env(profile=profile, region=region)
    server_command, server_args = _resolve_mcp_server_command()

    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=server_command,
                args=server_args,
                env=env,
            )
        ),
        tool_filters={"allowed": allowed_tools or ALLOWED_MCP_TOOLS},
        prefix="athena",
    )

def make_aws_api_mcp_client() -> MCPClient:
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION")
    env = _build_mcp_env(profile=profile, region=region)

    # AWS API MCP prefers its own profile env var.
    if profile:
        env["AWS_API_MCP_PROFILE_NAME"] = profile
    if region:
        env["AWS_REGION"] = region

    server_command, server_args = _resolve_api_mcp_server_command()

    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=server_command,
                args=server_args,
                env=env,
            )
        ),
        tool_filters={"allowed": AWS_API_ALLOWED_MCP_TOOLS},
        prefix="awsapi",
    )

def _build_runtime_glue_rules() -> str:
    """
    Build runtime-focused Glue rules appended to system prompt.

    :return: Runtime-specific prompt section
    """
    role_text = os.getenv("GLUE_JOB_ROLE_ARN", "") or "(not configured; ask user for role ARN)"
    # TODO: move from static default script object to conversation-scoped script paths:
    # s3://glue-assets-554032904022-eu-central-1-an/strands-glue-pipeline-agent/scripts/<conversation_id>/<script_name>.py
    script_text = os.getenv("GLUE_JOB_DEFAULT_SCRIPT_S3", "") or "(not configured; ask user for script S3 URI)"
    temp_dir_text = os.getenv("GLUE_TEMP_DIR", "") or "(not configured)"
    poll_interval_seconds = _get_glue_poll_interval_seconds(default_seconds=20.0)
    return (
        "RUNTIME GLUE RULES:\n"
        "- When GLUE_JOB_ROLE_ARN is configured, always set `job_definition.Role` to that exact ARN.\n"
        "- Do not use AWSGlueServiceRole-default unless the user explicitly asks for it.\n"
        "- When role/script/temp are not provided by user, use these defaults:\n"
        f"  - Role ARN default: {role_text}\n"
        f"  - ScriptLocation default: {script_text}\n"
        f"  - --TempDir default: {temp_dir_text}\n"
        "- For crawler-based conditional triggers:\n"
        "  - ensure crawler exists first (via `athena_manage_aws_glue_crawlers`),\n"
        "  - in predicate conditions use `CrawlerName` with `CrawlState`, not `State`.\n"
        "- Completion precondition for any created/updated job:\n"
        "  - run `athena_manage_aws_glue_jobs(operation='start-job-run', ...)`,\n"
        f"  - poll `athena_manage_aws_glue_jobs(operation='get-job-run', ...)` once every {poll_interval_seconds:g} seconds until terminal state,\n"
        "  - only mark success if state is `SUCCEEDED`;\n"
        "  - if state is `FAILED`/`TIMEOUT`, call `glue_get_job_run_diagnostics` and include root-cause log lines.\n"
        "- If `update-job` fails due MCP-managed resource constraints, explain it and propose create-new-job fallback.\n"
        "- For schedule/cron output, always state timezone explicitly as UTC.\n"
        f"- Runtime guardrail: repeated `get-job-run` calls for the same `(job_name, job_run_id)` are throttled to at least {poll_interval_seconds:g}s.\n"
    )


def _build_ci_handoff_rules(ci_session_name: str) -> str:
    """
    Build code-interpreter handoff rules appended to system prompt.

    :param ci_session_name: Code interpreter session identifier
    :return: Prompt section for Athena-to-CI handoff
    """
    return (
        "IMPORTANT DATA HANDOFF RULES:\n"
        "- Never manually transcribe tabular rows from tool text into Python lists/dicts.\n"
        "- For Athena data that will be analyzed in code interpreter, first call athena_query_to_ci_csv.\n"
        "- Always pass `database` explicitly when calling athena_query_to_ci_csv.\n"
        "- Then use the code_interpreter tool to read the returned CSV path from the sandbox.\n"
        + f"- The code interpreter session name for this run is: {ci_session_name}\n"
    )


def _create_skills_plugin(skills_dir: Path):
    """
    Create a Strands AgentSkills plugin from local skill files.

    Args:
        skills_dir: Directory containing skills (each child has `SKILL.md`).

    Returns:
        AgentSkills plugin instance, or `None` when the directory is missing.
    """
    if not skills_dir.exists():
        return None
    return AgentSkills(skills=str(skills_dir))


def make_agent(
    profile: Optional[str],
    region: str,
    model_id: str,
    mcp_client: MCPClient,
    enable_code_interpreter: bool = True,
) -> tuple[Agent, Optional[str], Optional[AgentCoreCodeInterpreter]]:
    """
    Create the Strands Glue pipeline agent.

    One function is used by both local CLI mode and AgentCore runtime mode.

    :param profile: AWS profile name
    :param region: AWS region
    :param model_id: Bedrock model ID
    :param mcp_client: MCP client wrapper for Athena/Glue tools
    :param enable_code_interpreter: Whether to attach AgentCore code interpreter + CSV handoff tool
    :return: Agent, optional CI session name, optional code interpreter tool
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    model = BedrockModel(
        boto_session=session,
        model_id=model_id,
    )

    aws_api_mcp_client = make_aws_api_mcp_client()

    tools = [mcp_client, aws_api_mcp_client]
    prompt_parts = [SYSTEM_PROMPT]
    poll_interval_seconds = _get_glue_poll_interval_seconds(default_seconds=20.0)
    hooks = [GlueJobRunPollThrottleHook(min_interval_seconds=poll_interval_seconds)]
    skills_dir = Path(__file__).resolve().parent / "skills"
    skills_plugin = _create_skills_plugin(skills_dir)
    ci_session_name: Optional[str] = None
    code_interpreter_tool: Optional[AgentCoreCodeInterpreter] = None

    # Always include Glue runtime rules so both local CLI and AgentCore runtime
    # enforce the same job/role/trigger behavior.
    prompt_parts.append(_build_runtime_glue_rules())

    if enable_code_interpreter:
        # Keep one explicit CI session name to simplify tool handoff and artifact extraction.
        ci_session_name = f"glue-pipeline-{uuid.uuid4().hex[:10]}"
        code_interpreter_tool = AgentCoreCodeInterpreter(
            region=region,
            session_name=ci_session_name,
        )

        athena_query_to_ci_csv = make_athena_query_to_ci_csv(
            session=session,
            region=region,
            code_interpreter_tool=code_interpreter_tool,
            ci_session_name=ci_session_name,
        )

        tools.extend([athena_query_to_ci_csv, code_interpreter_tool.code_interpreter])
        prompt_parts.append(_build_ci_handoff_rules(ci_session_name=ci_session_name))

    # Available in both local and runtime modes for failed-run debugging.
    glue_job_run_diagnostics = make_glue_job_run_diagnostics_tool(
        session=session,
        region=region,
    )
    tools.append(glue_job_run_diagnostics)

    agent_kwargs = {
        "model": model,
        "tools": tools,
        "system_prompt": "\n\n".join(prompt_parts),
        "hooks": hooks,
    }
    if skills_plugin is not None:
        agent_kwargs["plugins"] = [skills_plugin]

    agent = Agent(**agent_kwargs)

    return agent, ci_session_name, code_interpreter_tool
