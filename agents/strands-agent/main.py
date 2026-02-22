import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime
from os import linesep
from pathlib import Path
from typing import Tuple

import boto3
import pandas as pd
from dotenv import load_dotenv
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands import tool
from strands.models import BedrockModel
from strands.telemetry import StrandsTelemetry
from strands.tools.mcp import MCPClient
from strands_tools.code_interpreter import AgentCoreCodeInterpreter
from strands_tools.code_interpreter.models import (
    FileContent,
    ListFilesAction,
    WriteFilesAction,
)

from utils.prompts import SYSTEM_PROMPT
from utils.utils import _json_default, extract_text, extract_artifacts_from_sandbox

load_dotenv()


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments for the Strands agent.

    :return: argparse.Namespace object containing parsed arguments
    """
    p = argparse.ArgumentParser(description="Strands agent (Bedrock + Athena MCP + AgentCore Code Interpreter)")
    p.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS profile name used with aws sso login"
    )
    p.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION"),
        help="AWS region (e.g., eu-west-1, us-east-1)"
    )
    p.add_argument(
        "--model-id",
        default=os.environ.get("MODEL_ID"),
        help="AWS Bedrock model ID (or inference profile ARN / model id you use in Bedrock)"
    )
    p.add_argument("--database", default="iris_demo", help="Athena/Glue database")
    p.add_argument("--table", default="iris", help="Athena table")
    p.add_argument(
        "--prompt",
        default="Produce a short summary of features in the iris dataset.",
        help="User request for the agent",
    )
    p.add_argument(
        "--list-tools",
        action="store_true",
        help="List MCP tools and exit (useful for debugging MCP/AWS setup)",
    )
    p.add_argument("--run-dir", default="runs", help="Base directory for logs/traces/artifacts")
    p.add_argument("--enable-otlp", action="store_true", help="Also export traces to OTLP endpoint")
    p.add_argument("--otel-endpoint", default=None, help="OTLP endpoint, e.g. http://localhost:4318")

    return p.parse_args()


def setup_observability(
    run_dir: str,
    enable_otlp: bool = False,
    otel_endpoint: str = None
) -> Tuple[Path, Path]:
    """
    Setup observability infrastructure for the agent run.

    :param run_dir: Base directory for logs/traces/artifacts
    :param enable_otlp: Whether to enable OTLP export
    :param otel_endpoint: OTLP endpoint URL (optional)
    :return: Tuple of observability directory and artifacts directory
    """
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_root_dir = Path(run_dir) / "strands-agent" / ts
    obs_dir = run_root_dir / "observability"
    artifacts_dir = run_root_dir / "artifacts"
    obs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1) Python logging for Strands SDK internals (debug logs)
    log_file = obs_dir / "strands_debug.log"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Avoid duplicate handlers if function is called multiple times in dev
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(log_file) for h in
               root_logger.handlers):
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        root_logger.addHandler(fh)

    # Strands logger debug level (documented quickstart pattern)
    logging.getLogger("strands").setLevel(logging.DEBUG)

    # 2) Strands OpenTelemetry traces -> local JSONL (documented pattern)
    trace_jsonl = obs_dir / "traces.jsonl"
    trace_fp = open(trace_jsonl, "wt", encoding="utf-8")

    telemetry = StrandsTelemetry()
    telemetry.setup_console_exporter(
        out=trace_fp,
        formatter=lambda span: span.to_json() + linesep,
    )

    # Optional OTLP export (Jaeger/Langfuse/etc.)
    if enable_otlp:
        if otel_endpoint:
            telemetry.setup_otlp_exporter(endpoint=otel_endpoint)
        else:
            telemetry.setup_otlp_exporter()
        telemetry.setup_meter(
            enable_console_exporter=False,
            enable_otlp_exporter=True,
        )
    else:
        # Still useful to enable local metrics provider if you want metrics instrumentation exports
        telemetry.setup_meter(enable_console_exporter=False, enable_otlp_exporter=False)

    return obs_dir, artifacts_dir


def make_mcp_client() -> MCPClient:
    """
    Create an MCPClient instance with AWS context from environment variables.

    :return: MCPClient instance
    """
    # Ensure subprocess launched by stdio transport sees the same AWS context
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION")
    os.environ["AWS_DEFAULT_REGION"] = region

    # Important for SSO profiles (they live in ~/.aws/config, not ~/.aws/credentials)
    os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
    # Avoid unexpected credential-provider fallbacks / delays
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    # Optional but generally safer for multi-region setups
    os.environ.setdefault("AWS_STS_REGIONAL_ENDPOINTS", "regional")

    env = dict(os.environ)  # preserve PATH/HOME/etc.
    env.update({
        "AWS_PROFILE": profile,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "AWS_SDK_LOAD_CONFIG": "1",  # harmless for boto3, helps consistency
        "FASTMCP_LOG_LEVEL": "INFO",
    })

    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="uvx",
                args=[
                    "awslabs.aws-dataprocessing-mcp-server@latest",
                    "--allow-write",  # needed for Athena query execution operations
                ],
                env=env
            )
        ),
        tool_filters={
            "allowed": [
                "manage_aws_athena_databases_and_tables",
                "manage_aws_athena_query_executions",
            ]
        },
        prefix="athena",
    )


def make_agent(
    profile: str,
    region: str,
    model_id: str,
    database: str,
    mcp_client: MCPClient
) -> Agent:
    """
    Create a Strands Agent instance with specified configuration.

    :param args: Command-line arguments
    :param mcp_client: MCPClient instance
    :return: Agent instance
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    model = BedrockModel(
        boto_session=session,
        model_id=model_id,
    )

    # Initialise AgentCoreCodeInterpreter with explicit session_name: host-side write_files and agent-side code_interpreter
    # hit the same sandbox session
    ci_session_name = f"iris-{uuid.uuid4().hex[:10]}"
    code_interpreter_tool = AgentCoreCodeInterpreter(
        region=region,
        session_name=ci_session_name,
    )

    @tool
    def athena_query_to_ci_csv(
        sql: str,
        sandbox_path: str = "data/iris_query.csv",
        poll_seconds: float = 0.5,
        timeout_seconds: int = 60,
    ) -> str:
        """
        Execute a read-only Athena query and upload the result as CSV into the AgentCore Code Interpreter sandbox.
        Uses boto3 Athena APIs directly (compatible with Athena managed query results workgroups).
        """
        raw_sql = (sql or "").strip().rstrip(";")
        if not raw_sql:
            return json.dumps({"ok": False, "error": "Empty SQL"})

        if not re.match(r"(?is)^\s*(select|with)\b", raw_sql):
            return json.dumps({"ok": False, "error": "Only read-only SELECT/WITH queries are allowed."})

        wrapped_sql = f"SELECT * FROM ({raw_sql}) AS t"

        athena = session.client("athena", region_name=region)

        try:
            # IMPORTANT: no ResultConfiguration here (managed query results workgroup compatibility)
            start = athena.start_query_execution(
                QueryString=wrapped_sql,
                QueryExecutionContext={"Database": database, "Catalog": "AwsDataCatalog"},
                WorkGroup="primary",
            )
            qid = start["QueryExecutionId"]

            # Poll
            deadline = time.time() + timeout_seconds
            state = "QUEUED"
            reason = None
            while time.time() < deadline:
                q = athena.get_query_execution(QueryExecutionId=qid)
                status = q["QueryExecution"]["Status"]
                state = status["State"]
                reason = status.get("StateChangeReason")
                if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(poll_seconds)

            if state != "SUCCEEDED":
                return json.dumps({
                    "ok": False,
                    "error": f"Athena query {state}",
                    "reason": reason,
                    "query_execution_id": qid,
                    "sql_attempted": wrapped_sql,
                    "database": database,
                    "workgroup": "primary",
                })

            # Fetch paginated rows
            rows = []
            next_token = None
            col_names = None

            while True:
                kwargs = {"QueryExecutionId": qid, "MaxResults": 1000}
                if next_token:
                    kwargs["NextToken"] = next_token
                resp = athena.get_query_results(**kwargs)

                result_set = resp["ResultSet"]
                metadata = result_set["ResultSetMetadata"]["ColumnInfo"]
                if col_names is None:
                    col_names = [c["Name"] for c in metadata]

                page_rows = result_set.get("Rows", [])

                # First row of first page is header in GetQueryResults
                if page_rows:
                    start_idx = 1 if not rows else 0
                    for r in page_rows[start_idx:]:
                        data = r.get("Data", [])
                        vals = [cell.get("VarCharValue") if i < len(data) else None for i, cell in enumerate(data)]
                        # Normalize row length
                        if len(vals) < len(col_names):
                            vals += [None] * (len(col_names) - len(vals))
                        rows.append(vals[: len(col_names)])

                next_token = resp.get("NextToken")
                if not next_token:
                    break

            df = pd.DataFrame(rows, columns=col_names)

            # Optional light type casting for nicer downstream plotting (safe best effort)
            for c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="ignore")

            csv_text = df.to_csv(index=False)

            write_result = code_interpreter_tool.write_files(
                WriteFilesAction(
                    type="writeFiles",
                    content=[FileContent(path=sandbox_path, text=csv_text)],
                )
            )

            list_result = code_interpreter_tool.list_files(
                ListFilesAction(
                    type="listFiles",
                    path=str(Path(sandbox_path).parent),
                )
            )

            return json.dumps({
                "ok": True,
                "query_execution_id": qid,
                "database": database,
                "workgroup": "primary",
                "sql_executed": wrapped_sql,
                "sandbox_path": sandbox_path,
                "rows": int(len(df)),
                "columns": list(df.columns),
                "preview_rows": df.head(5).to_dict(orient="records"),
                "ci_session_name": ci_session_name,
                "write_result": write_result,
                "list_result": list_result,
            }, default=str)

        except Exception as e:
            return json.dumps({
                "ok": False,
                "error": str(e),
                "sql_attempted": wrapped_sql,
                "database": database,
                "workgroup": "primary",
            })

    agent = Agent(
        model=model,
        tools=[
            mcp_client,                   # keep MCP for Athena metadata / discovery
            athena_query_to_ci_csv,       # deterministic handoff tool
            code_interpreter_tool.code_interpreter,  # sandboxed code execution
        ],
        system_prompt=SYSTEM_PROMPT + "\n\n"
        + "IMPORTANT DATA HANDOFF RULES:\n"
        + "- Never manually transcribe tabular rows from tool text into Python lists/dicts.\n"
        + "- For any Athena data that will be analyzed in code interpreter, first call athena_query_to_ci_csv.\n"
        + "- Then use the code_interpreter tool to read the returned CSV path from the sandbox.\n"
        + f"- The code interpreter session name for this run is: {ci_session_name}\n",
    )

    return agent, ci_session_name, code_interpreter_tool


