from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import boto3
import pandas as pd
from deepagents import create_deep_agent
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool

from prompts import SYSTEM_PROMPT

ALLOWED_ATHENA_MCP_TOOL_NAMES = {
    "manage_aws_athena_databases_and_tables",
    "manage_aws_athena_query_executions",
}


@dataclass
class BackendBundle:
    backend: SandboxBackendProtocol
    name: str
    cleanup: Callable[[], None]


def make_backend(kind: str, local_shell_root: str | None = None) -> BackendBundle:
    if kind == "daytona":
        try:
            from daytona import Daytona
            from langchain_daytona import DaytonaSandbox
        except ImportError as e:
            raise RuntimeError(
                "Daytona backend selected but dependencies are missing. "
                "Install project deps with `uv sync`."
            ) from e

        daytona = Daytona()
        sandbox = daytona.create()
        backend = DaytonaSandbox(sandbox=sandbox)
        sandbox_id = getattr(backend, "id", "unknown")

        def _cleanup() -> None:
            try:
                sandbox.delete()
            except Exception:
                pass

        return BackendBundle(backend=backend, name=f"daytona:{sandbox_id}", cleanup=_cleanup)

    if kind == "local-shell":
        from deepagents.backends.local_shell import LocalShellBackend

        root_dir = str(Path(local_shell_root or ".").resolve())
        backend = LocalShellBackend(
            root_dir=root_dir,
            virtual_mode=True,
            inherit_env=True,
        )
        return BackendBundle(backend=backend, name=f"local-shell:{root_dir}", cleanup=lambda: None)

    raise ValueError(f"Unsupported backend kind: {kind}")


def make_bedrock_model(
    model_id: str,
    region: str,
    profile: str
) -> Any:
    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as e:
        raise RuntimeError(
            "langchain-aws is not installed. Run `uv sync` before running langchain-agent."
        ) from e

    return ChatBedrockConverse(
        model_id=model_id,
        region_name=region,
        credentials_profile_name=profile,
    )


def make_mcp_env(profile: str | None, region: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
            "AWS_SDK_LOAD_CONFIG": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_STS_REGIONAL_ENDPOINTS": "regional",
            "FASTMCP_LOG_LEVEL": "INFO",
        }
    )
    if profile:
        env["AWS_PROFILE"] = profile
    return env


async def load_athena_mcp_tools(profile: str | None, region: str) -> list[BaseTool]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as e:
        raise RuntimeError(
            "langchain-mcp-adapters is not installed. Run `uv sync` before running langchain-agent."
        ) from e

    mcp_client = MultiServerMCPClient(
        {
            "athena": {
                "command": "uvx",
                "args": [
                    "awslabs.aws-dataprocessing-mcp-server@latest",
                    "--allow-write",
                ],
                "transport": "stdio",
                "env": make_mcp_env(profile=profile, region=region),
            }
        }
    )

    tools = await mcp_client.get_tools()
    filtered = [tool_obj for tool_obj in tools if tool_obj.name in ALLOWED_ATHENA_MCP_TOOL_NAMES]
    return filtered if filtered else tools


