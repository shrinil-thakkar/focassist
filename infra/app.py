#!/usr/bin/env python3
import os
import aws_cdk as cdk
from stacks.backend_stack import FocAssistBackendStack

app = cdk.App()

FocAssistBackendStack(
    app,
    "FocAssistBackend",
    env=cdk.Environment(
        # CDK CLI always sets CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION from
        # the active AWS profile. Override via: cdk deploy -c account=... -c region=...
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="FocAssist backend — EC2 t4g.micro (ARM/Graviton2), Telegram bot, APScheduler",
)

app.synth()
