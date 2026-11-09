"""Pinned status message, journal, /info and the day summary (status_board.py)."""

import datetime
import re
import time
import unittest
from unittest import mock

import _setup
import status_board
from status_board import Board

OK_MEM = {"percent": 48.0, "swap_total_mb": 2048, "swap_used_mb": 174, "used_mb": 480, "total_mb": 1000}
OK_DISK = {"percent": 28.0, "used_gb": 13.0, "total_gb": 45.0}


def board():
    _setup.fresh_state()
    return Board(["arm", "arm-2"], "AD-1")


def balanced(html: str) -> bool:
    for tag in ("b", "i", "blockquote", "tg-time", "tg-spoiler", "pre"):
        opened = len(re.findall(rf"<{tag}[ >]", html))
        if opened != html.count(f"</{tag}>"):
            return False
    return True


@mock.patch("server_stats.memory", lambda: OK_MEM)
@mock.patch("server_stats.disk", lambda path="/": OK_DISK)
class Render(unittest.TestCase):
    def test_quiet_day_shows_only_the_essentials(self):
        b = board()
        b.today.update(tries=10, capacity=10)
        b.next_try = status_board._now()
        text = b.render()
        self.assertIn("🟡 <b>Hunting</b> · ⬜️⬜️ <b>0/2</b>", text)
        self.assertIn("⏭ Next <tg-time", text)
        self.assertIn("📊 Today <b>10</b> tries", text)
        for absent in ("🚦", "⚠️", "🖥", "Σ", "<code>", "tg-spoiler"):
            self.assertNotIn(absent, text)
        self.assertTrue(balanced(text))

    def test_429_line_with_last_time_and_problems_line(self):
        b = board()
        b.today.update(tries=200, capacity=184, throttled=15, errors=1)
        b.attempt("throttled", 90)
        text = b.render()
        self.assertIn("🚦 Throttled (429) <b>16</b> · 8% · last <tg-time", text)
        self.assertIn("⚠️ Errors <b>1</b>", text)
        self.assertNotIn("network", text)

    def test_won_slot_and_done_phase(self):
        b = board()
        b.have = {"arm"}
        b.phase = "done"
        text = b.render()
        self.assertIn("🟢 <b>Done</b> · ✅⬜️ <b>1/2</b>", text)
        self.assertNotIn("Next", text)

    def test_tg_time_fallback_is_never_empty(self):
        when = datetime.datetime(2026, 11, 9, 7, 5, tzinfo=status_board.TZ)
        self.assertTrue(status_board.tg_time(when).endswith(">07:05</tg-time>"))
        self.assertIn('format="dt">09.11 07:05<', status_board.tg_time(when, "dt"))

    def test_server_line_only_when_nearly_full(self):
        b = board()
        self.assertNotIn("🖥", b.render())
        with mock.patch("server_stats.memory", lambda: {**OK_MEM, "percent": 91.0, "swap_used_mb": 1600}):
            line = [l for l in b.render().splitlines() if l.startswith("🖥")][0]
        self.assertEqual(line, "🖥 <b>RAM 91%</b> · <b>swap 1.6 GB</b> · disk 28%")


@mock.patch("server_stats.memory", lambda: OK_MEM)
@mock.patch("server_stats.disk", lambda path="/": OK_DISK)
class Journal(unittest.TestCase):
    def test_newest_first_in_a_collapsed_quote(self):
        b = board()
        b.event("first")
        b.event("second")
        text = b.render()
        quote = text[text.index("<blockquote expandable>"):]
        self.assertLess(quote.index("second"), quote.index("first"))

    def test_stop_then_start_is_one_restarted_line(self):
        b = board()
        b.event("▶️ started")
        b.event("⏹ stopped")
        b.event("▶️ started")
        self.assertEqual([j[1] for j in b.journal], ["▶️ started", "🔄 restarted"])
        b.event("⏹ stopped")
        b.event("▶️ started")
        self.assertEqual(b.journal[-1][1:], ["🔄 restarted", 2])

    def test_late_start_is_not_merged(self):
        b = board()
        b.event("⏹ stopped")
        b.journal[-1][0] = time.time() - status_board.RESTART_MERGE_S - 1
        b.event("▶️ started")
        self.assertEqual([j[1] for j in b.journal], ["⏹ stopped", "▶️ started"])

    def test_outage_becomes_a_range(self):
        b = board()
        b.event(status_board.OUTAGE_PREFIX + "09:31")
        b.outage_over()
        self.assertRegex(b.journal[-1][1], r"^📡 OCI unreachable 09:31–\d\d:\d\d$")

    def test_old_429_lines_and_2_element_entries_load_cleanly(self):
        _setup.fresh_state()
        b = Board(["arm", "arm-2"], "AD-1")
        b.journal = [[1.0, "▶️ started"], [2.0, "⏳ 429 TooManyRequests, retried", 13]]
        b.save()
        again = Board(["arm", "arm-2"], "AD-1")
        self.assertEqual(again.journal, [[1.0, "▶️ started", 1]])

    def test_event_text_is_escaped(self):
        b = board()
        b.event("❌ OCI service error: <weird>")
        self.assertIn("&lt;weird&gt;", b.render())


@mock.patch("server_stats.memory", lambda: OK_MEM)
@mock.patch("server_stats.disk", lambda path="/": OK_DISK)
@mock.patch("server_stats.cpu_percent", lambda interval=0.5: 3.0)
@mock.patch("server_stats.load_avg", lambda: (0.21, 0.11, 0.08))
@mock.patch("server_stats.uptime_str", lambda: "3d 5h")
class Info(unittest.TestCase):
    def test_info_has_what_left_the_board(self):
        b = board()
        b.total = 656
        b.edge, b.hold, b.last_429 = 86.0, 12, time.time()
        text = b.info()
        for part in ("eu-test-1", "2 × A1.Flex · 1 OCPU · 6 GB each", "⬜️ arm  ⬜️ arm-2",
                     "🗓 Hunting <b>", "Σ <b>656</b> tries", "range 15–300 s",
                     "OCI limit ≈ 86 s · last 429 <tg-time", "holding 12 more tries",
                     "CPU 3% · load 0.21 0.11 0.08", "swap 174 MB", "up 3d 5h", "🔖 Code "):
            self.assertIn(part, text)
        self.assertTrue(balanced(text))

    def test_info_before_any_429(self):
        self.assertIn("OCI limit not hit yet", board().info())


class DaySummary(unittest.TestCase):
    def test_summary_after_midnight(self):
        b = board()
        b.day = "2026-11-08"
        b.today.update(tries=640, capacity=620, throttled=15, network=5)
        b.edge = 86.0
        b.roll_day_if_new()
        text = _setup.sent[-1][1]["text"]
        self.assertEqual(text.splitlines(), [
            "📊 <b>Day 08.11</b> · <b>640</b> tries",
            "🚦 Throttled (429) <b>15</b> · 2%",
            "⚠️ Network <b>5</b> · see /log",
            "🎚 OCI limit ≈ 86 s",
        ])
        self.assertEqual(b.today["tries"], 0)

    def test_clean_day(self):
        b = board()
        b.day = "2026-11-08"
        b.today.update(tries=10, capacity=10)
        b.roll_day_if_new()
        self.assertIn("✅ No errors", _setup.sent[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
