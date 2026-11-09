"""Telegram commands (/info, /log), the bot menu, chat filter and log archive (main.py)."""

import datetime
import html
import os
import re
import unittest
from unittest import mock

import _setup
import main
import status_board


class ParseDay(unittest.TestCase):
    def test_formats(self):
        today = main._local_now().date()
        self.assertEqual(main._parse_day("2026-11-07"), datetime.date(2026, 11, 7))
        self.assertEqual(main._parse_day("07.11.2026"), datetime.date(2026, 11, 7))
        d = main._parse_day(f"{today:%d.%m}")
        self.assertEqual(d, today)
        self.assertIsNone(main._parse_day("list"))
        self.assertIsNone(main._parse_day("31.02"))

    def test_day_month_is_never_in_the_future(self):
        tomorrow = main._local_now().date() + datetime.timedelta(days=1)
        d = main._parse_day(f"{tomorrow:%d.%m}")
        self.assertEqual(d.year, tomorrow.year - 1)


class Commands(unittest.TestCase):
    def setUp(self):
        _setup.fresh_state()
        main._board = status_board.Board(["arm", "arm-2"], "AD-1")

    def test_routing(self):
        with mock.patch.object(main, "_send_log") as log:
            main._handle_command("/log")
            main._handle_command("/log@OracleInstanceHunterBot list")
            main._handle_command("/LOG 07.11")
        self.assertEqual([c.args[0] for c in log.call_args_list], ["", "list", "07.11"])
        with mock.patch.object(main._board, "info", return_value="INFO"):
            main._handle_command("/info")
        self.assertEqual(_setup.sent[-1][1]["text"], "INFO")

    def test_old_and_unknown_commands_get_a_hint(self):
        for cmd in ("/status", "/logs", "/start"):
            main._handle_command(cmd)
            self.assertIn("/info · /log", _setup.sent[-1][1]["text"])

    def test_log_sends_todays_file_with_last_lines_as_caption(self):
        with open("hunter.log", "w") as f:
            f.writelines(f"{main._local_now():%Y-%m-%d %H:%M} [INFO] line {i} <x>\n" for i in range(200))
        main._send_log("")
        method, payload = _setup.sent[-1]
        self.assertEqual(method, "sendDocument")
        self.assertEqual(payload["parse_mode"], "HTML")
        caption = payload["caption"]
        visible = html.unescape(re.sub(r"<[^>]+>", "", caption))  # Telegram counts parsed text
        self.assertLessEqual(len(visible), 1024)
        self.assertIn("line 199 &lt;x&gt;", caption)
        self.assertNotIn("line 0 ", caption)

    def test_log_for_a_missing_day(self):
        main._send_log("01.01.2020")
        self.assertIn("No log for 01.01.2020", _setup.sent[-1][1]["text"])

    def test_listener_sets_menu_and_ignores_other_chats(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/info"}},
                   {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/info"}}]

        def fake_get(*args, **kwargs):
            main._stop_event.set()  # one round only
            return mock.Mock(json=lambda: {"result": updates})

        main._stop_event.clear()
        with mock.patch("requests.get", fake_get), \
                mock.patch.object(main, "_handle_command") as handle:
            main._bot_listener()
        main._stop_event.clear()
        method, payload = _setup.sent[0]
        self.assertEqual(method, "setMyCommands")
        self.assertEqual([c["command"] for c in payload["commands"]], ["info", "log"])
        self.assertEqual(handle.call_count, 1)


class LogRotation(unittest.TestCase):
    def test_old_lines_move_to_the_archive(self):
        today = main._local_now().date()
        old = today - datetime.timedelta(days=1)
        with open("hunter.log", "w") as f:
            f.write(f"{old} 23:59 [INFO] yesterday\nTraceback line\n{today} 00:01 [INFO] today\n")
        main._rotate_log()
        with open("hunter.log") as f:  # (+ the hunter's own "Log rotated" line)
            self.assertTrue(f.read().startswith(f"{today} 00:01 [INFO] today\n"))
        with open(os.path.join(main.LOG_DIR, f"hunter_{old}.log")) as f:
            self.assertEqual(f.read(), f"{old} 23:59 [INFO] yesterday\nTraceback line\n")


if __name__ == "__main__":
    unittest.main()
