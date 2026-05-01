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

## Running `strands_glue_pipeline_agent` (Chainlit chat frontend)

The headliner agent runs as a FastAPI service behind a Chainlit chat UI, with
Phoenix for LLM tracing. **One script brings up the whole stack:**

```bash
aws sso login
./scripts/run_local_stack.sh
```

| Surface         | URL                       |
|-----------------|---------------------------|
| Chainlit (chat) | http://127.0.0.1:8000     |
| Phoenix (UI)    | http://127.0.0.1:6006     |
| Agent API       | http://127.0.0.1:8080     |

`Ctrl-C` in the same terminal stops everything cleanly. Phoenix data persists
across restarts in a named docker volume; the agent and frontend each write
logs to `runs/<service>/<timestamp>/` (see [Observability](#observability) below).

To skip Phoenix or use a hosted backend (Langfuse, Honeycomb, anything OTLP-shaped):

```bash
SKIP_PHOENIX=1 OTEL_EXPORTER_OTLP_ENDPOINT=https://your-collector \
  AGENT_OTLP_ENABLE=1 ./scripts/run_local_stack.sh
```

This branch (`feature/chainlit-ui`) is active work-in-progress — see
[PLAN.md](PLAN.md) for the phase roadmap and commit links.

### Debugging individual services

When a piece is misbehaving and you want to iterate on just it, the local-stack
script is overkill. Bring up only what you need:

```bash
./scripts/run_agent_local.sh strands_glue_pipeline_agent   # agent FastAPI
./scripts/run_frontend_local.sh                            # Chainlit
```

These omit Phoenix entirely — set `OTEL_EXPORTER_OTLP_ENDPOINT` and
`AGENT_OTLP_ENABLE=1` yourself if you want OTLP export.

### Deploying to ECS

Infrastructure lives under [`infra/`](infra/) (CDK Python). Application
images are built + pushed by `scripts/deploy_*.sh`.

**One-time per AWS account + region** before the first deploy:

```bash
cd infra
cdk bootstrap aws://<account-id>/<region>
```

(account-id from `aws sts get-caller-identity --query Account --output text`).
This creates a small set of supporting resources CDK needs (S3 bucket for
assets, IAM roles for deployments). Costs are ~$0/month idle.

**First deploy** — full sequence:

```bash
aws sso login

# 1. Provision the infra. Network -> Data -> Ecr -> Compute, in dependency
#    order. Both ECS services come up at desiredCount=0 (no images yet).
cd infra && cdk deploy --all && cd -

# 2. Populate the GitHub PAT secret (only needed once; re-run only on rotation).
aws secretsmanager put-secret-value \
  --secret-id GlueAgent/Dev/GithubPat \
  --secret-string "<your-fine-grained-pat>"

# 3. Build, push, and roll out the container images. The single
#    deploy.sh takes which service to ship; `all` does both, agent
#    first. It tags by short git SHA + :latest, pushes both tags, and
#    bumps desiredCount from 0 to 1 on the first run.
./scripts/deploy.sh all
```

**Subsequent deploys** of code changes — re-run the script targeting
just what you changed:

```bash
./scripts/deploy.sh agent       # only the agent FastAPI service
./scripts/deploy.sh frontend    # only the Chainlit frontend
./scripts/deploy.sh all         # both
```

Each invocation rolls the service over to the new image with
`--force-new-deployment` and waits for ECS to report it stable. Tag
includes a `-dirty` suffix when the working tree has uncommitted
changes, so a running task's tag is always traceable.

To stop paying for the deployed stack when you're not demoing:

```bash
./scripts/teardown_dev_stack.sh
```

See [`infra/README.md`](infra/README.md) for the full setup walkthrough,
the cost ballpark per stack, and [PLAN.md](PLAN.md) for phase status.

### Known limitations (Phase 3b persistence)

- **Resumed threads can't continue the conversation.** Refreshing the page or
  reopening a thread from the sidebar restores the displayed history from
  Postgres, but the agent service is unaware of the resumed thread —
  follow-up questions land on a fresh agent session with no prior context.
  Two pieces still need wiring: (a) `@cl.on_chat_resume` on the frontend to
  link the resumed thread back to its `agent_session_id`, and (b) the agent
  service either becoming stateless (frontend ships full message history per
  request) or persisting `SessionRegistry` state across restarts. Tracked for
  a future phase; for now treat resumed threads as read-only history.
- **Inline element bytes don't replay on resume.** `cl.Image` / `cl.Plotly` /
  `cl.Dataframe` content for resumed threads needs an S3-backed
  `storage_provider` on `SQLAlchemyDataLayer`; without one, resumed threads
  show the structure (text, tool steps) but not the rendered charts/tables.
  Comes with the CDK work in Phase 4.

### Architecture

```
Browser ──▶ Chainlit  ──HTTP+SSE──▶ FastAPI agent service  ──▶ Bedrock / Athena / Glue / sandbox
   :8000                              :8080                       (your AWS account)
```

- `frontend/` — Chainlit chat UI. Consumes the `v1` SSE protocol (`text.delta`,
  `tool.start/end`, `ui.dataframe`, `ui.plotly`, `ui.image`, `done`, `error`)
  and renders tool steps, tables (`cl.Dataframe`), Plotly charts (`cl.Plotly`),
  and images (`cl.Image`) inline in the chat.
- `agent_server/` — shared FastAPI scaffold: routing, auth middleware, SSE
  plumbing, in-memory session registry, Strands→SSE event reducer, and the
  `display_dataframe` / `display_plotly` / `display_image` tool factories.
  Adding a new agent is a ~30-line `server/main.py` that supplies an agent
  factory and reuses everything else.
- `agents/<agent>/server/main.py` — the per-agent thin wrapper. The
  `strands_glue_pipeline_agent` one wires up sandbox text and image loaders
  so display tools resolve sandbox file paths server-side rather than
  pulling bytes through the LLM context.

### Observability

Every boot of the agent service writes to a fresh per-process directory:

```
runs/<service-name>/<YYYYmmddTHHMMSS>/
├── server.log              # all logs at AGENT_LOG_LEVEL (default INFO)
└── strands_traces.jsonl    # Strands' OpenTelemetry spans, one JSON per line
```

When `AGENT_OTLP_ENABLE=1` (the local-stack script sets this), spans are also
pushed to `OTEL_EXPORTER_OTLP_ENDPOINT` over OTLP HTTP/protobuf — Phoenix by
default. The `openinference-instrumentation-bedrock` package emits
OpenInference-conformant LLM child spans nested under Strands' agent-level
parents, so Phoenix's UI shows model name, prompt / completion / total token
counts, and computed cost per call.

**Persistence.** Phoenix stores its SQLite DB inside the named docker volume
`phoenix-agent-traces-data`, so traces persist across restarts of the local
stack. To wipe history:

```bash
docker volume rm phoenix-agent-traces-data
```

To swap Phoenix for a hosted backend (Langfuse, Honeycomb, anything OTLP-shaped),
unset `SKIP_PHOENIX` and just override `OTEL_EXPORTER_OTLP_ENDPOINT` /
`OTEL_EXPORTER_OTLP_HEADERS` — the agent has no hard dependency on Phoenix.

### Useful env knobs

| Variable                       | Default                  | Purpose |
|--------------------------------|--------------------------|---------|
| `AGENT_LOG_LEVEL`              | `INFO`                   | Set to `DEBUG` to surface httpx/botocore noise |
| `AGENT_RUN_DIR`                | `runs`                   | Base dir for per-process subdirs |
| `AGENT_OTLP_ENABLE`            | unset                    | `1` to enable OTLP export |
| `OTEL_EXPORTER_OTLP_ENDPOINT`  | unset                    | OTLP HTTP endpoint, e.g. `http://127.0.0.1:6006` |
| `AGENT_SERVICE_AUTH_SECRET`    | unset                    | Shared secret between Chainlit and the agent. Unset = unauthenticated local dev. |
| `PHOENIX_PORT`                 | `6006`                   | Phoenix UI + OTLP receiver |
| `AGENT_PORT` / `FRONTEND_PORT` | `8080` / `8000`          | If you need to dodge a port collision |

## Exposing the Deployed AgentCore Runtime as an OpenAI-compatible API

The local adapter wraps the deployed AgentCore runtime in an OpenAI-compatible HTTP API, usable from any OpenAI-style client (curl, Open WebUI, custom code, etc.).

Run in foreground:

```bash
./scripts/start_agentcore_openai_adapter.sh
```

Or run as a background daemon with healthcheck and PID management:

```bash
./scripts/agentcore_adapter_daemon.sh up      # start in background, wait for health
./scripts/agentcore_adapter_daemon.sh status  # check health + PID
./scripts/agentcore_adapter_daemon.sh down    # stop
```

Notes:
- Default URL: `http://127.0.0.1:8800/v1`
- Runtime ARN is auto-read from `agents/strands_agent/.bedrock_agentcore.yaml`
- Adapter API key defaults to `agentcore-local`
- Override runtime ARN manually if needed:
  - `AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:... ./scripts/start_agentcore_openai_adapter.sh`
