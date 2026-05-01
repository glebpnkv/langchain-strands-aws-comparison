"""Network stack: VPC + security groups.

Creates the foundation every other stack attaches to:

- A 2-AZ VPC with one public subnet and one private-with-egress subnet
  per AZ. 2 AZs is the floor for RDS subnet groups and ALBs (both refuse
  to come up with fewer), so we'd need it even if ECS could get away
  with one.
- One NAT gateway (down from the CDK default of one per AZ). This is a
  conscious dev-only cost trade-off: ~$32/mo saved at the price of
  outbound from the second AZ failing if the AZ holding the NAT
  blackholes. For prod, set `nat_gateways=2` (or use NAT Instances /
  VPC endpoints).
- Five security groups, one per network role. Their pairwise ingress
  rules are wired here too — the data and compute stacks just consume
  the SGs, they don't add rules.

The VPC and all SGs are exposed as public attributes so subsequent
stacks (passed via app.py) can attach to them directly. CDK auto-
creates the cross-stack exports/imports; we don't manage them by hand.
"""

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

# Service ports — kept here so SGs and the compute stack agree.
# The ALBs listen on 80 (standard HTTP) and forward to the container
# ports below. SGs MUST gate the ALB ingress on the listener port (80),
# not the container port — otherwise traffic that reaches the ALB is
# silently dropped at the SG before the listener ever sees it.
ALB_HTTP_PORT = 80
FRONTEND_HTTP_PORT = 8000
AGENT_HTTP_PORT = 8080
POSTGRES_PORT = 5432


class NetworkStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # ---- Security groups ------------------------------------------------
        # Created without ingress rules first so we can reference each other
        # when wiring rules below (avoids circular construct dependencies).

        self.frontend_alb_sg = ec2.SecurityGroup(
            self,
            "FrontendAlbSg",
            vpc=self.vpc,
            description="Frontend ALB - accepts user traffic on the Chainlit port",
            allow_all_outbound=True,
        )

        self.frontend_task_sg = ec2.SecurityGroup(
            self,
            "FrontendTaskSg",
            vpc=self.vpc,
            description="Frontend ECS tasks (Chainlit). Reachable from the frontend ALB only.",
            allow_all_outbound=True,
        )

        self.agent_alb_sg = ec2.SecurityGroup(
            self,
            "AgentAlbSg",
            vpc=self.vpc,
            description="Agent (internal) ALB - accepts traffic from frontend tasks only",
            allow_all_outbound=True,
        )

        self.agent_task_sg = ec2.SecurityGroup(
            self,
            "AgentTaskSg",
            vpc=self.vpc,
            description="Agent ECS tasks (FastAPI). Reachable from the agent ALB only.",
            allow_all_outbound=True,
        )

        self.rds_sg = ec2.SecurityGroup(
            self,
            "RdsSg",
            vpc=self.vpc,
            description="RDS Postgres for Chainlit Data Layer. Reachable from frontend tasks only.",
            allow_all_outbound=False,
        )

        # ---- Pairwise ingress rules ----------------------------------------
        # Trust path (ALBs listen on 80, container ports differ):
        #   user --[ALB_HTTP_PORT=80]--> frontend_alb_sg
        #   frontend_alb_sg --[FRONTEND_HTTP_PORT=8000]--> frontend_task_sg
        #   frontend_task_sg --[ALB_HTTP_PORT=80]--> agent_alb_sg
        #   agent_alb_sg --[AGENT_HTTP_PORT=8080]--> agent_task_sg
        #   frontend_task_sg --[POSTGRES_PORT=5432]--> rds_sg

        # User traffic into the frontend ALB on the ALB's listener port.
        # The ALB itself is `internal` scheme so this is corp-VPN /
        # SSM-port-forward reachable, not open to the public internet.
        self.frontend_alb_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="VPC clients to frontend ALB",
        )

        # ALB-to-task ingress is on the *container* port (frontend ALB
        # forwards 80 -> 8000).
        self.frontend_task_sg.add_ingress_rule(
            peer=self.frontend_alb_sg,
            connection=ec2.Port.tcp(FRONTEND_HTTP_PORT),
            description="Frontend ALB to Chainlit tasks",
        )

        # Frontend tasks reach the agent ALB on the listener port.
        self.agent_alb_sg.add_ingress_rule(
            peer=self.frontend_task_sg,
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Frontend tasks to agent ALB",
        )

        # Agent ALB-to-task is on the container port (8080).
        self.agent_task_sg.add_ingress_rule(
            peer=self.agent_alb_sg,
            connection=ec2.Port.tcp(AGENT_HTTP_PORT),
            description="Agent ALB to agent tasks",
        )

        self.rds_sg.add_ingress_rule(
            peer=self.frontend_task_sg,
            connection=ec2.Port.tcp(POSTGRES_PORT),
            description="Frontend tasks to Postgres (Chainlit Data Layer)",
        )

        # ---- Outputs (visible in CloudFormation console) -------------------
        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        cdk.CfnOutput(
            self,
            "VpcCidr",
            value=self.vpc.vpc_cidr_block,
            description="Whitelist this CIDR for any corp-network ingress allowances",
        )
