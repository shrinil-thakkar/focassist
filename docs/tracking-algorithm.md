# Tracking & Time-Accounting Algorithm (foundation)

The correctness layer everything else (sessions, focus score, reports) sits on. Scope: **one
person, one Mac, laptop screen-time only.** Not a record of total work done — offline meetings,
whiteboarding, and phone use are out of scope and must show up as *untracked*, never as zero.

Watchers in use: `aw-watcher-afk`, `aw-watcher-window`, `aw-watcher-web-chrome`. **Chrome only.**

---

## 1. Three states of time (the core honesty rule)

Every minute of wall-clock falls into exactly one:

- **Active** — you were using the laptop (input recently, or an engaged passive context; see §3).
- **Idle** — laptop on, but you were away / not engaged. Shown, not counted toward productivity.
- **Untracked** — no data: system asleep, lid closed, watcher/aw-server down, or a crash.

The cardinal rule: **idle and untracked are never merged, and untracked is never silently
treated as zero activity.** A confident number built on partial data is worse than an honestly
incomplete one.

---

## 2. The merge pipeline

AFK is the **master clock**; window and web are layered on top. The window watcher reports the
focused app even after you've walked away, so window data alone over-counts — the AFK intersect
is what fixes that.

1. **Partition from AFK.** Split the timeline into `not-afk` (input within the timeout → candidate
   active) and `afk` (no input past the timeout → candidate idle). Any wall-clock interval with
   **no AFK events at all** is **untracked** (sleep/crash/off) — construct this third state
   explicitly; the watchers don't hand it to you.
2. **Window ∩ not-afk.** Keep only the parts of window events that overlap `not-afk`. This is the
   active-app timeline. Window time during `afk` is discarded (kills "idle with VS Code open").
3. **Browser override.** Where the focused app is Chrome, replace "Google Chrome" with the
   `aw-watcher-web-chrome` URL for that interval → domain-level activity. If Chrome is focused but
   **no web event exists**, label the interval `browser-unlabeled` and **flag it** (see §5) — do
   not let it fall through to the default distraction bucket silently.
4. **Passive-engagement override** (see §3). Reclassify specific `afk` intervals back to active.

Output: an active timeline of `{app-or-domain, tier}` plus explicit **idle** and **untracked**
totals.

---

## 3. Passive-engagement override (your choice: keep engaged passive time active)

The problem: a 40-min conf talk or a Zoom call with no keyboard/mouse input looks identical to
being away, so plain AFK erases it.

**Rule:** during an `afk` interval, if the focused context is in the **engaged set**, reclassify
it as **active**, capped at a maximum continuous override (default **45 min**); any excess beyond
the cap reverts to idle (you probably left it running).

**Engaged set** (tunable in config):
- **Meeting apps:** Zoom, `meet.google.com`, Microsoft Teams.
- **Whitelisted-deep media only** — e.g. a YouTube channel you've promoted to deep. Tier follows
  the existing rule (whitelisted talk → deep).

**Everything else during `afk` stays idle** — crucially, this includes *non-whitelisted*
entertainment video left autoplaying. That's the key guard: we never count a video you walked
away from as distraction, and we only rescue contexts you've signalled are real work. This is
what lets us be "accurate" without re-opening the autoplay over-count.

*Residual limitation (acceptable):* a meeting you sit through silently with zero mouse movement
for >45 min gets truncated at the cap. Rare — most calls involve some movement, which keeps you
`not-afk` naturally. The cap is tunable if it bites.

---

## 4. Config knobs (so accuracy is tunable without code changes)

- `afk_timeout` — input gap before "away". Default **~180s (3 min)**. Confirm your installed value.
- `engaged_apps` — meeting/media contexts that override afk.
- `override_cap_minutes` — max continuous passive override. Default **45**.
- `browser_app_names` — `["Google Chrome", "Google Chrome Canary"]`. Anything not in here is a
  non-instrumented browser (see §5).
- `url_coverage_flag_threshold` — see §5.

---

## 5. Health & coverage detection (catching silent failures)

Silent non-measurement is real and partly structural — system sleep is *expected* (→ untracked),
but these need active detection:

- **Chrome extension down / unlabeled browser.** If Chrome-focused active time has URL coverage
  below `url_coverage_flag_threshold` (e.g. <50%) over a rolling window, raise a flag — otherwise
  all your GitHub/docs time collapses into one blob that default-to-distraction then blames you for.
- **Non-Chrome browser in use.** You only run Chrome, so any focused app that looks like a browser
  but isn't in `browser_app_names` (Safari, Arc, Firefox) → flag as unlabeled browser time.
- **High untracked during expected hours.** Not an error, but worth surfacing so a half-tracked
  day isn't read as a lazy one.

---

## 6. Reconciliation (the trust check)

For any hour and for the day: **active + idle + untracked must equal elapsed wall-clock** while
the machine was on. Run this as an assertion in tests and surface coverage in reports
("tracked 6h 10m; idle 40m; untracked 1h 10m"). If per-app minutes don't sum to the hour, or the
hours don't sum to the day, the pipeline has a leak — usually a mishandled afk-boundary split.

---

## 7. Edge cases

- **Event spans an afk or hour boundary** — split its minutes proportionally; keep this split
  identical across the daily totals, the 15-min timeline, and the hourly aggregates.
- **Screen locked** — typically triggers afk, then sleep → idle, then untracked. Treat normally.
- **Multiple displays / Spaces** — the window watcher tracks only the focused window; that's fine
  for one-person-one-screen-of-attention.
- **The over-count tail** — the last few minutes before going afk count as active. Small and
  acceptable; don't bother trimming in v1.

---

## 8. What this deliberately cannot see (state plainly to the user)

Laptop screen only. Offline meetings, whiteboarding, reading on paper, and phone time are
invisible **by design** and surface as untracked — not as wasted or idle time. The number is an
honest estimate of *on-laptop* productivity, nothing more.

---

## 9. Implementation note for Claude Code

- If the agent currently aggregates window events directly, the **highest-priority fix is §2 step
  2** (the AFK intersect) — without it, idle time is being counted as work right now.
- Implement §2 and §3 as one timeline-resolution pass that emits `{interval, app|domain, tier,
  state}`; sessions, hourly aggregates, and the score all consume its output, so they inherit
  correctness instead of each re-deriving it.
- Put §6 reconciliation in the test suite, not just at runtime.
