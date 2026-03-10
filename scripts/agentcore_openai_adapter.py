#!/usr/bin/env python3
"""OpenAI-compatible adapter for an Amazon Bedrock AgentCore Runtime."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def _region_from_arn(arn: str) -> str:
    parts = arn.split(":")
    if len(parts) > 3 and parts[3]:
        return parts[3]
    return "eu-central-1"


@dataclass(frozen=True)
class AdapterConfig:
    runtime_arn: str
    region: str
    host: str
    port: int
    model_id: str
    api_key: str | None
    qualifier: str | None


def _load_config() -> AdapterConfig:
    runtime_arn = os.getenv("AGENT_RUNTIME_ARN", "").strip()
    if not runtime_arn:
        raise RuntimeError("AGENT_RUNTIME_ARN is required")

    region = os.getenv("AWS_REGION", "").strip() or _region_from_arn(runtime_arn)
    host = os.getenv("ADAPTER_HOST", "0.0.0.0").strip()
    port = int(os.getenv("ADAPTER_PORT", "8800"))
    model_id = os.getenv("AGENTCORE_ADAPTER_MODEL_ID", "agentcore-runtime").strip()
    api_key = os.getenv("AGENTCORE_ADAPTER_API_KEY", "").strip() or None
    qualifier = os.getenv("AGENTCORE_QUALIFIER", "").strip() or None

    return AdapterConfig(
        runtime_arn=runtime_arn,
        region=region,
        host=host,
        port=port,
        model_id=model_id,
        api_key=api_key,
        qualifier=qualifier,
    )


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join([p for p in parts if p])
    return ""


def _to_agent_payload(request_json: dict[str, Any]) -> dict[str, Any]:
    messages = request_json.get("messages")
    if isinstance(messages, list):
        normalized_messages: list[dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "user"))
            text = _extract_text(msg.get("content"))
            if text:
                normalized_messages.append({"role": role, "content": text})
        if normalized_messages:
            return {"messages": normalized_messages}

    prompt = request_json.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return {"prompt": prompt}

    return {"prompt": ""}


def _extract_agent_output_text(response_json: dict[str, Any]) -> str:
    output = response_json.get("output")
    if not isinstance(output, dict):
        return json.dumps(response_json, ensure_ascii=False)

    message = output.get("message")
    if not isinstance(message, dict):
        return json.dumps(response_json, ensure_ascii=False)

    text = _extract_text(message.get("content"))
    return text or json.dumps(response_json, ensure_ascii=False)


def _build_openai_completion(model: str, text: str, completion_id: str, created: int) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class OpenAIAdapterHandler(BaseHTTPRequestHandler):
    config: AdapterConfig
    runtime_client: Any

    server_version = "AgentCoreOpenAIAdapter/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} {self.address_string()} - {fmt % args}")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str, code: str = "adapter_error") -> None:
        self._send_json(
            status,
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error" if status < 500 else "server_error",
                    "code": code,
                }
            },
        )

    def _is_authorized(self) -> bool:
        api_key = self.config.api_key
        if not api_key:
            return True

        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {api_key}"
        if auth == expected:
            return True

        self._send_error_json(401, "Invalid API key", code="invalid_api_key")
        return False

    def _parse_json_body(self) -> dict[str, Any] | None:
        content_length = self.headers.get("Content-Length", "").strip()
        if not content_length:
            self._send_error_json(400, "Missing request body")
            return None
        try:
            size = int(content_length)
        except ValueError:
            self._send_error_json(400, "Invalid Content-Length")
            return None

        body = self.rfile.read(size)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_error_json(400, "Invalid JSON body")
            return None

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._is_authorized():
            return

        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return

        if path == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": self.config.model_id,
                            "object": "model",
                            "created": 0,
                            "owned_by": "agentcore",
                        }
                    ],
                },
            )
            return

        self._send_error_json(404, f"Unknown path: {path}", code="not_found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._is_authorized():
            return

        if path != "/v1/chat/completions":
            self._send_error_json(404, f"Unknown path: {path}", code="not_found")
            return

        request_json = self._parse_json_body()
        if request_json is None:
            return

        requested_model = request_json.get("model")
        model = requested_model if isinstance(requested_model, str) and requested_model else self.config.model_id
        stream = bool(request_json.get("stream"))

        invoke_kwargs: dict[str, Any] = {
            "agentRuntimeArn": self.config.runtime_arn,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": json.dumps(_to_agent_payload(request_json)).encode("utf-8"),
        }
        if self.config.qualifier:
            invoke_kwargs["qualifier"] = self.config.qualifier

        runtime_user = request_json.get("user")
        if isinstance(runtime_user, str) and runtime_user:
            invoke_kwargs["runtimeUserId"] = runtime_user[:1024]

        try:
            response = self.runtime_client.invoke_agent_runtime(**invoke_kwargs)
            raw = response["response"].read()
            payload_json = json.loads(raw.decode("utf-8"))
            text = _extract_agent_output_text(payload_json)
        except (ClientError, BotoCoreError, KeyError, json.JSONDecodeError) as exc:
            self._send_error_json(502, f"AgentCore invoke failed: {exc}", code="agentcore_invoke_failed")
            return

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if stream:
            first_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            }
            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        self._send_json(200, _build_openai_completion(model=model, text=text, completion_id=completion_id, created=created))


def main() -> None:
    config = _load_config()
    runtime_client = boto3.client("bedrock-agentcore", region_name=config.region)

    OpenAIAdapterHandler.config = config
    OpenAIAdapterHandler.runtime_client = runtime_client

    server = ThreadingHTTPServer((config.host, config.port), OpenAIAdapterHandler)
    print("AgentCore OpenAI adapter is running")
    print(f"  host: {config.host}")
    print(f"  port: {config.port}")
    print(f"  model id: {config.model_id}")
    print(f"  runtime arn: {config.runtime_arn}")
    print(f"  region: {config.region}")
    server.serve_forever()


if __name__ == "__main__":
    main()
