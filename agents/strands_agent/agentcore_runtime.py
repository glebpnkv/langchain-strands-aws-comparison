import json
import os
import time
import uuid
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from utils.prompts import SYSTEM_PROMPT
from utils.utils import extract_text

app = BedrockAgentCoreApp()

AWS_REGION = os.environ["AWS_REGION"]
MODEL_ID = os.environ["MODEL_ID"]
ATHENA_DATABASE = os.environ["ATHENA_DATABASE"]
ATHENA_TABLE = os.getenv("ATHENA_TABLE", "")
ATHENA_WORKGROUP = os.getenv("ATHENA_WORKGROUP", "primary")
ATHENA_OUTPUT_S3 = os.getenv("ATHENA_OUTPUT_S3", "")

session = boto3.Session(region_name=AWS_REGION)
athena = session.client("athena")
glue = session.client("glue")
model = BedrockModel(model_id=MODEL_ID, boto_session=session)


def _query_context() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "QueryExecutionContext": {"Database": ATHENA_DATABASE},
        "WorkGroup": ATHENA_WORKGROUP,
    }
    if ATHENA_OUTPUT_S3:
        kwargs["ResultConfiguration"] = {"OutputLocation": ATHENA_OUTPUT_S3}
    return kwargs


def _normalize_cell(cell: dict[str, Any]) -> str:
    return cell.get("VarCharValue", "")


def _wait_for_query(query_execution_id: str, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        res = athena.get_query_execution(QueryExecutionId=query_execution_id)
        state = res["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return
        if state in {"FAILED", "CANCELLED"}:
            reason = res["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(1.5)

    athena.stop_query_execution(QueryExecutionId=query_execution_id)
    raise TimeoutError(f"Athena query timed out after {timeout_seconds}s")


def _run_athena_query(sql: str, max_rows: int = 200) -> list[dict[str, Any]]:
    start = athena.start_query_execution(
        QueryString=sql,
        **_query_context(),
    )
    qid = start["QueryExecutionId"]
    _wait_for_query(qid)

    rows: list[dict[str, Any]] = []
    next_token = None
    columns = None

    while True:
        kwargs: dict[str, Any] = {"QueryExecutionId": qid, "MaxResults": min(max_rows + 1, 1000)}
        if next_token:
            kwargs["NextToken"] = next_token

        page = athena.get_query_results(**kwargs)

        if columns is None:
            columns = [c["Name"] for c in page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]

        page_rows = page["ResultSet"]["Rows"]

        # Athena often returns a header row first
        start_idx = 0
        if page_rows:
            first = [_normalize_cell(c) for c in page_rows[0].get("Data", [])]
            if first == columns:
                start_idx = 1

        for row in page_rows[start_idx:]:
            rows.append(
                {
                    col: _normalize_cell(cell)
                    for col, cell in zip(columns, row.get("Data", []))
                }
            )
            if len(rows) >= max_rows:
                return rows

        next_token = page.get("NextToken")
        if not next_token:
            return rows


@tool
def list_tables(database: str = ATHENA_DATABASE, limit: int = 50) -> str:
    """List Glue tables in a database."""
    paginator = glue.get_paginator("get_tables")
    names: list[str] = []

    for page in paginator.paginate(DatabaseName=database):
        for table in page["TableList"]:
            names.append(table["Name"])
            if len(names) >= limit:
                return json.dumps({"database": database, "tables": names}, indent=2)

    return json.dumps({"database": database, "tables": names}, indent=2)


@tool
def describe_table(table_name: str = ATHENA_TABLE, database: str = ATHENA_DATABASE) -> str:
    """Describe a Glue table schema."""
    if not table_name:
        raise ValueError("table_name is required")

    table = glue.get_table(DatabaseName=database, Name=table_name)["Table"]
    cols = table["StorageDescriptor"].get("Columns", [])
    parts = table.get("PartitionKeys", [])

    return json.dumps(
        {
            "database": database,
            "table": table_name,
            "columns": [{"name": c["Name"], "type": c["Type"]} for c in cols],
            "partitions": [{"name": c["Name"], "type": c["Type"]} for c in parts],
            "location": table["StorageDescriptor"].get("Location"),
        },
        indent=2,
    )


@tool
def run_athena_query(sql: str, max_rows: int = 200) -> str:
    """Run a read-only Athena query and return JSON rows."""
    allowed_prefixes = ("select", "with", "show", "describe")
    if not sql.lstrip().lower().startswith(allowed_prefixes):
        raise ValueError("Only read-only queries are allowed in this deployment.")

    rows = _run_athena_query(sql=sql, max_rows=max_rows)
    return json.dumps(rows, indent=2)


def build_agent() -> Agent:
    extra_rules = """
You have direct tools for:
- listing Glue tables
- describing table schemas
- running read-only Athena SQL

Use describe_table before querying if the schema is unclear.
Prefer concise answers.
When reporting numbers, explain how you derived them.
"""

    return Agent(
        model=model,
        tools=[list_tables, describe_table, run_athena_query],
        system_prompt=SYSTEM_PROMPT + "\n\n" + extra_rules,
    )


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict) and "text" in content:
        return str(content["text"])
    return ""


def _prompt_from_payload(payload: dict[str, Any]) -> str:
    if payload.get("prompt"):
        return str(payload["prompt"])

    input_obj = payload.get("input")
    if isinstance(input_obj, dict) and input_obj.get("prompt"):
        return str(input_obj["prompt"])

    messages = payload.get("messages", [])
    transcript = []
    for msg in messages:
        role = msg.get("role", "user").upper()
        text = _text_from_content(msg.get("content"))
        if text:
            transcript.append(f"{role}: {text}")
    return "\n\n".join(transcript)


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    prompt = _prompt_from_payload(payload or {})
    if not prompt:
        text = "No prompt provided."
    else:
        agent = build_agent()
        result = agent(prompt)
        text = extract_text(result) or str(result)

    session_id = getattr(context, "session_id", None) or str(uuid.uuid4())

    return {
        "sessionId": session_id,
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
    }


if __name__ == "__main__":
    app.run()