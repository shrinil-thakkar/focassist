#!/bin/bash
# Run this ONCE locally before `cdk deploy`.
# Creates /focassist/* parameters in AWS SSM Parameter Store.
# Requires AWS CLI configured with credentials that can write to SSM.
set -euo pipefail

REGION=${AWS_DEFAULT_REGION:-us-east-1}
echo "Region: $REGION"
echo ""
echo "=== FocAssist SSM Parameter Setup ==="
echo "Secrets are stored as SecureString (encrypted, free tier)."
echo ""

# Generate a random token if the user just presses Enter
DEFAULT_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")

read -rp "Shared bearer token [press Enter to generate]: " TOKEN
TOKEN=${TOKEN:-$DEFAULT_TOKEN}

read -rsp "Telegram bot token: " TG_TOKEN; echo
read -rp "Telegram chat ID: " TG_CHAT_ID

echo ""
echo "Writing parameters..."

aws ssm put-parameter \
  --name /focassist/token \
  --value "$TOKEN" \
  --type SecureString \
  --overwrite \
  --region "$REGION" \
  --description "FocAssist shared bearer token (agent <-> backend)" \
  > /dev/null

aws ssm put-parameter \
  --name /focassist/telegram_bot_token \
  --value "$TG_TOKEN" \
  --type SecureString \
  --overwrite \
  --region "$REGION" \
  --description "FocAssist Telegram bot token" \
  > /dev/null

aws ssm put-parameter \
  --name /focassist/telegram_chat_id \
  --value "$TG_CHAT_ID" \
  --type String \
  --overwrite \
  --region "$REGION" \
  --description "FocAssist owner Telegram chat ID" \
  > /dev/null

echo "✅ SSM parameters created."
echo ""
echo "Also save FOCASSIST_TOKEN to .env.local for the Mac agent:"
echo "  export FOCASSIST_TOKEN=$TOKEN"
echo ""
echo "Next: cd infra && cdk deploy"
