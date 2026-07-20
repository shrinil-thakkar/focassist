# FocAssist — Personal Productivity Assistant

Self-hosted, $0 macOS productivity assistant. Tracks your time via ActivityWatch, plans your day through Telegram, and blocks distracting websites during focus blocks.

See `docs/BUILD_SPEC.md` for the full product spec.

---

## Architecture

```
Mac Agent (local)  ──push aggregates──►  Cloud Backend (AWS EC2)
                   ◄──poll directive──   │
                                         ├── Telegram Bot (nudges + chat)
                                         ├── APScheduler (evening/morning nudges)
                                         ├── FastAPI (agent API)
                                         └── SQLite (plans, rules, activity)
                                                    │
                                             Telegram API ──► Your phone + Mac
```

---

## Prerequisites

### On your Mac
- Python 3.11+
- [ActivityWatch](https://activitywatch.net/) installed and running
- ActivityWatch browser extension installed (for URL tracking)

### On AWS EC2 (t4g.micro, ARM/Graviton, Ubuntu 22.04)
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Port 8000 open in the EC2 security group (for agent ↔ backend; restrict to your home IP)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOURUSERNAME/focassist.git
cd focassist
```

### 2. Set up the backend (EC2)

**SSH into your EC2 instance, then:**

```bash
git clone https://github.com/YOURUSERNAME/focassist.git
cd focassist

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-backend.txt

cp .env.example .env
# Edit .env — fill in FOCASSIST_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
nano .env
```

**Find your Telegram chat ID:**
1. Start a chat with your bot on Telegram (send `/start`).
2. Run: `curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Look for `"chat":{"id": <number>}` in the response — that's your chat ID.
4. Set `TELEGRAM_CHAT_ID=<that number>` in `.env`, then also store it:

```bash
# After the backend is running once, set it in the DB:
python3 -c "
import os; os.environ['FOCASSIST_TOKEN']='your-token'
from backend import db; db.init_db(); db.set_config('telegram_chat_id', 'YOUR_CHAT_ID')
"
```

**Run the backend (test first, then as a service):**

```bash
# Test run (Ctrl+C to stop)
source .venv/bin/activate
source .env  # or: export $(cat .env | xargs)
python3 -m backend.main
```

**Install as a systemd service (so it survives reboots):**

```bash
sudo nano /etc/systemd/system/focassist.service
```

Paste:
```ini
[Unit]
Description=FocAssist Backend
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/focassist
EnvironmentFile=/home/ubuntu/focassist/.env
ExecStart=/home/ubuntu/focassist/.venv/bin/python -m backend.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable focassist
sudo systemctl start focassist
sudo journalctl -u focassist -f  # watch logs
```

**Enable HTTPS (strongly recommended):**
Put nginx + Let's Encrypt in front of port 8000. The agent uses `FOCASSIST_BACKEND_URL` which should be `https://your-domain-or-ip`.

### 3. Set up the Mac agent

```bash
cd /path/to/focassist
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.txt  # minimal — stdlib only for now
```

**Test it manually first:**

```bash
export FOCASSIST_BACKEND_URL=https://your-ec2-ip-or-domain
export FOCASSIST_TOKEN=your-shared-secret

# Check ActivityWatch is running
python3 -m agent.tracking.tracker

# Run one sync cycle
python3 -m agent.main  # Ctrl+C after first cycle
```

**Install as a launchd service (runs at login, always-on):**

```bash
# Edit the plist — fill in your username and paths
nano agent/com.focus.agent.plist

# Install
cp agent/com.focus.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.focus.agent.plist

# Check it's running
launchctl list | grep focus
tail -f /tmp/focassist-agent.log
```

---

## Usage (Telegram)

Once both the backend and agent are running, open Telegram and chat with your bot:

| Command | Action |
|---|---|
| `/plan` | Plan tomorrow — bot prompts you, then stores your reply |
| `/today` | Today's time breakdown (from pushed aggregates) |
| `/report` | Weekly productive vs unproductive summary |
| `/block_now <mins>` | Start an ad-hoc focus block (blocks distracting sites) |
| `/shift <label> <±mins>` | Shift a time block, e.g. `/shift focus +30` |
| `/sensitive <app\|domain>` | Add to the never-send-to-LLM list |

The bot also nudges you automatically:
- **21:30** — "Plan tomorrow" prompt
- **08:30** — Morning confirm of today's plan
- **Sunday 18:00** — Weekly report

---

## Milestone Status

- [x] **M1 — Plumbing**: ActivityWatch tracking, EC2 backend, Telegram bot, nightly planning, `/today` query
- [ ] **M2 — Schedule + blocking**: editable time blocks, irreversible website block, on-the-fly rescheduling
- [ ] **M3 — Intelligence**: tiered categorizer (rules → Gemini → ask me), self-improving rules, weekly insights

---

## Cost

- **EC2 t4g.micro**: covered by AWS credits. No always-free tier on EC2; if credits run low, migrate to Lambda + DynamoDB + EventBridge (see spec §4).
- **ActivityWatch**: free, open-source, local.
- **Telegram**: free.
- **Everything else**: free.

Anything that would start costing money is noted in `docs/BUILD_SPEC.md §4`.

---

## Security notes

- The backend API uses a shared bearer token (`FOCASSIST_TOKEN`). Keep it secret.
- Restrict EC2 port 8000 to your home IP in the security group.
- Raw activity data (URLs, window titles) never leaves your Mac — only aggregated category totals are pushed.
- The one exception: ambiguous items (domain + title) may eventually be sent to Gemini (M3). Sensitive apps/domains are excluded from this via `/sensitive`.
