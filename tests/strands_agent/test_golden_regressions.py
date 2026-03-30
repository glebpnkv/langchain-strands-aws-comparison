import json
from unittest.mock import MagicMock

from utils.tools import make_athena_query_to_ci_csv


def _build_tool_with_single_page(page: dict):
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-golden"}
    athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    athena.get_query_results.return_value = page

    session = MagicMock()
    session.client.return_value = athena

    ci_tool = MagicMock()
    ci_tool.write_files.return_value = {"status": "success"}
    ci_tool.list_files.return_value = {"status": "success"}

    tool = make_athena_query_to_ci_csv(
        session=session,
        region="us-east-1",
        database="iris_demo",
        code_interpreter_tool=ci_tool,
        ci_session_name="ci-golden",
        tool_decorator=lambda fn: fn,
        file_content_factory=lambda path, text: {"path": path, "text": text},
        write_files_action_factory=lambda type, content: {"type": type, "content": content},
        list_files_action_factory=lambda type, path: {"type": type, "path": path},
    )
    return tool


def test_golden_row_count_for_iris_is_150():
    tool = _build_tool_with_single_page(
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "row_count"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "row_count"}]},
                    {"Data": [{"VarCharValue": "150"}]},
                ],
            }
        }
    )

    result = json.loads(tool(sql="SELECT COUNT(*) AS row_count FROM iris", poll_seconds=0, timeout_seconds=1))
    assert result["ok"] is True
    assert result["preview_rows"][0]["row_count"] == 150


def test_golden_simple_aggregation_avg_sepal_length():
    tool = _build_tool_with_single_page(
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "avg_sepal_length"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "avg_sepal_length"}]},
                    {"Data": [{"VarCharValue": "5.8433333333"}]},
                ],
            }
        }
    )

    result = json.loads(
        tool(
            sql="SELECT AVG(sepal_length) AS avg_sepal_length FROM iris",
            poll_seconds=0,
            timeout_seconds=1,
        )
    )
    assert result["ok"] is True
    assert abs(result["preview_rows"][0]["avg_sepal_length"] - 5.8433333333) < 1e-9

