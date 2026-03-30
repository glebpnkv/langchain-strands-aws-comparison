import json
from pathlib import Path
from types import SimpleNamespace

import main


def _make_args(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        profile="test-profile",
        region="us-east-1",
        model_id="test-model",
        database="iris_demo",
        table="iris",
        prompt="Count rows and summarize.",
        run_dir=str(run_dir),
        enable_otlp=False,
        otel_endpoint=None,
        list_tools=False,
    )


def _fake_setup_observability(run_dir: str, enable_otlp: bool = False, otel_endpoint: str | None = None):
    del enable_otlp, otel_endpoint
    obs_dir = Path(run_dir) / "observability"
    artifacts_dir = Path(run_dir) / "artifacts"
    obs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (obs_dir / "traces.jsonl").write_text("", encoding="utf-8")
    return obs_dir, artifacts_dir


def test_make_mcp_client_smoke_sets_expected_env_and_allowed_tools(monkeypatch):
    captured = {}

    class FakeStdioServerParameters:
        def __init__(self, command, args, env):
            self.command = command
            self.args = args
            self.env = env

    def fake_stdio_client(params):
        captured["stdio_params"] = params
        return object()

    class FakeMCPClient:
        def __init__(self, transport_callable, **kwargs):
            self.transport_callable = transport_callable
            self.kwargs = kwargs
            captured["client_kwargs"] = kwargs
            captured["transport_callable"] = transport_callable

    monkeypatch.setenv("AWS_PROFILE", "my-profile")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(main, "StdioServerParameters", FakeStdioServerParameters)
    monkeypatch.setattr(main, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(main, "MCPClient", FakeMCPClient)

    client = main.make_mcp_client()
    assert isinstance(client, FakeMCPClient)

    captured["transport_callable"]()
    params = captured["stdio_params"]
    assert params.command == "uvx"
    assert params.args == ["awslabs.aws-dataprocessing-mcp-server@latest", "--allow-write"]
    assert params.env["AWS_PROFILE"] == "my-profile"
    assert params.env["AWS_REGION"] == "us-west-2"

    allowed_tools = captured["client_kwargs"]["tool_filters"]["allowed"]
    assert allowed_tools == [
        "manage_aws_athena_databases_and_tables",
        "manage_aws_athena_query_executions",
    ]


def test_run_agent_mode_failure_path_mcp_unavailable_is_best_effort(monkeypatch, tmp_path):
    class FakeMCPClient:
        def stop(self, *_args):
            return None

    class FailingAgent:
        def __call__(self, _task):
            raise RuntimeError("MCP server unavailable")

    monkeypatch.setattr(main, "setup_observability", _fake_setup_observability)
    monkeypatch.setattr(main, "make_mcp_client", lambda: FakeMCPClient())
    monkeypatch.setattr(main, "make_agent", lambda **kwargs: (FailingAgent(), "ci-session", object()))

    args = _make_args(tmp_path)
    exit_code = main.run_agent_mode(args)

    final_answer_path = tmp_path / "observability" / "final_answer.txt"
    assert exit_code == 0
    assert final_answer_path.exists()
    text = final_answer_path.read_text(encoding="utf-8")
    assert "Likely AWS configuration issue" in text
    assert "MCP server unavailable" in text


def test_run_agent_mode_observability_and_metrics_smoke(monkeypatch, tmp_path):
    class FakeMCPClient:
        def stop(self, *_args):
            return None

    class FakeMetrics:
        def get_summary(self):
            return {"tool_calls": 2, "latency_ms": 123}

    class FakeResult:
        message = {
            "content": [
                {
                    "text": "Plan:\n- Query count\n- Summarize\n\nSQL used: SELECT COUNT(*) AS row_count FROM iris;"
                }
            ]
        }
        metrics = FakeMetrics()

    class FakeAgent:
        def __call__(self, _task):
            return FakeResult()

    monkeypatch.setattr(main, "setup_observability", _fake_setup_observability)
    monkeypatch.setattr(main, "make_mcp_client", lambda: FakeMCPClient())
    monkeypatch.setattr(main, "make_agent", lambda **kwargs: (FakeAgent(), "ci-session", object()))
    monkeypatch.setattr(main, "extract_artifacts_from_sandbox", lambda *_args, **_kwargs: None)

    args = _make_args(tmp_path)
    exit_code = main.run_agent_mode(args)

    obs_dir = tmp_path / "observability"
    metrics_path = obs_dir / "metrics_summary.json"
    traces_path = obs_dir / "traces.jsonl"

    assert exit_code == 0
    assert traces_path.exists()
    assert metrics_path.exists()
    metrics_summary = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics_summary["tool_calls"] == 2

