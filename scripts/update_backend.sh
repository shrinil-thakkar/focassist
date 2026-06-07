#!/bin/bash
# Deploy latest code to EC2 after a git push.
# Usage: ./scripts/update_backend.sh <ec2-ip> [path/to/focassist.pem]
set -euo pipefail

EC2_IP=${1:?"Usage: $0 <ec2-ip> [key.pem]"}
KEY=${2:-focassist.pem}

if [ ! -f "$KEY" ]; then
  echo "Key file '$KEY' not found. Download it first:"
  echo "  aws ssm get-parameter --name /ec2/keypair/<id> --with-decryption --query Parameter.Value --output text > $KEY && chmod 400 $KEY"
  exit 1
fi

echo "Deploying to ubuntu@$EC2_IP ..."
ssh -i "$KEY" -o StrictHostKeyChecking=no ubuntu@"$EC2_IP" '
  set -euo pipefail
  cd /opt/focassist
  git pull --ff-only
  .venv/bin/pip install -q -r requirements-backend.txt
  sudo systemctl restart focassist
  echo ""
  sudo systemctl status focassist --no-pager -l
'
echo ""
echo "Done. Stream logs with:"
echo "  ssh -i $KEY ubuntu@$EC2_IP 'sudo journalctl -u focassist -f'"
