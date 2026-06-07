# Focus Assistant — Build Spec (v1)

A self-hosted, $0 personal productivity assistant for macOS. It helps me plan my day,
keeps me on schedule via a chat assistant, tracks where my time goes, and blocks
distracting websites during focus time. Built to be scaffolded and iterated on with
Claude Code.

---

## 1. Context & constraints

- **Cost:** $0. Use only free/open-source tools and free cloud resources. I have AWS and GCP
  credits, with **more on AWS — so AWS is the default host.** EC2 runs on credits now; the
  perpetual-free migration target (if credits run low) is the AWS serverless stack (see §4),
  since EC2 has no always-free tier the way GCP's e2-micro does.
- **Platform (v1):** macOS only. iPhone gets nudges for free via Telegram, but no tracking
  or blocking on iOS in v1.
- **Build model:** Claude Code does the scaffolding and most of the coding. I stay in the
  loop to review, run setup commands, and request changes.
- **Privacy:** Raw activity data (URLs, window titles) never leaves my Mac. Only aggregated
  summaries are sent anywhere off-device.

---

## 2. Product goals (the 8 requirements)

1. Make me plan tomorrow on the current night.
2. Keep prompting/nudging me, chatbot-easy. I can also ask it for data.
3. Make me follow the schedule.
4. Track my time spent on the laptop; provide a weekly insight/report.
5. Categorize time into productive vs unproductive; when unclear, ask me (broadly or specifically).
6. Set out productive time with dedicated slots for unproductive time; flexible to shift on the fly.
7. Stop me from doing unproductive things during productive time (v1: **websites only**).
8. Push me toward completing tasks smartly.

---

## 3. Architecture overview

Three parts, connected over outbound HTTPS only (the Mac sits behind home NAT, so it
*polls/pushes* to the backend — the backend never reaches into the Mac).

```
  ┌────────────────────────────┐         ┌──────────────────────────────┐
  │  MAC AGENT (local only)     │         │  CLOUD BACKEND (AWS EC2,     │
  │                             │         │  t4g.micro on credits)       │
  │  • ActivityWatch            │  push   │                                │
  │    - active app + title     │ ──────► │  • Telegram bot (chat + nudges)│
  │    - active URL (extension) │ summary │  • Scheduler (nudge timing)    │
  │  • Categorizer (rules)      │         │  • SQLite (plans, schedules,   │
  │  • Website blocker          │ ◄────── │    daily/weekly aggregates)    │
  │    (SelfControl, irreversible)│ poll  │  • Rule engine (v1)            │
  │  • Sync client (push/poll)  │ directive│  • Gemini free tier (v2)      │
  └────────────────────────────┘         └───────────────┬──────────────┘
                                                          │ Telegram API
                                                          ▼
                                                   ┌──────────────┐
                                                   │  Telegram     │
                                                   │  (Mac + phone)│
                                                   └──────────────┘
```

---

## 4. Tech stack (all free)

| Layer | Choice | Why |
|---|---|---|
| Time tracking | **ActivityWatch** (open-source, local) | Already logs active app, window title, and active URL (browser extension). Local REST API at `localhost:5600`. Don't rebuild tracking. |
| Categorization | **Tiered: rules → LLM → ask me** | Tier 1: deterministic domain/app rules (free, instant) cover the obvious bulk. Tier 2: Gemini classifies only the ambiguous tail (batched daily, stays in free tier). Tier 3: Telegram asks me about the rest. Every answer (mine or a confident LLM call) becomes a new Tier-1 rule, so the LLM/ask load shrinks over time. |
| Website blocking | **SelfControl** (open-source) via its CLI, or a locked hosts-file block | Irreversible once started → real friction, can't be undone mid-session. |
| Chat + nudges | **Telegram Bot** (`python-telegram-bot`) | Free push to Mac + phone, no UI to build. |
| Backend host | **AWS EC2 t4g.micro** (paid from credits) | Always-on so nudges fire even when the Mac is asleep. EC2 has no perpetual free tier, so when credits run low, migrate to the always-free serverless stack (next row). |
| Perpetual-free fallback | **AWS Lambda + DynamoDB + EventBridge Scheduler** | AWS's always-free path: Lambda (1M req/mo) + DynamoDB (25GB) + EventBridge for nudge timing. More moving parts than a VM, but $0 forever and a clean fit for an event-driven nudge bot. Migrate here only if credits run out. |
| Scheduler | **APScheduler** | Fires planning prompts, focus-block start/end, weekly report. |
| Database | **SQLite** | Plans, schedules, categories, aggregates. Simple, file-based. |
| Assistant brain | **Rule-based (v1) → Gemini free tier (v2)** | Deterministic commands first; add LLM for freeform chat once plumbing works. |

