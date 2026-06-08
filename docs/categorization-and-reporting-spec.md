# Categorization & Reporting Spec (Developer)

Companion to `BUILD_SPEC.md`. This is the authoritative detail for §4 (categorization) and the
reporting side of the assistant. Goal: measure *real* productivity, surface periods of serious
focus, and nudge tomorrow to be better — calibrated for a software developer.

---

## 1. The tier model

Four tiers. **Deep work is the hero metric**; supporting work is credited but kept separate so
it can't inflate the focus number; neutral is excluded from all ratios.

| Tier | Meaning | Counts toward |
|---|---|---|
| **Deep work** | Building/creating: coding, designing, writing specs | Focus score (primary) |
| **Supporting** | Necessary coordination: comms, email, tickets, meetings | "Work" ratio, not depth |
| **Neutral** | System, idle, transitions | Nothing (excluded) |
| **Distraction** | Entertainment, social, and **anything uncategorized** | Penalty |

**Active time** = deep + supporting + distraction (excludes neutral/idle). All ratios use active time.

---

## 2. Ruleset

### Deep work
- **Apps:** Terminal, iTerm2, Xcode, VS Code, Cursor, PyCharm + other JetBrains IDEs, Sublime,
  Neovim/Vim, Postman/Insomnia, TablePlus/DB clients, Docker Desktop.
- **Domains:** github.com, gitlab.com, stackoverflow.com, official docs (docs.python.org,
  developer.mozilla.org, `*.readthedocs.io`, `docs.*`), pypi.org, npmjs.com, figma.com,
  `localhost`/`127.0.0.1` (your dev server), and **AI coding assistants** — chatgpt.com,
  claude.ai, gemini.google.com, github.com/copilot. *(AI tools were missing from the old rules.)*

### Supporting
- **Apps:** Slack, Zoom, Microsoft Teams, Calendar.
- **Domains:** mail.google.com, outlook.com, linear.app, notion.so, atlassian/jira, asana.com,
  trello.com, calendar.google.com, meet.google.com.
- *Note:* Slack/Linear/Notion moved here from "productive" — they're coordination, not deep build.
  If you write design docs in Notion (real deep work), promote that via a whitelist (below).

### Neutral
- Finder, System Settings, Activity Monitor, lock screen/screensaver, idle.

### Distraction (default for anything unmatched)
- **Video:** youtube.com, netflix.com, primevideo.com, twitch.tv, disneyplus.com, hulu.com,
  hbomax.com/max.com.
- **Social:** reddit.com, x.com/twitter.com, instagram.com, tiktok.com, facebook.com.
- **Everything else uncategorized** (news, shopping, random browsing) → distraction by default.

### Whitelist overrides (the key mechanic)
Distraction domains can be promoted to deep/supporting at **finer-than-domain granularity** —
this requires path/channel/subreddit matching, not just the hostname. **More specific rule wins.**
- `youtube.com/@SomeConfChannel` or a flagged video → deep
- `reddit.com/r/rust`, `reddit.com/r/programming` → deep
- `news.ycombinator.com` → deep/supporting (your call)
- specific tech blogs/newsletters → deep

Rule shape: `{ match_type: domain|app|path|url_contains, match_value, tier, category, source }`.
Resolution order: most specific match (path > domain > app default) wins.

### How new whitelist entries get created
Per your choice, **unknown stays distraction** — no LLM auto-classification. But to cut manual
toil, the assistant may *suggest* promotions you approve in one tap:
> "You spent 38m on `youtube.com/@ThePrimeagen` (currently distraction). Whitelist as deep? [Yes/No]"
Suggestions use domain + title only, batched, and honor the sensitive-app skip list from BUILD_SPEC §5.

---

## 3. Serious-productivity (focus session) detection

Your defaults: **≥25-min blocks, ~5-min distraction tolerance.**

1. Walk the day's timeline of tiered events.
2. Start a session at the first deep-work event.
3. Keep the session alive across any non-deep stretch **shorter than 5 minutes** (a quick Slack
   glance or notification is absorbed). A non-deep stretch **≥5 minutes ends the session.**
4. A completed session counts as "serious productivity" only if its total span **≥25 minutes.**
5. **Guard:** if absorbed non-deep time exceeds **20% of the span**, the block doesn't qualify —
   prevents a half-scrolled "session" from counting.

