#!/usr/bin/env python3
"""
Runs on EC2 at boot (as root) to pull secrets from SSM Parameter Store
and write /opt/focassist/.env. Requires an IAM role with ssm:GetParameter
on arn:aws:ssm:<region>:<account>:parameter/focassist/*.
"""
import sys
import boto3

REGION = sys.argv[1] if len(sys.argv) > 1 else "us-east-1"
ssm = boto3.client("ssm", region_name=REGION)


def get(name: str, encrypted: bool = False) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=encrypted)["Parameter"]["Value"]


token = get("/focassist/token", encrypted=True)
tg_token = get("/focassist/telegram_bot_token", encrypted=True)
tg_chat_id = get("/focassist/telegram_chat_id")

env_content = f"""\
FOCASSIST_TOKEN={token}
TELEGRAM_BOT_TOKEN={tg_token}
TELEGRAM_CHAT_ID={tg_chat_id}
FOCASSIST_DB=/var/lib/focassist/focassist.db
FOCASSIST_API_HOST=0.0.0.0
FOCASSIST_API_PORT=8000
"""

env_path = "/opt/focassist/.env"
with open(env_path, "w") as f:
    f.write(env_content)

# Restrict permissions — contains secrets
import os
os.chmod(env_path, 0o600)

print(f"SSM params written to {env_path}")
