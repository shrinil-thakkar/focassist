"""
Part 1 Verification Suite — Automatic Invariants (docs/part1-verification-suite.md §A).

Run with: python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent.categorizer import load_rules
from agent.timeline import resolve_timeline, detect_flags, DEFAULT_CONFIG
from agent.session_detector import build_daily_aggregates, build_hourly_aggregates
from backend.scoring import _top_items, _tier_totals

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # 09:00 UTC = 14:30 IST


def _ev(offset_min: float, duration_min: float, data: dict) -> dict:
    ts = T0 + timedelta(minutes=offset_min)
    return {"timestamp": ts.isoformat().replace("+00:00", "Z"),
            "duration": duration_min * 60, "data": data}


def _afk(offset_min, duration_min, status):
    return _ev(offset_min, duration_min, {"status": status})


def _win(offset_min, duration_min, app, title=""):
    return _ev(offset_min, duration_min, {"app": app, "title": title})


def _web(offset_min, duration_min, url, app="Google Chrome", title=""):
    return _ev(offset_min, duration_min, {"app": app, "url": url, "title": title})


class I1I2I3Tests(unittest.TestCase):
    """I1 Completeness, I2 No overlap, I3 Tier partition — run together on each fixture."""

    def setUp(self):
        load_rules()

    # ── shared assertion helpers ──────────────────────────────────────────────

    def _i1(self, result, elapsed_min):
        total = result["active_minutes"] + result["idle_minutes"] + result["untracked_minutes"]
        self.assertAlmostEqual(total, elapsed_min, delta=1.0,
                               msg=f"I1 Completeness: {total:.2f}m ≠ {elapsed_min:.2f}m elapsed")

    def _i2(self, result, start, end):
        tl = result["timeline"]
        self.assertTrue(tl, "I2: timeline is empty")
        self.assertEqual(tl[0]["start"], start, "I2: first interval doesn't start at range_start")
        self.assertEqual(tl[-1]["end"], end, "I2: last interval doesn't end at range_end")
        for i, (prev, nxt) in enumerate(zip(tl, tl[1:])):
            self.assertEqual(prev["end"], nxt["start"],
                             f"I2: gap/overlap between intervals {i} and {i+1}: "
                             f"{prev['end']} != {nxt['start']}")

    def _i3(self, result):
        valid_tiers = {"deep", "supporting", "distraction", "neutral"}
        for iv in result["timeline"]:
            if iv["state"] == "active":
                self.assertIn(iv["tier"], valid_tiers,
                              f"I3: Unknown tier '{iv['tier']}' on active interval")
        tier_sum_min = sum(
            (iv["end"] - iv["start"]).total_seconds() / 60
            for iv in result["timeline"] if iv["state"] == "active"
        )
        self.assertAlmostEqual(tier_sum_min, result["active_minutes"], delta=1.0,
                               msg=f"I3 Tier partition: {tier_sum_min:.2f}m ≠ active={result['active_minutes']:.2f}m")

    def _check_all(self, result, start, end, elapsed_min):
        self._i1(result, elapsed_min)
        self._i2(result, start, end)
        self._i3(result)

    # ── test scenarios ────────────────────────────────────────────────────────

    def test_all_active(self):
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code")],
            "web": [],
        }
        self._check_all(resolve_timeline(events, range_start=start, range_end=end), start, end, 60)

    def test_all_idle(self):
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "afk")],
            "window": [],
            "web": [],
        }
        self._check_all(resolve_timeline(events, range_start=start, range_end=end), start, end, 60)

    def test_all_untracked(self):
        start, end = T0, T0 + timedelta(hours=1)
        events = {"afk": [], "window": [], "web": []}
        self._check_all(resolve_timeline(events, range_start=start, range_end=end), start, end, 60)

    def test_mixed_active_idle_untracked(self):
        start, end = T0, T0 + timedelta(hours=4)
        events = {
            "afk": [
                _afk(0, 60, "not-afk"),
                _afk(60, 60, "afk"),
                # 120-180: untracked gap (no AFK event)
                _afk(180, 60, "not-afk"),
            ],
            "window": [
                _win(0, 60, "Visual Studio Code"),
                _win(60, 60, "Visual Studio Code"),  # during afk — discarded
                _win(180, 60, "Slack"),
            ],
            "web": [],
        }
        r = resolve_timeline(events, range_start=start, range_end=end)
        self._check_all(r, start, end, 240)
        self.assertAlmostEqual(r["active_minutes"], 120, delta=1.0)
        self.assertAlmostEqual(r["idle_minutes"], 60, delta=1.0)
        self.assertAlmostEqual(r["untracked_minutes"], 60, delta=1.0)

    def test_browser_with_web_events(self):
        start, end = T0, T0 + timedelta(hours=2)
        events = {
            "afk": [_afk(0, 120, "not-afk")],
            "window": [_win(0, 120, "Google Chrome")],
            "web": [
                _web(0, 60, "https://github.com/me/repo"),
                # 60-90: gap → browser-unlabeled
                _web(90, 30, "https://youtube.com/watch?v=abc"),
            ],
        }
        r = resolve_timeline(events, range_start=start, range_end=end)
        self._check_all(r, start, end, 120)

    def test_passive_override_zoom(self):
        start, end = T0, T0 + timedelta(hours=2)
        events = {
            "afk": [_afk(0, 120, "afk")],
            "window": [_win(0, 120, "Zoom")],
            "web": [],
        }
        r = resolve_timeline(events, range_start=start, range_end=end)
        self._check_all(r, start, end, 120)

    def test_hour_boundary_spanning_event(self):
        """Activity that crosses an hour boundary is split but totals still reconcile."""
        start = T0
        end = T0 + timedelta(hours=2)
        # Single 90-min window event spanning across the first hour boundary
        events = {
            "afk": [_afk(0, 120, "not-afk")],
            "window": [_win(0, 90, "Terminal"), _win(90, 30, "Slack")],
            "web": [],
        }
        r = resolve_timeline(events, range_start=start, range_end=end)
        self._check_all(r, start, end, 120)


class I4TopListTests(unittest.TestCase):
    """I4: _top_items sum ≤ tier total — catches display duplicates like Telegram/web.telegram.org."""

    def setUp(self):
        load_rules()

    def _check_i4(self, aggregates):
        totals = _tier_totals(aggregates)
        for tier in ("deep", "supporting", "distraction"):
            top = _top_items(aggregates, tier, n=10)
            top_sum = sum(mins for _, mins in top)
            tier_total = totals.get(tier, 0)
            self.assertLessEqual(
                top_sum, tier_total + 1.0,
                msg=f"I4 {tier}: top_sum={top_sum:.1f}m > tier_total={tier_total:.1f}m "
                    f"(display duplicate detected)"
            )

    def test_telegram_web_no_double_count(self):
        """Telegram native + web.telegram.org browser must not both appear in supporting total."""
        start, end = T0, T0 + timedelta(hours=2)
        events = {
            "afk": [_afk(0, 120, "not-afk")],
            "window": [_win(0, 60, "Telegram"), _win(60, 60, "Google Chrome")],
            "web": [_web(60, 60, "https://web.telegram.org/k/")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        aggregates = build_daily_aggregates(events, resolved=resolved)
        self._check_i4(aggregates)

    def test_multi_app_single_tier(self):
        start, end = T0, T0 + timedelta(hours=3)
        events = {
            "afk": [_afk(0, 180, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code"),
                       _win(60, 60, "Terminal"),
                       _win(120, 60, "Cursor")],
            "web": [],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        aggregates = build_daily_aggregates(events, resolved=resolved)
        self._check_i4(aggregates)

    def test_mixed_tiers(self):
        start, end = T0, T0 + timedelta(hours=3)
        events = {
            "afk": [_afk(0, 180, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code"),
                       _win(60, 60, "Slack"),
                       _win(120, 60, "Google Chrome")],
            "web": [_web(120, 60, "https://youtube.com/watch?v=abc")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        aggregates = build_daily_aggregates(events, resolved=resolved)
        self._check_i4(aggregates)


class I5I6HourlyDailyTests(unittest.TestCase):
    """
    I5: sum of hourly_activity per tier == daily_aggregates per tier.
    I6: within each hour, per-app/site minutes sum to that hour's active total.
    Note: build_daily_aggregates excludes neutral; build_hourly_aggregates includes it.
    Checks cover deep/supporting/distraction only.
    """

    def setUp(self):
        load_rules()

    def _hourly_totals_by_tier(self, hourly):
        totals: dict[str, float] = {}
        for item in hourly:
            if item["tier"] == "neutral":
                continue
            t = item["tier"]
            totals[t] = totals.get(t, 0) + item["minutes"]
        return totals

    def test_i5_per_tier(self):
        start, end = T0, T0 + timedelta(hours=3)
        events = {
            "afk": [_afk(0, 180, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code"),
                       _win(60, 30, "Slack"),
                       _win(90, 60, "Google Chrome"),
                       _win(150, 30, "Terminal")],
            "web": [_web(90, 60, "https://github.com/me/repo")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        daily = build_daily_aggregates(events, resolved=resolved)
        hourly = build_hourly_aggregates(events, resolved=resolved)

        daily_totals = _tier_totals(daily)
        hourly_totals = self._hourly_totals_by_tier(hourly)

        for tier in ("deep", "supporting", "distraction"):
            dt = daily_totals.get(tier, 0)
            ht = hourly_totals.get(tier, 0)
            self.assertAlmostEqual(dt, ht, delta=1.0,
                                   msg=f"I5 Hourly↔Daily mismatch for '{tier}': "
                                       f"daily={dt:.1f}m, hourly={ht:.1f}m")

    def test_i5_overall_active(self):
        start, end = T0, T0 + timedelta(hours=4)
        events = {
            "afk": [_afk(0, 90, "not-afk"),
                    _afk(90, 30, "afk"),
                    _afk(120, 120, "not-afk")],
            "window": [_win(0, 90, "Visual Studio Code"),
                       _win(90, 30, "Zoom"),  # passive override (afk + Zoom)
                       _win(120, 120, "Slack")],
            "web": [],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        daily = build_daily_aggregates(events, resolved=resolved)
        hourly = build_hourly_aggregates(events, resolved=resolved)

        daily_total = sum(a["minutes"] for a in daily)
        hourly_total = sum(h["minutes"] for h in hourly if h["tier"] != "neutral")
        self.assertAlmostEqual(daily_total, hourly_total, delta=1.0,
                               msg=f"I5 Total active: daily={daily_total:.1f}m, hourly={hourly_total:.1f}m")

    def test_i6_per_hour_does_not_exceed_60min(self):
        """I6 sanity: no hour can have more active minutes than exist in 60 minutes."""
        start, end = T0, T0 + timedelta(hours=4)
        events = {
            "afk": [_afk(0, 240, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code"),
                       _win(60, 60, "Slack"),
                       _win(120, 60, "Terminal"),
                       _win(180, 60, "Cursor")],
            "web": [],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        hourly = build_hourly_aggregates(events, resolved=resolved)

        by_hour: dict[int, float] = {}
        for item in hourly:
            h = item["hour"]
            by_hour[h] = by_hour.get(h, 0) + item["minutes"]

        for h, total in by_hour.items():
            self.assertLessEqual(total, 61.0,
                                 msg=f"I6: Hour {h:02d} has {total:.1f}m active > 60m (impossible)")

    def test_i6_hourly_matches_timeline_per_hour(self):
        """I6: sum of hourly items for each IST hour == active minutes from the resolved timeline."""
        start, end = T0, T0 + timedelta(hours=3)
        events = {
            "afk": [_afk(0, 180, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code"),
                       _win(60, 60, "Slack"),
                       _win(120, 60, "Terminal")],
            "web": [],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        hourly = build_hourly_aggregates(events, resolved=resolved)

        # Recompute expected per-IST-hour active from the timeline (same logic as build_hourly_aggregates)
        expected: dict[int, float] = {}
        for iv in resolved["timeline"]:
            if iv["state"] != "active":
                continue
            ev_s = iv["start"].astimezone(IST)
            ev_e = iv["end"].astimezone(IST)
            cur = ev_s
            while cur < ev_e:
                hr_end = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                seg_end = min(ev_e, hr_end)
                seg_min = (seg_end - cur).total_seconds() / 60.0
                if seg_min > 0:
                    expected[cur.hour] = expected.get(cur.hour, 0) + seg_min
                cur = seg_end

        actual: dict[int, float] = {}
        for item in hourly:
            h = item["hour"]
            actual[h] = actual.get(h, 0) + item["minutes"]

        for h, exp in expected.items():
            act = actual.get(h, 0)
            self.assertAlmostEqual(exp, act, delta=1.0,
                                   msg=f"I6 Hour {h:02d}: expected={exp:.1f}m, got={act:.1f}m")


class I7BrowserSingleSourceTests(unittest.TestCase):
    """I7: no interval contributes both a window-app entry and a web-domain entry."""

    def setUp(self):
        load_rules()

    def test_no_double_count_when_web_covers_all(self):
        """Chrome 60 min + github.com web 60 min → 60 min total, not 120."""
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "not-afk")],
            "window": [_win(0, 60, "Google Chrome")],
            "web": [_web(0, 60, "https://github.com/me/repo")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        aggregates = build_daily_aggregates(events, resolved=resolved)

        total = sum(a["minutes"] for a in aggregates)
        self.assertAlmostEqual(total, 60.0, delta=2.0,
                               msg=f"I7: Double-count: {total:.1f}m ≠ 60m")
        self.assertTrue(any(a.get("domain") == "github.com" for a in aggregates),
                        "I7: github.com entry missing")
        # No bare Chrome window entry without domain should survive
        self.assertFalse(
            any(a["app"] == "Google Chrome" and not a.get("domain")
                and a.get("category") != "browser-unlabeled"
                for a in aggregates),
            "I7: Raw Chrome window entry survived browser override"
        )

    def test_each_browser_interval_is_domain_or_unlabeled(self):
        """Every Chrome-app active interval is either domain-labeled or browser-unlabeled."""
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "not-afk")],
            "window": [_win(0, 60, "Google Chrome")],
            "web": [_web(0, 40, "https://github.com/me/repo")],  # gap 40-60: unlabeled
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        browser_names = set(DEFAULT_CONFIG["browser_app_names"])
        for iv in resolved["timeline"]:
            if iv["state"] != "active" or iv.get("app") not in browser_names:
                continue
            has_domain = bool(iv.get("domain"))
            is_unlabeled = iv.get("category") == "browser-unlabeled"
            self.assertTrue(
                has_domain or is_unlabeled,
                f"I7: Chrome interval at {iv['start']} is neither domain-labeled "
                f"nor browser-unlabeled (domain={iv.get('domain')!r}, "
                f"category={iv.get('category')!r})"
            )

    def test_partial_web_coverage_splits_correctly(self):
        """Chrome 60 min, web 40 min → ~40m domain + ~20m unlabeled = ~60m, no leftover."""
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "not-afk")],
            "window": [_win(0, 60, "Google Chrome")],
            "web": [_web(0, 40, "https://github.com/me/repo")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        aggregates = build_daily_aggregates(events, resolved=resolved)

        domain_min = sum(a["minutes"] for a in aggregates if a.get("domain") == "github.com")
        unlabeled_min = sum(a["minutes"] for a in aggregates if a.get("category") == "browser-unlabeled")

        self.assertAlmostEqual(domain_min, 40.0, delta=2.0,
                               msg=f"I7: github.com={domain_min:.1f}m, expected ~40m")
        self.assertAlmostEqual(unlabeled_min, 20.0, delta=2.0,
                               msg=f"I7: unlabeled={unlabeled_min:.1f}m, expected ~20m")
        self.assertAlmostEqual(domain_min + unlabeled_min, 60.0, delta=2.0,
                               msg=f"I7: total Chrome time mismatch: {domain_min + unlabeled_min:.1f}m ≠ 60m")


class I8CoverageDefinitionTests(unittest.TestCase):
    """I8: url_coverage = extension-sourced URLs / total Chrome time (title-derived names excluded)."""

    def setUp(self):
        load_rules()

    def test_extension_down_gives_zero_coverage(self):
        """Chrome active + title says 'GitHub' but no web events → coverage = 0%, not inflated."""
        start, end = T0, T0 + timedelta(minutes=30)
        events = {
            "afk": [_afk(0, 30, "not-afk")],
            "window": [_win(0, 30, "Google Chrome", title="GitHub - my-repo · Pull Requests")],
            "web": [],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        flags = detect_flags(resolved)

        flag = next((f for f in flags if f["type"] == "chrome_unlabeled"), None)
        self.assertIsNotNone(flag, "I8: No chrome_unlabeled flag when extension is down")
        coverage = flag.get("coverage", 1.0)
        self.assertAlmostEqual(coverage, 0.0, delta=0.01,
                               msg=f"I8: Coverage {coverage:.1%} — title-derived labels are being counted")

    def test_extension_up_gives_full_coverage(self):
        """Chrome + matching web events → 100% coverage, no chrome_unlabeled flag."""
        start, end = T0, T0 + timedelta(minutes=30)
        events = {
            "afk": [_afk(0, 30, "not-afk")],
            "window": [_win(0, 30, "Google Chrome")],
            "web": [_web(0, 30, "https://github.com/me/repo")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        flags = detect_flags(resolved)

        flag = next((f for f in flags if f["type"] == "chrome_unlabeled"), None)
        self.assertIsNone(flag, f"I8: Unexpected chrome_unlabeled flag at 100% coverage: {flag}")

    def test_below_threshold_fires_flag(self):
        """Chrome 30 min, extension covers only 10 min → 33% coverage → flag fires."""
        start, end = T0, T0 + timedelta(minutes=30)
        events = {
            "afk": [_afk(0, 30, "not-afk")],
            "window": [_win(0, 30, "Google Chrome")],
            "web": [_web(0, 10, "https://github.com/me/repo")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        flags = detect_flags(resolved)

        flag = next((f for f in flags if f["type"] == "chrome_unlabeled"), None)
        self.assertIsNotNone(flag, "I8: No chrome_unlabeled flag at ~33% coverage")
        coverage = flag.get("coverage", 1.0)
        self.assertAlmostEqual(coverage, 1 / 3, delta=0.05,
                               msg=f"I8: Coverage {coverage:.2f} ≠ expected ~0.33")

    def test_above_threshold_no_flag(self):
        """Chrome 30 min, extension covers 20 min → 67% coverage → flag must not fire."""
        start, end = T0, T0 + timedelta(minutes=30)
        events = {
            "afk": [_afk(0, 30, "not-afk")],
            "window": [_win(0, 30, "Google Chrome")],
            "web": [_web(0, 20, "https://github.com/me/repo")],
        }
        resolved = resolve_timeline(events, range_start=start, range_end=end)
        flags = detect_flags(resolved)

        flag = next((f for f in flags if f["type"] == "chrome_unlabeled"), None)
        self.assertIsNone(flag,
                          f"I8: chrome_unlabeled fired at 67% coverage (above 50% threshold): {flag}")


if __name__ == "__main__":
    unittest.main()
