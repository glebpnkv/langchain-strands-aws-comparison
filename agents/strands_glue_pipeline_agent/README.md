# Strands Glue Pipeline Agent

A Strands-based agent that builds, tests, and delivers AWS Glue Python Shell jobs. It iterates on jobs in a dev AWS account, commits the production version to a pre-specified GitHub repo, and opens a PR once the wheel-based production run succeeds.

## Requirements

**This agent requires a GitHub fine-grained Personal Access Token.** Startup will fail or the first GitHub tool call will error without one. See [Setup](#setup) below.

Other prerequisites:
- AWS credentials for a dev account (profile configured in `~/.aws/config` or via env vars).
- Python 3.11+ and `uv` (or `pip`) to install dependencies.
- Outbound HTTPS to `api.githubcopilot.com` (the agent uses GitHub's hosted MCP server; no local install).
- Access to Bedrock in the configured region for the chosen model.

## How it works (end-to-end flow)

1. **Input source selection** — the agent chooses between the Athena flow and the raw-S3 flow based on what the user provides (or on `ATHENA_DATABASE` / `RAW_DATA_BUCKET_S3_URI`).
2. **Phase A — scratch iteration and commit**
   - **A.1** The agent writes a loose `.py` script, uploads it to S3, creates a Glue job named `scratch-<conversation-id>-*`, and iterates in the dev AWS account until the run succeeds. The agent creates these scratch jobs directly through MCP.
   - **A.2** On success, the agent lays the code out per the `project-structure` skill, adds or updates the job's entry in `glue-jobs.yaml`, creates a feature branch in the target GitHub repo, and pushes the files.
   - **A.3** The agent **ends the turn** and asks the user to resume once GitHub Actions (`test` → `build-wheels` → `deploy`) has gone green.
3. **Phase B — production verification and PR** (on resume)
   - **B.1** The agent verifies CI is green via the GitHub MCP workflow-run tools. If CI failed, it surfaces the failing job logs and stops.
   - **B.2** The agent looks up the Glue job created by CI (not by the agent) and runs `start-job-run` to verify it works with the deployed wheel. If it fails, the agent surfaces the diagnostics and stops — no PR.
   - **B.3** Only after the verification run succeeds, the agent opens a PR against the default branch. Turn ends. The agent does not watch for merge.

The division of responsibilities is deliberate: **the agent creates Glue jobs only in Phase A.1 (scratch)**. In Phase B, `deploy/deploy.py` in the target repo creates them via boto3 from `glue-jobs.yaml`.

## Setup

### 1. Set up a target code repo for the agent

The agent commits generated job code into a separate GitHub repo that you control — not into this repo. The directory [`target_repo_template/`](./target_repo_template/) in this project is a **reference template**: it holds the canonical layout (`jobs/`, `glue-jobs.yaml`, `deploy/deploy.py`, the `build-and-deploy.yml` CI pipeline) that the agent's `project-structure` skill expects. Treat it as a starter scaffold to copy into a fresh repo, not as something the agent commits to directly.

To bootstrap a target repo:

1. Create an empty GitHub repo (e.g. `your-org/glue-jobs`). This is the repo the agent will push branches and PRs to.
2. Clone it locally, then copy the template scaffold in:

   ```bash
   # From within an empty clone of your target repo:
   rsync -a --exclude='.git' /path/to/langchain-strands-aws-comparison/agents/strands_glue_pipeline_agent/target_repo_template/ ./
   git add .
   git commit -m "Bootstrap repo layout"
   git push
   ```

3. Configure the CI secrets listed in `target_repo_template/README.md` (AWS creds, asset bucket, Glue role ARN). Then push a throwaway branch to confirm the `build-and-deploy.yml` pipeline (`test` → `build-wheels` → `deploy`) goes green end-to-end before running the agent against the repo. A green run on an empty manifest is expected to upload no wheels — that's fine; the first real job push will produce them.

Once that repo exists and CI is green, continue with the PAT in step 2.

### 2. Create a GitHub fine-grained PAT

Fine-grained PATs cannot be created via CLI — GitHub only allows creation through the web UI. Follow these steps exactly:

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. **Token name**: anything memorable (e.g. `strands-glue-pipeline-agent-local`).
3. **Resource owner**: the owner of the target repo from step 1. If the repo is in an organisation, select that org — the org must allow fine-grained PATs (Org Settings → Third-party Access → Personal access tokens).
4. **Expiration**: up to 1 year. Set a calendar reminder to rotate.
5. **Repository access**: choose **Only select repositories** and pick the single target repo from step 1.
6. **Repository permissions** — set exactly these, leave everything else at `No access`:
   - **Contents**: `Read and write`
   - **Metadata**: `Read-only` (required, auto-enabled)
   - **Pull requests**: `Read and write`
7. **Account permissions**: leave all at `No access`.
8. Click **Generate token**. Copy the token immediately — it is shown only once.

### 3. Configure `.env`

Copy the template and fill it in:

```bash
cp agents/strands_glue_pipeline_agent/.env.template agents/strands_glue_pipeline_agent/.env
```

All values in `.env.template` are documented inline. Key variables:

| Variable | Required | Purpose |
|---|---|---|
| `AWS_PROFILE` / `AWS_REGION` | Yes | Dev AWS account credentials. |
| `MODEL_ID` | Yes | Bedrock model ID. |
| `GLUE_JOB_ROLE_ARN` | Yes | IAM role Glue assumes. |
| `GITHUB_PAT` | Yes | The fine-grained PAT from step 2. |
| `TARGET_REPO_OWNER` / `TARGET_REPO_NAME` | Yes | Owner and repo name from step 1. |
| `TARGET_REPO_DEFAULT_BRANCH` | No | Default `main`. |
| `ATHENA_DATABASE` / `ATHENA_TABLE` | Optional | Default Athena input; user can override per conversation. |
| `RAW_DATA_BUCKET_S3_URI` | Optional | Default raw-S3 input; enables the raw-S3 flow when no Athena source is given. |

### 4. Install dependencies

```bash
cd agents/strands_glue_pipeline_agent
uv sync  # or: pip install -r requirements.txt
```

The GitHub MCP server is GitHub's hosted service at `https://api.githubcopilot.com/mcp/` — no local install. The agent authenticates with the fine-grained PAT from `.env`.

### 5. Set up AWS Glue prerequisites

See `setup_glue_job_prereqs.sh` for IAM roles, S3 staging locations, and Bedrock access. Run it once per dev account.

## Running locally

The default path is the Chainlit chat frontend with Phoenix tracing — one
script brings up the FastAPI service, the UI, and the trace UI together:

```bash
aws sso login
./scripts/run_local_stack.sh    # from the repo root
```

Open http://127.0.0.1:8000 for the chat UI and http://127.0.0.1:6006 for
Phoenix. Full details and overrides are in the root [README.md](../../README.md).

For one-shot CLI runs (no UI, no service), `main.py` is still here:

```bash
cd agents/strands_glue_pipeline_agent
uv run python main.py --prompt "Build a Glue job that averages daily orders from myschema.orders and writes to s3://my-outputs/daily-avg/"
```

## Skills

The agent auto-loads every skill under `skills/`:

- **`athena-query-execution`** — SQL-only pipelines run via Athena.
- **`glue-python-shell-job`** — two-phase (scratch via MCP + commit-driven deploy) Glue Python Shell job flow.
- **`s3-raw-data-ingestion`** — discovery flow for raw data in S3 that isn't catalogued in Athena.
- **`project-structure`** — directory layout, package naming, and entrypoint conventions for committed code. Points at the canonical scaffold in `target_repo_template/`.

## Current limitations

These are deliberate simplifications, not bugs. They are the next items to address.

- **The agent does not poll GitHub Actions for the CI pipeline.** After pushing to the feature branch it ends the turn and asks the user to resume once CI is green. Automating this is a drop-in upgrade once the CI pipeline is stable.
- **The agent does not watch for PR merge.** Its turn ends at "PR open + Phase B verification run succeeded." Post-merge verification is a fresh conversation.
- **The CI pipeline in the target repo (`.github/workflows/build-and-deploy.yml`) uses static AWS access keys.** OIDC federation is the intended production upgrade.
- **PySpark jobs are not yet supported.** The agent handles Python Shell jobs only. A dedicated PySpark skill will be added in a separate branch. The reference PySpark scaffold that previously lived at `target_repo_template/jobs/example_pyspark/` has been removed until then.
- **Tool-spam guarding is narrow.** `GlueJobRunPollThrottleHook` throttles repeated `get-job-run` polls across the MCP tool and the `awsapi_call_aws` CLI fallback, but it only covers that one operation. The model has been observed to spam other identical tool calls (e.g. repeated `aws glue get-job-run` or `aws athena get-query-execution` via arbitrary paths) while claiming in its text that it's waiting. The next step is a more general pattern — likely either (a) a generic "identical `(tool_name, canonicalised_args)` within N seconds → sleep" hook as defence-in-depth, or (b) an industry pattern such as token-bucket rate limiters per tool class, AWS SDK-style exponential backoff at the hook layer, or explicit "wait N seconds" synthetic tool calls that the model must make between polls. Needs research before committing to a design.

## Deploying to ECS

This agent is being migrated from Bedrock AgentCore to ECS Fargate. The new topology runs the agent as a FastAPI service that streams typed SSE events to a separate Chainlit frontend service. Infrastructure (VPC, ECR, RDS, ALBs, IAM roles, ECS services) is provisioned via CDK Python in [`../../infra/`](../../infra/).

The IAM policy granting Glue/S3/Logs/PassRole access to the agent task role lives at [`../../infra/policies/strands_glue_pipeline_access.json`](../../infra/policies/strands_glue_pipeline_access.json) (extracted from the previous AgentCore deploy script). Outstanding scope-down work and the Athena-permissions extension are documented in [`../../infra/policies/README.md`](../../infra/policies/README.md).

Secret material — `GITHUB_PAT`, the Bedrock model ID override, the service-to-service auth token between frontend and agent — is held in AWS Secrets Manager and injected via the ECS task definition's `secrets` block. **Never bake `GITHUB_PAT` into the image or a `.env` inside the image.**

End-to-end deployment scripts (`scripts/deploy_agent.sh`, `scripts/deploy_frontend.sh`) and the CDK stacks land in subsequent phases of this branch.



uv run python main.py --prompt "Build a Glue job that counts rows in sample_database.sample_table and writes the count to s3://glue-assets-554032904022-eu-central-1-an/scratch/row-count/."