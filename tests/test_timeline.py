"""
Tests for the AFK-anchored merge pipeline (docs/tracking-algorithm.md §2-3, §6).

Run with: python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta, timezone

from agent.categorizer import load_rules
from agent.timeline import resolve_timeline, partition_afk, DEFAULT_CONFIG

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # 09:00 UTC


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


class ReconciliationTests(unittest.TestCase):
    """§6 — active + idle + untracked must equal elapsed wall-clock, always."""

    def setUp(self):
        load_rules()

    def _assert_reconciles(self, result, range_start, range_end):
        elapsed_min = (range_end - range_start).total_seconds() / 60.0
        total = result["active_minutes"] + result["idle_minutes"] + result["untracked_minutes"]
        self.assertAlmostEqual(total, elapsed_min, places=2)

        # Timeline itself must be gapless and non-overlapping over the range.
        timeline = result["timeline"]
        self.assertTrue(timeline)
        self.assertEqual(timeline[0]["start"], range_start)
        self.assertEqual(timeline[-1]["end"], range_end)
        for prev, nxt in zip(timeline, timeline[1:]):
            self.assertEqual(prev["end"], nxt["start"])

    def test_pure_not_afk_with_window_events(self):
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "not-afk")],
            "window": [_win(0, 60, "Visual Studio Code")],
            "web": [],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        self._assert_reconciles(result, start, end)
        self.assertAlmostEqual(result["active_minutes"], 60, places=1)
        self.assertEqual(result["idle_minutes"], 0)
        self.assertEqual(result["untracked_minutes"], 0)

    def test_afk_with_vscode_open_does_not_count_as_active(self):
        """The headline bug from §9: window time during afk must be discarded."""
        start, end = T0, T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "afk")],
            "window": [_win(0, 60, "Visual Studio Code")],  # focused app survives the walk-away
            "web": [],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        self._assert_reconciles(result, start, end)
        self.assertEqual(result["active_minutes"], 0)
        self.assertAlmostEqual(result["idle_minutes"], 60, places=1)

    def test_gap_with_no_afk_events_is_untracked(self):
        start, end = T0, T0 + timedelta(hours=2)
        events = {
            # only covers the first hour — the second hour has no AFK data at all
            "afk": [_afk(0, 60, "not-afk")],
            "window": [_win(0, 60, "Terminal")],
            "web": [],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        self._assert_reconciles(result, start, end)
        self.assertAlmostEqual(result["untracked_minutes"], 60, places=1)
        states = [iv["state"] for iv in result["timeline"]]
        self.assertIn("untracked", states)

    def test_mixed_day_reconciles(self):
        start, end = T0, T0 + timedelta(hours=3)
        events = {
            "afk": [
                _afk(0, 50, "not-afk"),
                _afk(50, 40, "afk"),
                # 90-150 untracked gap
                _afk(150, 30, "not-afk"),
            ],
            "window": [
                _win(0, 50, "Visual Studio Code"),
                _win(50, 40, "Visual Studio Code"),  # during afk — discarded
                _win(150, 30, "Slack"),
            ],
            "web": [],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        self._assert_reconciles(result, start, end)
        self.assertAlmostEqual(result["active_minutes"], 80, places=1)   # 50 + 30
        self.assertAlmostEqual(result["idle_minutes"], 40, places=1)
        self.assertAlmostEqual(result["untracked_minutes"], 60, places=1)


class PartitionAfkTests(unittest.TestCase):
    def test_gapless_and_sorted(self):
        start, end = T0, T0 + timedelta(hours=1)
        afk = [_afk(10, 20, "not-afk"), _afk(40, 10, "afk")]
        parts = partition_afk(afk, start, end)
        self.assertEqual(parts[0], (start, T0 + timedelta(minutes=10), "untracked"))
        self.assertEqual(parts[-1][1], end)
        for prev, nxt in zip(parts, parts[1:]):
            self.assertEqual(prev[1], nxt[0])


class PassiveOverrideTests(unittest.TestCase):
    """§3 — engaged contexts during afk get rescued, capped at override_cap_minutes."""

    def setUp(self):
        load_rules()

    def test_zoom_during_afk_is_rescued_up_to_cap(self):
        start = T0
        end = T0 + timedelta(hours=2)
        cap = DEFAULT_CONFIG["override_cap_minutes"]
        events = {
            "afk": [_afk(0, 120, "afk")],
            "window": [_win(0, 120, "Zoom")],
            "web": [],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        active = [iv for iv in result["timeline"] if iv["state"] == "active"]
        self.assertTrue(active)
        self.assertAlmostEqual(sum((iv["end"] - iv["start"]).total_seconds() / 60 for iv in active),
                               cap, places=1)
        self.assertAlmostEqual(result["idle_minutes"], 120 - cap, places=1)

    def test_non_whitelisted_video_left_autoplaying_stays_idle(self):
        """The key guard from §3 — never rescue autoplay you walked away from."""
        start = T0
        end = T0 + timedelta(hours=1)
        events = {
            "afk": [_afk(0, 60, "afk")],
            "window": [_win(0, 60, "Google Chrome")],
            "web": [_web(0, 60, "https://www.youtube.com/watch?v=abc123")],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        self.assertEqual(result["active_minutes"], 0)
        self.assertAlmostEqual(result["idle_minutes"], 60, places=1)


class BrowserOverrideTests(unittest.TestCase):
    """§2 step 3 — Chrome focus resolves to domain-level activity, or browser-unlabeled."""

    def setUp(self):
        load_rules()

    def test_chrome_with_web_event_resolves_to_domain(self):
        start, end = T0, T0 + timedelta(minutes=30)
        events = {
            "afk": [_afk(0, 30, "not-afk")],
            "window": [_win(0, 30, "Google Chrome")],
            "web": [_web(0, 30, "https://github.com/me/repo")],
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        active = [iv for iv in result["timeline"] if iv["state"] == "active"]
        self.assertTrue(any(iv["domain"] == "github.com" and iv["tier"] == "deep" for iv in active))

    def test_chrome_without_web_event_is_flagged_unlabeled(self):
        start, end = T0, T0 + timedelta(minutes=30)
        events = {
            "afk": [_afk(0, 30, "not-afk")],
            "window": [_win(0, 30, "Google Chrome")],
            "web": [],  # extension down — must not silently fall into default distraction
        }
        result = resolve_timeline(events, range_start=start, range_end=end)
        active = [iv for iv in result["timeline"] if iv["state"] == "active"]
        self.assertTrue(any(iv["category"] == "browser-unlabeled" for iv in active))


if __name__ == "__main__":
    unittest.main()