def make_athena_query_to_backend_csv_tool(
    *,
    session: boto3.Session,
    region: str,
    database: str,
    backend: SandboxBackendProtocol,
    backend_name: str,
) -> BaseTool:
    ci_session_name = f"iris-{uuid.uuid4().hex[:10]}"

    @tool
    def athena_query_to_backend_csv(
        sql: str,
        sandbox_path: str = "/tmp/iris_query.csv",
        poll_seconds: float = 0.5,
        timeout_seconds: int = 60,
    ) -> str:
        """
        Execute a read-only Athena query and upload the result as CSV into the backend sandbox.
        """
        raw_sql = (sql or "").strip().rstrip(";")
        if not raw_sql:
            return json.dumps({"ok": False, "error": "Empty SQL"})

        if not re.match(r"(?is)^\s*(select|with)\b", raw_sql):
            return json.dumps({"ok": False, "error": "Only read-only SELECT/WITH queries are allowed."})

        sandbox_path_abs = sandbox_path if sandbox_path.startswith("/") else f"/tmp/{sandbox_path.lstrip('/')}"
        wrapped_sql = f"SELECT * FROM ({raw_sql}) AS t"
        athena = session.client("athena", region_name=region)

        try:
            start = athena.start_query_execution(
                QueryString=wrapped_sql,
                QueryExecutionContext={"Database": database, "Catalog": "AwsDataCatalog"},
                WorkGroup="primary",
            )
            qid = start["QueryExecutionId"]

            deadline = time.time() + timeout_seconds
            state = "QUEUED"
            reason = None
            while time.time() < deadline:
                query_execution = athena.get_query_execution(QueryExecutionId=qid)
                status = query_execution["QueryExecution"]["Status"]
                state = status["State"]
                reason = status.get("StateChangeReason")
                if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(poll_seconds)

            if state != "SUCCEEDED":
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Athena query {state}",
                        "reason": reason,
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": database,
                        "workgroup": "primary",
                    }
                )

            rows: list[list[Any]] = []
            next_token = None
            col_names = None

            while True:
                kwargs: dict[str, Any] = {"QueryExecutionId": qid, "MaxResults": 1000}
                if next_token:
                    kwargs["NextToken"] = next_token
                response = athena.get_query_results(**kwargs)

                result_set = response["ResultSet"]
                metadata = result_set["ResultSetMetadata"]["ColumnInfo"]
                if col_names is None:
                    col_names = [column["Name"] for column in metadata]

                page_rows = result_set.get("Rows", [])
                if page_rows:
                    start_idx = 1 if not rows else 0
                    for row in page_rows[start_idx:]:
                        data = row.get("Data", [])
                        values = [cell.get("VarCharValue") if i < len(data) else None for i, cell in enumerate(data)]
                        if len(values) < len(col_names):
                            values += [None] * (len(col_names) - len(values))
                        rows.append(values[: len(col_names)])

                next_token = response.get("NextToken")
                if not next_token:
                    break

            df = pd.DataFrame(rows, columns=col_names)
            for column_name in df.columns:
                df[column_name] = pd.to_numeric(df[column_name], errors="ignore")

            csv_bytes = df.to_csv(index=False).encode("utf-8")
            upload_result = backend.upload_files([(sandbox_path_abs, csv_bytes)])
            upload_status = [{"path": item.path, "error": item.error} for item in upload_result]

            return json.dumps(
                {
                    "ok": True,
                    "query_execution_id": qid,
                    "database": database,
                    "workgroup": "primary",
                    "sql_executed": wrapped_sql,
                    "sandbox_path": sandbox_path_abs,
                    "rows": int(len(df)),
                    "columns": list(df.columns),
                    "preview_rows": df.head(5).to_dict(orient="records"),
                    "backend": backend_name,
                    "backend_session_name": ci_session_name,
                    "upload_status": upload_status,
                },
                default=str,
            )
        except Exception as e:
            return json.dumps(
                {
                    "ok": False,
                    "error": str(e),
                    "sql_attempted": wrapped_sql,
                    "database": database,
                    "workgroup": "primary",
                }
            )

    return athena_query_to_backend_csv


def create_langchain_agent(
    *,
    profile: str | None,
    region: str,
    model_id: str,
    database: str,
    table: str,
    backend: SandboxBackendProtocol,
    backend_name: str,
    debug: bool = False,
) -> tuple[Runnable, list[str]]:
    session = boto3.Session(profile_name=profile, region_name=region)
    model = make_bedrock_model(model_id=model_id, region=region, profile=profile)

    mcp_tools = asyncio.run(load_athena_mcp_tools(profile=profile, region=region))
    handoff_tool = make_athena_query_to_backend_csv_tool(
        session=session,
        region=region,
        database=database,
        backend=backend,
        backend_name=backend_name,
    )
    tools = [*mcp_tools, handoff_tool]

    context_prompt = (
        f"Run context:\n"
        f"- Athena database: {database}\n"
        f"- Athena table: {table}\n"
        f"- Sandbox backend: {backend_name}\n"
    )
    deep_agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=f"{SYSTEM_PROMPT}\n\n{context_prompt}",
        backend=backend,
        debug=debug,
        name="langchain-agent",
    )

    return deep_agent, [tool_obj.name for tool_obj in tools]
