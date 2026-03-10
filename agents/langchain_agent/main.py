from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent import create_langchain_agent, load_athena_mcp_tools, make_backend


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LangChain Deep Agent (Bedrock + Athena MCP + sandbox backend)")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"), help="AWS profile name used with aws sso login")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"), help="AWS region (e.g., us-east-1)")
    parser.add_argument(
        "--model-id",
        default=os.environ.get("MODEL_ID"),
        help="Bedrock model ID (e.g., us.anthropic.claude-sonnet-4-20250514-v1:0)",
    )
    parser.add_argument("--database", default="iris_demo", help="Athena/Glue database")
    parser.add_argument("--table", default="iris", help="Athena table")
    parser.add_argument(
        "--prompt",
        default="Analyze the iris dataset and produce a short summary and one useful plot.",
        help="User request for the agent",
    )
    parser.add_argument("--list-tools", action="store_true", help="List available Athena MCP tools and exit")
    parser.add_argument(
        "--backend",
        choices=["daytona", "local-shell"],
        default=os.environ.get("SANDBOX_BACKEND", "daytona"),
        help="Sandbox backend for deepagents execution tools",
    )
    parser.add_argument(
        "--local-shell-root",
        default=str(Path.cwd()),
        help="Root directory used when --backend=local-shell",
    )
    parser.add_argument("--run-dir", default="runs", help="Base directory for logs/results/artifacts")
    parser.add_argument(
        "--remote-artifacts-dir",
        default="/tmp/artifacts",
        help="Remote sandbox directory to pull generated files from after run",
    )
    parser.add_argument("--debug", action="store_true", help="Enable deepagents debug mode")
    return parser.parse_args()


def setup_run_dirs(run_dir: str) -> tuple[Path, Path]:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_root = Path(run_dir) / "langchain-agent" / ts
    obs_dir = run_root / "observability"
    artifacts_dir = run_root / "artifacts"
    obs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return obs_dir, artifacts_dir


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text = content.get("text")
        if text:
            return str(text)
    return str(content)


def extract_final_answer(result: Any) -> str:
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                msg_type = getattr(msg, "type", None) or (msg.get("type") if isinstance(msg, dict) else None)
                if msg_type in {"ai", "assistant"}:
                    content = getattr(msg, "content", None)
                    if content is None and isinstance(msg, dict):
                        content = msg.get("content")
                    return extract_text_from_content(content)
        if "output" in result:
            return extract_text_from_content(result.get("output"))
    return str(result)


def make_task(prompt: str, database: str, table: str) -> str:
    return f"""
User task:
{prompt}

Dataset location:
- Athena database: {database}
- Athena table: {table}

Please:
- start with a short plan,
- inspect schema if needed,
- run Athena SQL via MCP,
- use sandbox execution for stats/plots,
- move query results with athena_query_to_backend_csv before Python analysis,
- return concise analysis with SQL used.
""".strip()


def extract_backend_artifacts(
    backend: Any,
    artifacts_dir: Path,
    remote_artifacts_dir: str,
) -> list[str]:
    command = f"find {shlex.quote(remote_artifacts_dir)} -type f 2>/dev/null || true"
    result = backend.execute(command)
    paths = [line.strip() for line in result.output.splitlines() if line.strip().startswith("/")]
    if not paths:
        return []

    downloaded = backend.download_files(paths)
    saved: list[str] = []
    for item in downloaded:
        if item.error or item.content is None:
            continue
        local_path = artifacts_dir / item.path.lstrip("/")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(item.content)
        saved.append(str(local_path))
    return saved


def list_tools_mode(profile: str | None, region: str) -> int:
    tools = asyncio.run(load_athena_mcp_tools(profile=profile, region=region))
    print("Available Athena MCP tools:")
    for tool_obj in tools:
        print(f" - {tool_obj.name}")
    return 0


def run_agent_mode(args: argparse.Namespace) -> int:
    if not args.model_id:
        print("--model-id is required unless using --list-tools", file=sys.stderr)
        return 2
    if not args.region:
        print("--region is required", file=sys.stderr)
        return 2

    obs_dir, artifacts_dir = setup_run_dirs(args.run_dir)
    log_file = obs_dir / "langchain_agent.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stderr)],
    )

    backend_bundle = make_backend(kind=args.backend, local_shell_root=args.local_shell_root)
    try:
        agent, tool_names = create_langchain_agent(
            profile=args.profile,
            region=args.region,
            model_id=args.model_id,
            database=args.database,
            table=args.table,
            backend=backend_bundle.backend,
            backend_name=backend_bundle.name,
            debug=args.debug,
        )
        (obs_dir / "tools_used.txt").write_text("\n".join(tool_names) + "\n", encoding="utf-8")

        task = make_task(args.prompt, database=args.database, table=args.table)
        result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": task}]}))
        final_answer = extract_final_answer(result)
        print(final_answer)
        (obs_dir / "final_answer.txt").write_text(final_answer, encoding="utf-8")

        (obs_dir / "agent_result_raw.json").write_text(
            json.dumps(result, indent=2, default=_json_default),
            encoding="utf-8",
        )

        saved = extract_backend_artifacts(
            backend=backend_bundle.backend,
            artifacts_dir=artifacts_dir,
            remote_artifacts_dir=args.remote_artifacts_dir,
        )
        if saved:
            (obs_dir / "saved_artifacts.txt").write_text("\n".join(saved) + "\n", encoding="utf-8")

        print(f"\n[run observability] {obs_dir}")
        print(f"[run artifacts] {artifacts_dir}")
        return 0
    except Exception as e:
        logging.error(f"Error running agent: {e}", exc_info=True)
        return 1
    finally:
        backend_bundle.cleanup()


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    try:
        args = parse_args()
        if args.list_tools:
            if not args.region:
                print("--region is required for --list-tools", file=sys.stderr)
                return 2
            return list_tools_mode(profile=args.profile, region=args.region)
        return run_agent_mode(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
