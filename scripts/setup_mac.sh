#!/bin/bash
# One-time FocAssist Mac setup.
# Run this once after cloning the repo and filling in .env.local.
# Requires sudo once — installs the privileged blocker daemon.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_DIR/.venv/bin/python"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LAUNCH_DAEMONS="/Library/LaunchDaemons"

echo ""
echo "=== FocAssist Mac Setup ==="
echo "Repo: $REPO_DIR"
echo ""

# ── Preflight ─────────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
  echo "ERROR: venv not found at $PYTHON"
  echo "  Run: /usr/local/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements-backend.txt"
  exit 1
fi

if [ ! -f "$REPO_DIR/.env.local" ]; then
  echo "ERROR: .env.local not found — copy .env.example and fill in values."
  exit 1
fi

# ── 1. Privileged blocker daemon (/Library/LaunchDaemons, runs as root) ───────
echo "Installing privileged blocker daemon (requires sudo)..."

sudo tee "$LAUNCH_DAEMONS/com.focus.blocker.plist" > /dev/null << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.focus.blocker</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$REPO_DIR/agent/blocking/blocker_daemon.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/focassist-blocker.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/focassist-blocker-error.log</string>
</dict>
</plist>
PLIST

sudo chown root:wheel "$LAUNCH_DAEMONS/com.focus.blocker.plist"
sudo chmod 644 "$LAUNCH_DAEMONS/com.focus.blocker.plist"
sudo launchctl unload "$LAUNCH_DAEMONS/com.focus.blocker.plist" 2>/dev/null || true
sudo launchctl load "$LAUNCH_DAEMONS/com.focus.blocker.plist"
echo "  ✅ Blocker daemon installed and running"

# ── 2. User agent (~Library/LaunchAgents, runs as current user) ───────────────
echo "Installing Mac agent..."

# Read env vars from .env.local
source "$REPO_DIR/.env.local"
BACKEND_URL="${FOCASSIST_BACKEND_URL:-}"
TOKEN="${FOCASSIST_TOKEN:-}"
AW_HOST="${AW_HOSTNAME:-}"

if [ -z "$BACKEND_URL" ] || [ -z "$TOKEN" ]; then
  echo "ERROR: FOCASSIST_BACKEND_URL and FOCASSIST_TOKEN must be set in .env.local"
  exit 1
fi

mkdir -p "$LAUNCH_AGENTS"
cat > "$LAUNCH_AGENTS/com.focus.agent.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.focus.agent</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>agent.main</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>FOCASSIST_BACKEND_URL</key>
        <string>$BACKEND_URL</string>
        <key>FOCASSIST_TOKEN</key>
        <string>$TOKEN</string>
        <key>AW_HOSTNAME</key>
        <string>$AW_HOST</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/focassist-agent.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/focassist-agent-error.log</string>

    <key>ThrottleInterval</key>
    <integer>300</integer>
</dict>
</plist>
PLIST

launchctl unload "$LAUNCH_AGENTS/com.focus.agent.plist" 2>/dev/null || true
launchctl load "$LAUNCH_AGENTS/com.focus.agent.plist"
echo "  ✅ Mac agent installed and running"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✅ Setup complete!"
echo ""
echo "Logs:"
echo "  Agent:   tail -f /tmp/focassist-agent.log"
echo "  Blocker: tail -f /tmp/focassist-blocker.log"
echo ""
echo "Test blocking:"
echo "  python3 -c \"import sys; sys.path.insert(0,'$REPO_DIR'); from agent.blocker import is_active; print('Daemon reachable:', is_active())\""
