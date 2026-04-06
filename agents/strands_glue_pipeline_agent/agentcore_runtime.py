import os
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent import make_agent, make_mcp_client
from utils.utils import extract_text

app = BedrockAgentCoreApp()

AWS_REGION = os.environ["AWS_REGION"]
MODEL_ID = os.environ["MODEL_ID"]


def _text_from_content(content: Any) -> str:
    """
    Convert mixed message content into plain text.

    :param content: AgentCore message content payload
    :return: Joined text representation
    """
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
    """
    Extract a prompt string from AgentCore runtime payload.

    :param payload: Runtime request payload
    :return: Prompt text or empty string
    """
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


def _append_utc_note(text: str) -> str:
    """
    Ensure user-facing output includes an explicit UTC timezone reminder.

    :param text: Agent response text
    :return: Response with UTC note if missing
    """
    stripped = (text or "").strip()
    note = "Timezone note: all Glue schedule and cron times are interpreted in UTC."
    if not stripped:
        return note
    if "UTC" in stripped.upper():
        return stripped
    return f"{stripped}\n\n{note}"


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AgentCore runtime entrypoint.

    :param payload: Runtime request payload
    :param context: Runtime invocation context
    :return: AgentCore-compatible response
    """
    prompt = _prompt_from_payload(payload or {})
    if not prompt:
        text = _append_utc_note("No prompt provided.")
    else:
        mcp_client = make_mcp_client()
        try:
            with mcp_client:
                agent, _, _ = make_agent(
                    profile=os.environ.get("AWS_PROFILE"),
                    region=AWS_REGION,
                    model_id=MODEL_ID,
                    mcp_client=mcp_client,
                    enable_code_interpreter=False,
                )
                result = agent(prompt)
                text = _append_utc_note(extract_text(result) or str(result))
        finally:
            try:
                mcp_client.stop(None, None, None)
            except Exception:
                # Runtime response should not fail due to cleanup errors.
                pass

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
