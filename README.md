# langchain-strands-aws-comparison
Comparison of LangChain and Strands along the following dimensions (✅: implemented):

| Dimension                    | LangChain (Deep Agents)                                                   | Strands                                                        | Notes (AWS Bedrock / on-prem)                                                                     |
| ---------------------------- |---------------------------------------------------------------------------|----------------------------------------------------------------| ------------------------------------------------------------------------------------------------- |
| Bedrock LLM support          | `langchain-aws` Bedrock chat models (✅)                                   | Bedrock is default model provider (✅)                          | Both solid for Bedrock-centric stacks ([docs.langchain.com][1])                                   |
| Out-of-box agent             | `create_deep_agent()` strong baseline (✅)                                 | `Agent()` minimal baseline; add tools (✅)                      | Deep Agents feels more “batteries included” by default ([GitHub][3])                              |
| MCP support                  | Not MCP-first in Deep Agents surface, but good adapter (✅)                | First-class MCP client + tools (✅)                             | Strands has an official “few lines” MCP example ([strandsagents.com][4])                          |
| Sandboxed code exec          | ~~AgentCore Code Interpreter toolkit in `langchain-aws`~~ Daytona for now | AgentCore Code Interpreter tool listed in community tools (✅)  | Both can align on managed sandboxing via AgentCore ([docs.langchain.com][5])                      |
| AWS integrations breadth     | Very broad AWS integration catalog                                        | Growing AWS-aligned tool ecosystem (✅)                         | LangChain’s AWS surface area is notably larger today ([docs.langchain.com][5])                    |
| Observability posture        | Strong ecosystem; common tracing/eval tooling                             | Built-in instrumentation + telemetry (OTel-friendly) (✅)       | If OTel everywhere is a hard requirement, Strands may be simpler ([Amazon Web Services, Inc.][9]) |

[1]: https://docs.langchain.com/oss/python/integrations/chat/bedrock?utm_source=chatgpt.com "ChatBedrock - Docs by LangChain"
[2]: https://reference.langchain.com/python/integrations/langchain_openai/ChatOpenAI/?utm_source=chatgpt.com "ChatOpenAI | LangChain Reference"
[3]: https://github.com/langchain-ai/deepagents?utm_source=chatgpt.com "langchain-ai/deepagents: Deep Agents ..."
[4]: https://strandsagents.com/latest/documentation/docs/examples/python/mcp_calculator/ "MCP - Strands Agents"
[5]: https://docs.langchain.com/oss/python/integrations/providers/aws "AWS (Amazon) integrations - Docs by LangChain"
[6]: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop "Human-in-the-loop - Docs by LangChain"
[7]: https://strandsagents.com/latest/documentation/docs/user-guide/safety-security/guardrails/?utm_source=chatgpt.com "Guardrails"
[8]: https://aws.amazon.com/blogs/compute/building-a-serverless-document-chat-with-aws-lambda-and-amazon-bedrock/?utm_source=chatgpt.com "Building a serverless document chat with AWS Lambda ..."
[9]: https://aws.amazon.com/blogs/machine-learning/strands-agents-sdk-a-technical-deep-dive-into-agent-architectures-and-observability/?utm_source=chatgpt.com "Strands Agents SDK: A technical deep dive into ..."
[10]: https://dev.to/aws/strands-agents-now-speaks-typescript-a-side-by-side-guide-12b3?utm_source=chatgpt.com "Strands Agents now speaks TypeScript: A side-by-side guide"


## Initial Setup
### venv setup
Install dependencies using uv:
```bash
uv sync
```
### AWS profile setup
Login to your AWS account using SSO:
```bash
aws sso login
```
### LibreChat setup (one-time)
Run the setup script once from the repository root:
```bash
./scripts/setup_librechat.sh
```

Then manually edit `vendor/LibreChat/.env` and set:
```bash
BEDROCK_AWS_DEFAULT_REGION=eu-central-1
```

### Uploading data sample to Athena
Run `upload_iris_data.py` to upload a sample Iris dataset to Athena in your AWS account:
```bash
source .venv/bin/activate
aws sso login
python scripts/upload_iris_data.py --bucket <bucket_name>
```

## Running `strands-agent`
Set up environment variables:
```bash
cp agents/strands-agent/.env.template agents/strands-agent/.env
# Edit agents/strands-agent/.env with your configuration
```

From the root directory:
```bash
source .venv/bin/activate
aws sso login
python agents/strands-agent/main.py
```

## Running `langchain-agent`
Set up environment variables:
```bash
cp agents/langchain-agent/.env.template agents/langchain-agent/.env
# Edit agents/langchain-agent/.env with your configuration
```

If you use Daytona backend, make sure to add `DAYTONA_API_KEY` to `.env`, then run:
```bash
source .venv/bin/activate
aws sso login
python agents/langchain-agent/main.py
```

Optional: list Athena MCP tools before running:
```bash
python agents/langchain-agent/main.py --list-tools
```

## Running `LibreChat`

```bash
cd vendor/LibreChat
docker compose up -d && docker compose logs -f api
```

## Running LibreChat with Deployed AgentCore Runtime

Start the local OpenAI-compatible adapter (from repo root) in one terminal:

```bash
./scripts/start_agentcore_openai_adapter.sh
```

Then start LibreChat in a second terminal:

```bash
cd vendor/LibreChat
docker compose up -d && docker compose logs -f api
```

In the LibreChat model picker, select `Strands AgentCore Runtime`.

Notes:
- Adapter URL configured in `vendor/LibreChat/librechat.yaml`: `http://host.docker.internal:8800/v1`
- Runtime ARN is auto-read from `agents/strands_agent/.bedrock_agentcore.yaml`
- Adapter API key defaults to `agentcore-local` (matches LibreChat custom endpoint config)
- Override runtime ARN manually if needed:
  - `AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:... ./scripts/start_agentcore_openai_adapter.sh`

### One-command startup (adapter + LibreChat)

You can run both the adapter and LibreChat with one script:

```bash
./scripts/librechat_agentcore_stack.sh up
```

Useful commands:

```bash
./scripts/librechat_agentcore_stack.sh start     # start both, no log follow
./scripts/librechat_agentcore_stack.sh status    # health + docker compose ps
./scripts/librechat_agentcore_stack.sh logs      # follow LibreChat api logs
./scripts/librechat_agentcore_stack.sh down      # stop LibreChat + adapter
```

If you only want to start services without log follow:

```bash
./scripts/librechat_agentcore_stack.sh up --no-follow
```
