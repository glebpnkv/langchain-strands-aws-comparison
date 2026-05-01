"""CDK app entrypoint.

Synthesises the CloudFormation templates that provision the deployed
infrastructure for the strands_glue_pipeline_agent's Chainlit chat
frontend + FastAPI agent service. Today this is just a skeleton — the
real stacks land phase by phase under stacks/.

Naming convention: GlueAgent-<Layer>-<Stage>. Layer is one of Network /
Data / Ecr / Compute. Stage is just "Dev" until we add a second
environment.

Region defaults to the agent service's default (eu-central-1) but can
be overridden via CDK_DEFAULT_REGION. Account is read from the AWS
credentials in the environment at synth/deploy time — no need to hard
code it.
"""

import os

import aws_cdk as cdk

from stacks.compute import ComputeStack
from stacks.data import DataStack
from stacks.ecr import EcrStack
from stacks.network import NetworkStack

DEFAULT_REGION = "eu-central-1"
STAGE = "Dev"


def main() -> None:
    app = cdk.App()

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION") or DEFAULT_REGION,
    )

    # Tag every taggable resource so AWS Cost Explorer can filter the whole
    # project's spend in one click. Scoped to the App so every stack picks
    # it up automatically — no per-stack tagging boilerplate needed.
    cdk.Tags.of(app).add("Project", "GlueAgent")
    cdk.Tags.of(app).add("Stage", STAGE)
    cdk.Tags.of(app).add("ManagedBy", "cdk")

    network = NetworkStack(
        app,
        f"GlueAgent-Network-{STAGE}",
        env=env,
        description="VPC + security groups for the strands_glue_pipeline_agent stack",
    )

    data = DataStack(
        app,
        f"GlueAgent-Data-{STAGE}",
        env=env,
        description="RDS Postgres for Chainlit thread persistence + SM-managed credentials",
        vpc=network.vpc,
        rds_security_group=network.rds_sg,
    )

    ecr_stack = EcrStack(
        app,
        f"GlueAgent-Ecr-{STAGE}",
        env=env,
        description="ECR repos for the agent + frontend container images",
    )

    ComputeStack(
        app,
        f"GlueAgent-Compute-{STAGE}",
        env=env,
        description="ECS cluster + agent + frontend (2 internal ALBs, 5 SM secrets, SSM params)",
        vpc=network.vpc,
        agent_alb_sg=network.agent_alb_sg,
        agent_task_sg=network.agent_task_sg,
        frontend_alb_sg=network.frontend_alb_sg,
        frontend_task_sg=network.frontend_task_sg,
        agent_repo=ecr_stack.agent_repo,
        frontend_repo=ecr_stack.frontend_repo,
        db_instance=data.db_instance,
        db_secret=data.db_secret,
        stage=STAGE,
    )

    app.synth()


if __name__ == "__main__":
    main()
