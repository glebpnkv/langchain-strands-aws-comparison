"""ECR stack: container image registries for the agent + frontend services.

Two repos, one per service. Phase 5's `deploy_agent.sh` and
`deploy_frontend.sh` build, tag, and push to these. The compute stack
(4e/4f) reads images from them.

Lifecycle policy is "keep last 10 tagged images" — enough headroom to
roll back a few deploys, small enough that storage cost stays flat.
ECR's basic vulnerability scan runs on every push (free; CVEs show up
in the AWS console).

Dev-only: empty_on_delete + removal_policy=DESTROY so the teardown
script can actually delete the repos (CloudFormation otherwise refuses
to delete a non-empty ECR repo). For prod, drop both flags and
delete-protect manually.
"""

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from constructs import Construct


# Lowercase + slashes per ECR's repo naming rules. Slashes are not real
# directories on the registry — they just group the repos visually in
# the console.
AGENT_REPO_NAME = "glue-agent/agent"
FRONTEND_REPO_NAME = "glue-agent/frontend"


class EcrStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        common = {
            "image_scan_on_push": True,
            "empty_on_delete": True,
            "removal_policy": cdk.RemovalPolicy.DESTROY,
            "lifecycle_rules": [
                ecr.LifecycleRule(
                    description="Keep the 10 most recent images, expire older ones",
                    max_image_count=10,
                ),
            ],
        }

        self.agent_repo = ecr.Repository(
            self,
            "AgentRepo",
            repository_name=AGENT_REPO_NAME,
            **common,
        )

        self.frontend_repo = ecr.Repository(
            self,
            "FrontendRepo",
            repository_name=FRONTEND_REPO_NAME,
            **common,
        )

        # Outputs are how Phase 5's deploy scripts discover where to push.
        # SSM Parameter Store is the indirection point; the deploy scripts
        # read from SSM rather than parsing CloudFormation outputs.
        cdk.CfnOutput(
            self,
            "AgentRepoUri",
            value=self.agent_repo.repository_uri,
            description="`docker push` this URI to publish a new agent image",
        )
        cdk.CfnOutput(
            self,
            "FrontendRepoUri",
            value=self.frontend_repo.repository_uri,
            description="`docker push` this URI to publish a new frontend image",
        )
