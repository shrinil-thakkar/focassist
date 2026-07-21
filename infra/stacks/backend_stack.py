import os
from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
)
from constructs import Construct


class FocAssistBackendStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        github_repo = (
            self.node.try_get_context("github_repo")
            or "https://github.com/shrinil-thakkar/focassist.git"
        )
        # Restrict agent API port to your home IP for extra security.
        # Set via: cdk deploy -c allowed_cidr=YOUR.IP.HERE/32
        allowed_cidr = self.node.try_get_context("allowed_cidr") or "0.0.0.0/0"

        # ── Networking ────────────────────────────────────────────────────────
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        sg = ec2.SecurityGroup(
            self, "BackendSG",
            vpc=vpc,
            description="FocAssist backend",
            allow_all_outbound=True,
        )
        sg.add_ingress_rule(ec2.Peer.ipv4(allowed_cidr), ec2.Port.tcp(22), "SSH")
        sg.add_ingress_rule(ec2.Peer.ipv4(allowed_cidr), ec2.Port.tcp(8000), "Agent API")

        # ── IAM ───────────────────────────────────────────────────────────────
        role = iam.Role(
            self, "BackendRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                # Enables SSM Session Manager (SSH alternative, no port 22 needed)
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        # Allow reading /focassist/* SSM parameters at boot and at runtime
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter/focassist/*"
            ],
        ))

        # ── Key pair (private key auto-stored in SSM by CDK) ──────────────────
        key_pair = ec2.KeyPair(self, "FocAssistKeyPair", key_pair_name="focassist-key")

        # ── AMI — Ubuntu 22.04 LTS ARM64 (Graviton2) ──────────────────────────
        ami = ec2.MachineImage.from_ssm_parameter(
            "/aws/service/canonical/ubuntu/server/22.04/stable/current/arm64/hvm/ebs-gp2/ami-id",
            os=ec2.OperatingSystemType.LINUX,
        )

        # ── User data ─────────────────────────────────────────────────────────
        script_path = os.path.join(os.path.dirname(__file__), "..", "user_data.sh")
        with open(script_path) as f:
            script = f.read()
        script = script.replace("__GITHUB_REPO__", github_repo)
        script = script.replace("__REGION__", self.region)

        ud = ec2.UserData.for_linux()
        ud.add_commands(script)

        # ── EC2 instance — t4g.micro (ARM/Graviton2) ──────────────────────────
        instance = ec2.Instance(
            self, "BackendInstance",
            instance_type=ec2.InstanceType("t4g.micro"),
            machine_image=ami,
            vpc=vpc,
            role=role,
            security_group=sg,
            key_pair=key_pair,
            user_data=ud,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(20),
                )
            ],
        )

        # ── Elastic IP — stable address for agent's FOCASSIST_BACKEND_URL ─────
        eip = ec2.CfnEIP(self, "BackendEIP", instance_id=instance.instance_id)

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "PublicIp", value=eip.ref,
            description="Stable public IP of the backend")

        CfnOutput(self, "BackendUrl", value=f"http://{eip.ref}:8000",
            description="Set this as FOCASSIST_BACKEND_URL in the agent plist")

        CfnOutput(self, "SshCommand", value=f"ssh -i ~/.ssh/focassist.pem ubuntu@{eip.ref}",
            description="SSH after downloading the private key (see GetPrivateKeyCmd)")

        CfnOutput(self, "GetPrivateKeyCmd",
            value=(
                f"aws ssm get-parameter"
                f" --name /ec2/keypair/{key_pair.key_pair_id}"
                f" --with-decryption --query Parameter.Value --output text"
                f" > ~/.ssh/focassist.pem && chmod 400 ~/.ssh/focassist.pem"
            ),
            description="Run this locally to download the SSH private key")

        CfnOutput(self, "ServiceLogs",
            value=f"ssh -i ~/.ssh/focassist.pem ubuntu@{eip.ref} 'sudo journalctl -u focassist -f'",
            description="Stream backend logs")

        CfnOutput(self, "SetupLog",
            value=f"ssh -i ~/.ssh/focassist.pem ubuntu@{eip.ref} 'cat /var/log/focassist-setup.log'",
            description="View the one-time bootstrap log if something went wrong")