Outputs per day: list of qualified sessions (start, end, deep minutes, absorbed minutes),
total deep-in-session minutes, session count, longest streak.

---

## 4. Focus score (0–100)

Three **independent** signals, blended. Transparent on purpose — an arbitrary score is meaningless.

```
Focus Score = 100 × ( 0.45·Depth + 0.25·Consistency + 0.30·Cleanliness )

Depth        = min( deep_session_minutes / 240 , 1 )      # 4h of in-session deep work = full
Consistency  = min( longest_session_minutes / 90 , 1 )    # a sustained 90-min block = full
Cleanliness  = 1 − min( distraction_minutes / active_minutes , 0.5 ) / 0.5   # ≥50% distraction = 0
```

- **Depth** rewards *volume* of real focus (not just avoiding distraction on a light day).
- **Consistency** rewards *sustained* focus — the "serious productivity period" you care about.
- **Cleanliness** is the distraction lever; it bites hard (50% of active time wasted → 0).

Calibration check: a solid dev day (4h deep work, a 90-min streak, <10% distraction) ≈ **95**.
Today's sample (≈1h scattered deep, ~54% distraction) ≈ **22**. Weights are tunable in `config`.

---

## 5. Daily report (Telegram, compact)

Sent on a daily cadence. Mock-up:

```
📊 Focus Score: 22/100   ▼ 19 vs yesterday
🗓 2026-06-07 · active 3h 02m

🟩 Deep         1h 00m   2 sessions · longest 40m
🟦 Supporting      —
🟥 Distraction  1h 39m
⬜ Neutral         3m

Timeline (09→18h):
🟩🟩🟥🟥 🟦🟩🟥🟥 🟥🟥⬜🟩
└ longest focus 09:10–09:50

💡 YouTube after 14:00 broke your only long session.
   Block video 14:00–16:00 tomorrow?
```

Elements: headline score + trend arrow; tier totals; tier bars (or emoji blocks); session
summary; an hourly timeline strip; one coaching insight.

---

## 6. Weekly report (richer)

Sent once a week. Mock-up:

```
📈 Week of Jun 1–7
Avg Focus 38  ▲ +6 vs prior week
Deep work 14h 20m · best day Wed (71)

Mon ▓▓░░░ 34
Tue ▓▓▓░░ 52
Wed ▓▓▓▓░ 71
Thu ▓▓░░░ 36
Fri ▓▓▓░░ 48
Sat ▓░░░░ 18
Sun ▓░░░░ 12

Tiers:  Deep 14h20 · Supporting 6h10 · Distraction 9h45
Top distractions:  youtube.com 4h00 · reddit.com 2h10 · x.com 1h30
Best session:  Wed 09:05–10:40 (1h35 deep)

💡 Deep work clusters before noon; afternoons average 60% distraction.
💡 Sessions shortened mid-week — protect a no-meeting morning block.
```

Elements: weekly avg score + trend; per-day score bars; weekly deep-work total + best day; tier
breakdown; top distraction sources; best session; 1–2 coaching insights.

---

## 7. Coaching insights

1–2 per report, **specific and data-derived** — never generic. Pattern types to mine:
- Time-of-day focus pattern ("best focus 09–11; protect it").
- Recurring focus-breaker ("YouTube after lunch is your top session-killer").
- Trend shift ("sessions 20% shorter than last week").
- Plan adherence (planned vs actual focus blocks).

These run on **aggregates only** — no raw titles needed — so they're a safe Gemini use even on
the free tier. This is the natural home for the LLM in v1's reporting.

---

## 8. Notes for Claude Code

- Categorization is **rule-based + manual whitelist**, not LLM auto-classification. Unknown =
  distraction. The LLM only (a) generates coaching insights from aggregates, and (b) optionally
  *suggests* whitelist promotions for one-tap approval.
- Implement specificity-ordered rule resolution (path > domain > app default).
- The agent needs to capture enough URL detail (path/subreddit/channel) for whitelist matching,
  but still only sends domain + title off-device, and never for sensitive-flagged apps.
- Store score weights, deep-work thresholds (25m / 5m / 20% / 240m / 90m targets), and nudge
  times in `config` so they're tunable without code changes.
- Daily report is text/emoji (renders in Telegram); don't over-engineer it into images.
