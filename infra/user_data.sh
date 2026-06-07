#!/bin/bash
# EC2 bootstrap — runs once at first launch as root.
# All output is logged to /var/log/focassist-setup.log
set -euo pipefail
exec > /var/log/focassist-setup.log 2>&1

echo "=== FocAssist bootstrap started $(date) ==="

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git

# boto3 at system level — used only by the one-time read_ssm.py script
pip3 install -q boto3

# ── Clone repo ────────────────────────────────────────────────────────────────
REPO=__GITHUB_REPO__
REGION=__REGION__
INSTALL_DIR=/opt/focassist

# Embed GitHub PAT into clone URL if stored in SSM (for private repos).
# Uses boto3 (installed above) — aws CLI is not yet available at this stage.
GITHUB_PAT=$(python3 - <<PYEOF
import boto3, sys
try:
    ssm = boto3.client('ssm', region_name='$REGION')
    print(ssm.get_parameter(Name='/focassist/github_pat', WithDecryption=True)['Parameter']['Value'], end='')
except Exception:
    pass
PYEOF
)
if [ -n "$GITHUB_PAT" ]; then
  CLONE_URL=$(echo "$REPO" | sed "s|https://|https://${GITHUB_PAT}@|")
else
  CLONE_URL="$REPO"
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Repo already cloned — pulling latest"
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$CLONE_URL" "$INSTALL_DIR"
fi
chown -R ubuntu:ubuntu "$INSTALL_DIR"

# ── Write .env from SSM params ────────────────────────────────────────────────
python3 "$INSTALL_DIR/infra/read_ssm.py" "$REGION"

# ── Python venv + backend deps ────────────────────────────────────────────────
cd "$INSTALL_DIR"
sudo -u ubuntu python3 -m venv .venv
sudo -u ubuntu .venv/bin/pip install -q -r requirements-backend.txt

# ── Data directory ────────────────────────────────────────────────────────────
mkdir -p /var/lib/focassist
chown ubuntu:ubuntu /var/lib/focassist

# ── Systemd service ───────────────────────────────────────────────────────────
cat > /etc/systemd/system/focassist.service << 'EOF'
[Unit]
Description=FocAssist Backend
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/focassist
EnvironmentFile=/opt/focassist/.env
ExecStart=/opt/focassist/.venv/bin/python -m backend.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable focassist
systemctl start focassist

echo "=== FocAssist bootstrap complete $(date) ==="
