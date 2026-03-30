import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd


def _identity_decorator(func: Callable[..., str]) -> Callable[..., str]:
    return func


def _resolve_tool_decorator(
    tool_decorator: Callable[[Callable[..., str]], Callable[..., str]] | None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    if tool_decorator is not None:
        return tool_decorator
    try:
        from strands import tool as strands_tool

        return strands_tool
    except Exception:
        return _identity_decorator


def _resolve_ci_factories(
    file_content_factory: Callable[..., Any] | None,
    write_files_action_factory: Callable[..., Any] | None,
    list_files_action_factory: Callable[..., Any] | None,
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    if file_content_factory and write_files_action_factory and list_files_action_factory:
        return file_content_factory, write_files_action_factory, list_files_action_factory

    from strands_tools.code_interpreter.models import FileContent, ListFilesAction, WriteFilesAction

    return (
        file_content_factory or FileContent,
        write_files_action_factory or WriteFilesAction,
        list_files_action_factory or ListFilesAction,
    )


def make_athena_query_to_ci_csv(
    session: Any,
    region: str,
    database: str,
    code_interpreter_tool: Any,
    ci_session_name: str,
    workgroup: str = "primary",
    tool_decorator: Callable[[Callable[..., str]], Callable[..., str]] | None = None,
    file_content_factory: Callable[..., Any] | None = None,
    write_files_action_factory: Callable[..., Any] | None = None,
    list_files_action_factory: Callable[..., Any] | None = None,
) -> Callable[..., str]:
    """
    Build a tool that runs read-only Athena SQL and writes results into code interpreter sandbox as CSV.
    """
    tool_wrap = _resolve_tool_decorator(tool_decorator)
    file_content, write_files_action, list_files_action = _resolve_ci_factories(
        file_content_factory=file_content_factory,
        write_files_action_factory=write_files_action_factory,
        list_files_action_factory=list_files_action_factory,
    )

    @tool_wrap
    def athena_query_to_ci_csv(
        sql: str,
        sandbox_path: str = "data/iris_query.csv",
        poll_seconds: float = 0.5,
        timeout_seconds: int = 60,
    ) -> str:
        """
        Execute a read-only Athena query and upload the result as CSV into the AgentCore sandbox.
        """
        raw_sql = (sql or "").strip().rstrip(";")
        if not raw_sql:
            return json.dumps({"ok": False, "error": "Empty SQL"})

        if not re.match(r"(?is)^\s*(select|with)\b", raw_sql):
            return json.dumps({"ok": False, "error": "Only read-only SELECT/WITH queries are allowed."})

        wrapped_sql = f"SELECT * FROM ({raw_sql}) AS t"
        athena = session.client("athena", region_name=region)

        try:
            start = athena.start_query_execution(
                QueryString=wrapped_sql,
                QueryExecutionContext={"Database": database, "Catalog": "AwsDataCatalog"},
                WorkGroup=workgroup,
            )
            qid = start["QueryExecutionId"]

            deadline = time.time() + timeout_seconds
            state = "QUEUED"
            reason = None
            timed_out = True

            while time.time() < deadline:
                q = athena.get_query_execution(QueryExecutionId=qid)
                status = q["QueryExecution"]["Status"]
                state = status["State"]
                reason = status.get("StateChangeReason")
                if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    timed_out = False
                    break
                time.sleep(max(poll_seconds, 0.0))

            if timed_out and state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "Athena query timeout",
                        "reason": reason,
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": database,
                        "workgroup": workgroup,
                    }
                )

            if state != "SUCCEEDED":
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Athena query {state}",
                        "reason": reason,
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": database,
                        "workgroup": workgroup,
                    }
                )

            rows: list[list[Any]] = []
            next_token = None
            col_names: list[str] | None = None
            first_page = True

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
                start_idx = 0
                if first_page and page_rows:
                    header_cells = page_rows[0].get("Data", [])
                    header_values = [cell.get("VarCharValue") for cell in header_cells]
                    if header_values == col_names:
                        start_idx = 1

                for row in page_rows[start_idx:]:
                    data = row.get("Data", [])
                    vals = [cell.get("VarCharValue") if i < len(data) else None for i, cell in enumerate(data)]
                    if len(vals) < len(col_names):
                        vals += [None] * (len(col_names) - len(vals))
                    rows.append(vals[: len(col_names)])

                first_page = False
                next_token = resp.get("NextToken")
                if not next_token:
                    break

            df = pd.DataFrame(rows, columns=col_names or [])
            for column in df.columns:
                try:
                    df[column] = pd.to_numeric(df[column])
                except (TypeError, ValueError):
                    pass

            csv_text = df.to_csv(index=False)

            try:
                write_result = code_interpreter_tool.write_files(
                    write_files_action(
                        type="writeFiles",
                        content=[file_content(path=sandbox_path, text=csv_text)],
                    )
                )
                list_result = code_interpreter_tool.list_files(
                    list_files_action(
                        type="listFiles",
                        path=str(Path(sandbox_path).parent),
                    )
                )
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Code interpreter write failed: {exc}",
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": database,
                        "workgroup": workgroup,
                        "sandbox_path": sandbox_path,
                        "ci_session_name": ci_session_name,
                    }
                )

            return json.dumps(
                {
                    "ok": True,
                    "query_execution_id": qid,
                    "database": database,
                    "workgroup": workgroup,
                    "sql_executed": wrapped_sql,
                    "sandbox_path": sandbox_path,
                    "rows": int(len(df)),
                    "columns": list(df.columns),
                    "preview_rows": df.head(5).to_dict(orient="records"),
                    "ci_session_name": ci_session_name,
                    "write_result": write_result,
                    "list_result": list_result,
                },
                default=str,
            )

        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "sql_attempted": wrapped_sql,
                    "database": database,
                    "workgroup": workgroup,
                }
            )

    return athena_query_to_ci_csv
