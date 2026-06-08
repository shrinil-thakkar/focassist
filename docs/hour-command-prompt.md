# Claude Code prompt — `/hour` command (full version)

Implement a `/hour` Telegram command that shows, for a given hour of today, exactly which
apps/sites I used and for how long, broken down by tier. This extends the ingest pipeline to
store **hourly app/domain aggregates** (not just daily totals). Keep it consistent with the
existing tier model (deep / supporting / distraction / neutral), the ruleset/whitelist
resolution, and the privacy rule that **only aggregated data leaves the Mac — no raw URLs,
paths, or window titles.**

## 1. New data: hourly aggregates

Add a table:

```
hourly_activity(
  date        TEXT,      -- YYYY-MM-DD, local
  hour        INTEGER,   -- 0..23, local clock hour
  source      TEXT,      -- 'app' | 'domain'
  name        TEXT,      -- e.g. 'Visual Studio Code' or 'youtube.com' (domain-level only)
  tier        TEXT,      -- 'deep' | 'supporting' | 'distraction' | 'neutral'
  category    TEXT,      -- 'dev' | 'comms' | 'video' | ...
  minutes     INTEGER
)
```
Index on `(date, hour)`. Aggregate key is `(date, hour, source, name, tier, category)`.

**Why `tier` is part of the key, not derived at read time:** whitelist rules resolve at
path/subreddit/channel granularity on the agent, but only domain-level names leave the Mac. So
a single domain can legitimately split across tiers in the same hour (e.g. a whitelisted
`youtube.com/@ConfTalks` portion is `deep`, the rest is `distraction`). Storing
`(name, tier)` preserves that split without ever sending the path off-device.

## 2. Agent-side computation

The agent already holds raw ActivityWatch events with timestamps locally. For each event:
- Resolve tier + category via the existing ruleset (including whitelist path rules).
- Assign minutes to the event's **local clock hour**; if an event spans an hour boundary, split
  its minutes across the hours it covers.
- Aggregate to `(hour, source, name, tier, category) → minutes`.

The agent recomputes the **full day's** `hourly_activity` on each push (don't send deltas).

## 3. Extend the `/ingest` payload

Add `hourly_activity[]` alongside the fields already sent. Full current shape:

```json
{
  "date": "2026-06-07",
  "aggregates": [ {"category":"dev","app":"...","domain":"...","minutes":42} ],
  "sessions":   [ {"start":"09:10","end":"09:52","deep_min":40,"absorbed_min":2} ],
  "timeline":   [ {"bucket":"09:00","tier":"deep"} ],
  "hourly_activity": [
    {"hour":9,"source":"app","name":"Visual Studio Code","tier":"deep","category":"dev","minutes":42},
    {"hour":9,"source":"domain","name":"youtube.com","tier":"distraction","category":"video","minutes":6}
  ]
}
```

Backend on ingest: **replace all `hourly_activity` rows for that `date`** (idempotent, since the
agent sends the full day each time). Optional: derive the existing daily `activity` totals from
`hourly_activity` to avoid storing the same data twice — only if it doesn't force a wider refactor.

## 4. The command

Accept, all resolving to a single local hour of **today**:
- `/hour 14` (24h), `/hour 2pm` / `/hour 9am` (am/pm), `/hour 9` (bare = 09:00)
- `/hour` with no arg → the **current** hour
- `/hour now` → current hour

Validation:
- Out of range → "Give me an hour 0–23 (or like `2pm`)."
- Future hour today → "That hour hasn't happened yet."
- Hour with no tracked activity → "Nothing tracked 14:00–15:00 — idle or laptop closed."
- Current hour is partial → label it "(so far)" and note minutes accounted vs the wall-clock elapsed.

Use the local timezone from `config`.

## 5. Display (Telegram)

```
🕐 14:00–15:00 · 2026-06-07

🟩 Deep         28m
🟦 Supporting    9m
🟥 Distraction  18m
⬜ Neutral       5m
60m accounted

15-min: 🟩🟩🟥🟦

By app/site
🟩 Visual Studio Code   24m
🟩 github.com            4m
🟦 Slack                 9m
🟥 youtube.com          14m
🟥 reddit.com            4m
⬜ Finder                5m

⏱ Focus session 13:40–14:22 overlaps this hour (deep ✓)
```

Rules for the view:
- Tier totals first, then the four 15-min tier buckets for this hour (reuse the daily timeline
  logic), then the per-app/site list.
- Sort the app/site list by minutes descending; prefix each with its tier emoji; same name can
  appear under two tiers (the whitelist split) — that's expected, don't merge them.
- If any focus session (from `sessions[]`) overlaps the hour, show it on the last line.
- Keep it text/emoji — it must render in Telegram, don't generate an image.

## 6. Migration / consistency

- Reuse the tier↔category mapping and rule-resolution code that already exists; do not duplicate it.
- Fold the new table into the same migration that recreates `rules` and backfills the old
  category names, if that work is still pending — one migration, not several.
- This changes the agent↔backend contract: `BUILD_SPEC.md §11` should gain `hourly_activity[]`
  in the `/ingest` body and the new table in the data model. Flag it; I'll update the docs.

## 7. Privacy (do not regress)

- Hourly aggregates are **name (app or domain) + tier + minutes** only. No raw URLs, no paths,
  no window titles leave the Mac — path-level whitelist info is reflected solely via the tier split.
- This data goes only to my own backend and is shown only to me in Telegram; it is **never** sent
  to the LLM, so the sensitive-app skip list (which gates LLM exposure) needs no special handling here.
