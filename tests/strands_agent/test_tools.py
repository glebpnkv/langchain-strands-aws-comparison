import json
from unittest.mock import MagicMock

from utils.tools import make_athena_query_to_ci_csv


def _file_content_factory(path: str, text: str) -> dict:
    return {"path": path, "text": text}


def _write_files_action_factory(type: str, content: list[dict]) -> dict:
    return {"type": type, "content": content}


def _list_files_action_factory(type: str, path: str) -> dict:
    return {"type": type, "path": path}


def _build_tool(athena_client: MagicMock, code_interpreter_tool: MagicMock):
    session = MagicMock()
    session.client.return_value = athena_client
    return make_athena_query_to_ci_csv(
        session=session,
        region="us-east-1",
        database="iris_demo",
        code_interpreter_tool=code_interpreter_tool,
        ci_session_name="ci-test",
        tool_decorator=lambda fn: fn,
        file_content_factory=_file_content_factory,
        write_files_action_factory=_write_files_action_factory,
        list_files_action_factory=_list_files_action_factory,
    )


def test_athena_query_to_ci_csv_rejects_empty_sql():
    athena = MagicMock()
    ci_tool = MagicMock()
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="   "))
    assert result["ok"] is False
    assert result["error"] == "Empty SQL"


def test_athena_query_to_ci_csv_rejects_non_read_only_sql():
    athena = MagicMock()
    ci_tool = MagicMock()
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="DELETE FROM iris_demo.iris"))
    assert result["ok"] is False
    assert "SELECT/WITH" in result["error"]


def test_athena_query_to_ci_csv_handles_failed_query_state():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": "syntax error"}}
    }

    ci_tool = MagicMock()
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT * FROM iris", poll_seconds=0, timeout_seconds=1))
    assert result["ok"] is False
    assert result["error"] == "Athena query FAILED"
    assert result["reason"] == "syntax error"
    assert result["query_execution_id"] == "qid-1"


def test_athena_query_to_ci_csv_handles_timeout():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-timeout"}

    ci_tool = MagicMock()
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT * FROM iris", poll_seconds=0, timeout_seconds=0))
    assert result["ok"] is False
    assert result["error"] == "Athena query timeout"
    assert result["query_execution_id"] == "qid-timeout"


def test_athena_query_to_ci_csv_success_path_writes_csv():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-success"}
    athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    athena.get_query_results.return_value = {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "species"}, {"Name": "sepal_length"}]},
            "Rows": [
                {"Data": [{"VarCharValue": "species"}, {"VarCharValue": "sepal_length"}]},
                {"Data": [{"VarCharValue": "setosa"}, {"VarCharValue": "5.1"}]},
            ],
        }
    }

    ci_tool = MagicMock()
    ci_tool.write_files.return_value = {"status": "success"}
    ci_tool.list_files.return_value = {"status": "success"}
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT species, sepal_length FROM iris;", sandbox_path="data/out.csv"))
    assert result["ok"] is True
    assert result["rows"] == 1
    assert result["columns"] == ["species", "sepal_length"]
    assert result["sql_executed"] == "SELECT * FROM (SELECT species, sepal_length FROM iris) AS t"

    write_action = ci_tool.write_files.call_args[0][0]
    written_file = write_action["content"][0]
    assert written_file["path"] == "data/out.csv"
    assert "species,sepal_length" in written_file["text"]
    assert "setosa,5.1" in written_file["text"]


def test_athena_query_to_ci_csv_handles_pagination_and_header_row():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-pages"}
    athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    athena.get_query_results.side_effect = [
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "species"}, {"Name": "n"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "species"}, {"VarCharValue": "n"}]},
                    {"Data": [{"VarCharValue": "setosa"}, {"VarCharValue": "50"}]},
                ],
            },
            "NextToken": "token-2",
        },
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "species"}, {"Name": "n"}]},
                "Rows": [{"Data": [{"VarCharValue": "versicolor"}, {"VarCharValue": "50"}]}],
            }
        },
    ]

    ci_tool = MagicMock()
    ci_tool.write_files.return_value = {"status": "success"}
    ci_tool.list_files.return_value = {"status": "success"}
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT species, n FROM counts", poll_seconds=0, timeout_seconds=1))
    assert result["ok"] is True
    assert result["rows"] == 2
    assert result["preview_rows"][0]["species"] == "setosa"
    assert result["preview_rows"][1]["species"] == "versicolor"


def test_athena_query_to_ci_csv_handles_code_interpreter_write_failure():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-write-fail"}
    athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    athena.get_query_results.return_value = {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "x"}]},
            "Rows": [
                {"Data": [{"VarCharValue": "x"}]},
                {"Data": [{"VarCharValue": "1"}]},
            ],
        }
    }

    ci_tool = MagicMock()
    ci_tool.write_files.side_effect = RuntimeError("sandbox unavailable")
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT 1 AS x", poll_seconds=0, timeout_seconds=1))
    assert result["ok"] is False
    assert "Code interpreter write failed" in result["error"]
    assert result["query_execution_id"] == "qid-write-fail"