---

## 5. Data flow & privacy model

- ActivityWatch stores **raw** events (URLs, titles) locally only.
- The Mac agent computes **aggregates** (e.g. category totals, app-level minutes) and pushes
  only those to the backend.
- The backend stores aggregates + plans/schedules in SQLite.
- **Default guardrail:** Gemini receives aggregated summaries, not raw activity.
- **Exception — categorization:** the LLM tier needs item-level signal (domain + window title)
  for the ambiguous tail *only*. Just those leftover items are sent, batched. Apps/domains I
  mark **sensitive** (banking, private docs) are never sent — they skip straight to Tier 3 (ask me).
- If even the ambiguous-tail titles feel too sensitive for the Gemini free tier, run the
  categorizer on Bedrock via AWS credits instead (private, but not free forever).
- The Mac agent polls the backend for the current directive (is a focus block active? which
  domains to block?) and acts locally.

---

## 6. Component responsibilities

**Mac agent (Python, runs as a `launchd` login service):**
- Read events from the local ActivityWatch API.
- Apply Tier-1 category rules locally; queue only ambiguous items for Tier 2/3 (LLM/ask).
- Push periodic aggregates + the uncategorized queue to the backend.
- Poll the backend for the active directive; when a focus block is active, start a
  SelfControl/hosts block for the configured domains and duration.

**Cloud backend (Python on AWS EC2 t4g.micro):**
- Telegram bot: handle chat, send scheduled nudges, answer data queries.
- Scheduler: evening "plan tomorrow", morning confirm, focus-block start/end check-ins,
  weekly report.
- Rule engine (v1): parse commands (`/plan`, `/today`, `/report`, "push focus 30 min"),
  fill nudge templates.
- DB: persist plans, schedules, categories, aggregates.
- Serve the current directive to the Mac agent; receive aggregates + clarification queue.

**Telegram (client):** chat surface + push notifications, on Mac and phone.

---

## 7. v1 scope vs later

**In v1:**
- macOS tracking + categorization, Telegram assistant, nightly planning, schedule check-ins,
  **website-only** blocking with an irreversible lock, weekly report, rule-based brain.