def list_tools_mode() -> int:
    """
    List available tools in MCP mode without constructing an agent.

    :return: Exit code (0 for success)
    """
    # MANUAL MCP MODE ONLY (no agent construction here)
    mcp_client = make_mcp_client()
    try:
        with mcp_client:
            tools = mcp_client.list_tools_sync()
            print("Available MCP tools:", flush=True)
            for t in tools:
                name = getattr(t, "tool_name", None) or (t.get("tool_name") if isinstance(t, dict) else str(t))
                print(f" - {name}", flush=True)
        return 0
    finally:
        # Extra cleanup for local-dev robustness after errors/Ctrl-C
        try:
            mcp_client.stop(None, None, None)
        except Exception as e:
            print(f"Warning during MCP cleanup in list_tools_mode: {e}", file=sys.stderr, flush=True)


def run_agent_mode(args) -> int:
    """
    Run the Strands Agent with the specified configuration.

    :param args: Command-line arguments
    :return: Exit code (0 for success)
    """
    if not args.model_id:
        print("--model-id is required unless using --list-tools", file=sys.stderr, flush=True)
        return 2

    run_dir, artifacts_dir = setup_observability(
        run_dir=args.run_dir,
        enable_otlp=args.enable_otlp,
        otel_endpoint=args.otel_endpoint,
    )

    mcp_client = make_mcp_client()
    try:
        agent, ci_session_name, code_interpreter_tool = make_agent(
            profile=args.profile,
            region=args.region,
            model_id=args.model_id,
            database=args.database,
            mcp_client=mcp_client
        )

        task = f"""
User task:
{args.prompt}

Dataset location:
- Athena database: {args.database}
- Athena table: {args.table}

Please:
- start with a short plan,
- inspect schema if needed,
- run Athena SQL via MCP,
- use code interpreter for stats,
- return concise analysis with SQL used.
""".strip()

        result = agent(task)

        # Human-readable answer
        final_text = extract_text(result)
        print(final_text, flush=True)
        (run_dir / "final_answer.txt").write_text(final_text, encoding="utf-8")

        # Structured metrics summary (tokens, latency, tool usage, traces summary)
        try:
            metrics_summary = result.metrics.get_summary()
            (run_dir / "metrics_summary.json").write_text(
                json.dumps(metrics_summary, indent=2, default=_json_default),
                encoding="utf-8",
            )
        except Exception as e:
            (run_dir / "metrics_summary_error.txt").write_text(str(e), encoding="utf-8")

        # Optional: raw result dump for debugging
        try:
            (run_dir / "agent_result_raw.json").write_text(
                json.dumps(result, indent=2, default=_json_default),
                encoding="utf-8",
            )
        except Exception:
            (run_dir / "agent_result_raw.txt").write_text(str(result), encoding="utf-8")

        # Extract all artifacts from code interpreter sandbox
        try:
            extract_artifacts_from_sandbox(code_interpreter_tool, ci_session_name, artifacts_dir)

            print(f"\n[run artifacts] {run_dir}", flush=True)
        except Exception as e:
            print(f"Warning during artifact extraction: {e}", file=sys.stderr, flush=True)

    finally:
        try:
            mcp_client.stop(None, None, None)
        except Exception as e:
            print(f"Warning during MCP cleanup in run_agent_mode: {e}", file=sys.stderr, flush=True)

        trace_fp = getattr(args, "_trace_fp", None)
        if trace_fp:
            try:
                trace_fp.close()
            except Exception:
                pass


def main() -> int:
    try:
        args = parse_args()
        if args.list_tools:
            return list_tools_mode()
        return run_agent_mode(args)
    except KeyboardInterrupt:
        # Standard shell convention for Ctrl-C
        print("\nInterrupted by user.", file=sys.stderr, flush=True)
        return 130
    except Exception as e:
        # Ensure we exit cleanly even if MCP/model code raises unexpectedly
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())