**Deferred (v2+):**
- App blocking on macOS (the janky force-quit-and-nudge approach).
- Gemini free-tier layer for freeform chat + smarter-worded nudges.
- iOS tracking/blocking (would require Apple's Screen Time / Family Controls framework).

---

## 8. Milestones & acceptance criteria

**Milestone 1 — Plumbing**
- ActivityWatch installed + reporting active app and URL locally.
- AWS EC2 t4g.micro provisioned; Telegram bot live.
- Bot answers "what did I do today?" from pushed aggregates.
- Nightly prompt asks me to plan tomorrow; plan is stored.
- *Done when:* I can plan tomorrow in Telegram and query today's time.

**Milestone 2 — Schedule + blocking**
- Plan is stored as editable time blocks (productive + dedicated unproductive slots).
- Focus-block start fires a nudge; agent starts an irreversible website block for that block.
- "Push my focus block 30 min" reschedules on the fly.
- *Done when:* during a focus block, blocked sites are unreachable and can't be undone early.

**Milestone 3 — Intelligence**
- Tiered categorizer: Tier-1 rules → Tier-2 Gemini on the ambiguous tail → Tier-3 Telegram
  clarification ("Is `figma.com` productive?"). Every answer is saved as a new Tier-1 rule.
- Sensitive-app list honored (those items skip the LLM and go straight to asking me).
- Weekly report: productive vs unproductive breakdown + a couple of insights.
- Gemini also powers natural-language chat and nudge wording at this stage.
- *Done when:* categorization self-improves, the ask-rate drops over time, and I get a useful weekly report.

---

## 9. Suggested repo structure

**One monorepo, two deploy targets** (the Mac agent and the backend). The Telegram bot is a
module inside `backend/`, not a separate repo. This spec lives at `docs/BUILD_SPEC.md`; let
Claude Code generate `README.md` for setup/deploy steps.

```
focus-assistant/
├── agent/                 # Mac local agent
│   ├── tracker.py         # reads ActivityWatch API
│   ├── categorizer.py     # Tier-1 rules + queue ambiguous items for LLM/ask
│   ├── blocker.py         # SelfControl/hosts website blocking
│   ├── sync.py            # push aggregates / poll directive
│   └── com.focus.agent.plist  # launchd service
├── backend/               # AWS EC2 t4g.micro
│   ├── bot.py             # Telegram handlers
│   ├── scheduler.py       # APScheduler jobs
│   ├── rules.py           # v1 rule engine (command parsing, templates)
│   ├── llm.py             # Gemini: chat + ambiguous-tail categorization
│   ├── db.py              # SQLite models
│   └── api.py             # directive + ingest endpoints for the agent
├── shared/
│   └── schema.py          # shared data shapes
├── docs/
│   └── BUILD_SPEC.md      # this document
└── README.md              # setup & deploy steps (generated by Claude Code)
```

---

## 10. Notes for Claude Code

- Build strictly in milestone order; ship M1 end-to-end before starting M2.
- Keep the Mac↔cloud contract outbound-only from the Mac (poll + push). No inbound to the Mac.
- Off-Mac data is aggregates-only by default. The one exception is the Tier-2 categorizer,
  which may send the ambiguous tail's domain + title — honor the sensitive-app skip list there.
- Make the website block genuinely irreversible during an active focus block (this is the
  whole point — assume my future self will try to cheat).
- Prefer always-free tiers; document anything that would start costing money so I can decide.
- Backend host is EC2 **t4g.micro (ARM/Graviton)** — build ARM-compatible (watch for Python
  wheels). If ARM causes friction, fall back to t3.micro (x86, marginally pricier post-credits).
- Keep the backend modular (bot / scheduler / db / api as clean seams) so the perpetual-free
  migration — Lambda + DynamoDB + EventBridge — is a swap, not a rewrite.

---

## 11. Interface contracts (v1 starting point — Claude Code may refine)

These pin down the cross-component seams. Implementation details elsewhere are Claude Code's call.

### Agent ↔ backend API (HTTPS, bearer-token auth)
One shared secret token (agent config + backend env var); personal system, so no OAuth.
Header on every request: `Authorization: Bearer <token>`.

- `POST /ingest` — agent → backend. Body:
  `{ "date": "2026-06-07", "aggregates": [{"category","app","domain","minutes"}], "ambiguous": [{"app","domain","title","minutes"}] }`
- `GET /directive` — agent ← backend. Returns:
  `{ "focus_block_active": true, "block_domains": ["youtube.com"], "block_until": "2026-06-07T11:00:00Z" }`
- `GET /rules` — agent ← backend. Returns the current Tier-1 ruleset so the agent classifies locally:
  `[{"match_type":"domain","match_value":"github.com","category":"work","productive":true}]`

### Data model (SQLite)
- `time_blocks(id, date, start, end, label, kind[productive|unproductive|focus], block_domains)`
- `rules(id, match_type[domain|app|regex], match_value, category, productive, source[seed|user|llm])`
- `activity(date, category, app, domain, minutes)` — aggregates pushed by the agent
- `ambiguous_queue(id, app, domain, title, minutes, status[pending|asked|resolved])`
- `config(key, value)` — sensitive_apps, nudge times, working hours, token

### Nudge triggers (times stored in `config`)
- Evening (~21:30): "plan tomorrow" flow.
- Morning (~08:30): confirm / adjust today's plan.
- Focus-block start: "Starting <label> — blocking <domains> until <time>."
- Focus-block end: short check-in / break prompt.
- Distraction (M2+): nudge on a blocked-domain attempt during a focus block.
- Weekly (~Sun 18:00): weekly report.

### Telegram commands (v1)
- `/plan` — plan tomorrow (or today)
- `/today` — today's time breakdown so far
- `/report` — latest weekly report
- `/shift <block> <±mins>` — move a block (e.g. `/shift focus +30`)
- `/block_now <mins>` — start an ad-hoc focus block
- `/sensitive <app|domain>` — add to the never-send-to-LLM list
- free text — answers to categorization questions; (M3+) freeform chat via Gemini
