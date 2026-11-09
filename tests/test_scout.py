"""Scout (scout.py) and how the hunting loop reacts to it (main.run)."""

import threading
import time
import unittest
from unittest import mock

import oci.exceptions

import _setup
import main
import scout
import status_board

AD1, AD2 = "x:EU-TEST-1-AD-1", "x:EU-TEST-1-AD-2"


def err(status, code="X"):
    return oci.exceptions.ServiceError(status, code, {}, "Out of host capacity." if status == 500 else code)


class ScoutUnit(unittest.TestCase):
    def make(self, answers, ads=(AD1, AD2)):
        it = iter(answers)

        def check(ad):
            a = next(it)
            if isinstance(a, Exception):
                raise a
            return a
        return scout.Scout(list(ads), 2, check=check)

    def test_yes_wakes_and_take_returns_that_ad(self):
        s = self.make(["OUT_OF_HOST_CAPACITY", "AVAILABLE"])
        s.step()
        self.assertFalse(s.wake.is_set())
        self.assertIsNone(s.take())
        s.step()
        self.assertTrue(s.wake.is_set())
        self.assertEqual(s.take(), AD2)

    def test_no_after_yes_clears_it(self):
        s = self.make(["AVAILABLE", "OUT_OF_HOST_CAPACITY"], ads=(AD1,))
        s.step()
        s.step()
        self.assertIsNone(s.take())

    def test_old_yes_goes_stale(self):
        s = self.make(["AVAILABLE"], ads=(AD1,))
        s.step()
        with mock.patch("time.time", lambda: s._last_yes[AD1] + s.fresh_s() + 1):
            self.assertIsNone(s.take())

    def test_mute_blocks_wake_and_take(self):
        s = self.make(["AVAILABLE"], ads=(AD1,))
        s.mute(60)
        s.step()
        self.assertFalse(s.wake.is_set())
        self.assertIsNone(s.take())

    def test_false_alarm_ignores_only_that_ad(self):
        s = self.make(["AVAILABLE", "AVAILABLE"])
        s.step()
        s.false_alarm(AD1)
        self.assertIsNone(s.take())
        s.wake.clear()
        s.step()  # AD-2 says yes: still watched
        self.assertTrue(s.wake.is_set())
        self.assertEqual(s.take(), AD2)

    def test_false_alarmed_ad_ignored_until_report_says_no(self):
        seen = []
        s = self.make(["AVAILABLE", "AVAILABLE", "OUT_OF_HOST_CAPACITY", "AVAILABLE"], ads=(AD1,))
        s.on_seen = lambda: seen.append(1)
        s.step()
        s.false_alarm(AD1)
        s.wake.clear()
        s.step()  # still "yes": the same wrong window, no new shot
        self.assertFalse(s.wake.is_set())
        self.assertIsNone(s.take())
        self.assertEqual(len(seen), 1)
        s.step()  # "no" ends it
        s.step()  # a new "yes" counts again
        self.assertTrue(s.wake.is_set())
        self.assertEqual(s.take(), AD1)
        self.assertEqual(len(seen), 2)

    def test_health_shows_a_failing_report(self):
        s = self.make([err(404, "NotAuthorizedOrNotFound"), "OUT_OF_HOST_CAPACITY"], ads=(AD1,))
        self.assertEqual(s.health(), "")
        s.step()
        self.assertIn("NotAuthorizedOrNotFound", s.health())
        s.step()
        self.assertEqual(s.health(), "")

    def test_health_shows_a_slowed_scout(self):
        s = self.make([err(429)], ads=(AD1,))
        s.step()
        self.assertIn("every 4 s", s.health())

    def test_report_429_doubles_interval_then_recovers(self):
        s = self.make([err(429)] + ["OUT_OF_HOST_CAPACITY"] * scout.CLEAN_TO_SPEED_UP, ads=(AD1,))
        s.step()
        self.assertEqual(s.interval, 4)
        for _ in range(scout.CLEAN_TO_SPEED_UP):
            s.step()
        self.assertEqual(s.interval, 2)

    def test_errors_wait_longer_and_are_not_counted(self):
        calls = []
        s = self.make([ConnectionError("reset")], ads=(AD1,))
        s.on_check = lambda: calls.append(1)
        s.step()
        self.assertEqual(s._next_wait, scout.ERROR_WAIT_S)
        self.assertEqual(calls, [])

    def test_seen_counted_once_per_window(self):
        seen = []
        s = self.make(["AVAILABLE", "AVAILABLE", "OUT_OF_HOST_CAPACITY", "AVAILABLE"], ads=(AD1,))
        s.on_seen = lambda: seen.append(1)
        for _ in range(4):
            s.step()
        self.assertEqual(len(seen), 2)


@mock.patch("main._bot_listener", lambda: None)
@mock.patch("server_stats.memory", lambda: {"percent": 40.0, "swap_total_mb": 0, "swap_used_mb": 0,
                                            "used_mb": 400, "total_mb": 1000})
@mock.patch("server_stats.disk", lambda path="/": {"percent": 20.0, "used_gb": 9.0, "total_gb": 45.0})
class LoopWithScout(unittest.TestCase):
    """main.run() with OCI faked: target 2 slots, normal pause 60 s (so within the test
    only scout shots can happen), the scout checking every 0.05 s."""

    def run_hunter(self, launch, status="AVAILABLE", timeout=5):
        _setup.fresh_state()
        main._stop_event.clear()
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("r", main.run()), daemon=True)
        with mock.patch.object(main.config, "SCOUT_INTERVAL_S", 0.05), \
                mock.patch.object(scout, "RETRY_NET_S", 0.2), \
                mock.patch("oci_client.running_instance_names", lambda: set()), \
                mock.patch("oci_client.capacity_status", lambda ad: status), \
                mock.patch("oci_client.launch_instance", launch):
            t.start()
            t.join(timeout)
            main._stop_event.set()
            t.join(5)
        main._stop_event.clear()
        return main._board

    def test_scout_yes_launches_without_waiting_the_pause(self):
        calls = []

        def launch(name, ad):
            calls.append((name, ad, time.time()))
            return {"name": name, "public_ip": "1.2.3.4", "region": "eu-test-1"}
        start = time.time()
        b = self.run_hunter(launch, timeout=2)
        self.assertEqual([c[0] for c in calls], ["arm"])  # the 2nd slot waits the pause
        self.assertLess(calls[0][2] - start, 1.5)  # not after the 60 s pause
        self.assertEqual(b.scout["won"], 1)
        self.assertEqual(b.pause, 60)  # scout shots don't teach the pause

    def test_429_on_scout_shot_waits_the_pause_and_keeps_the_edge(self):
        calls = []

        def launch(name, ad):
            calls.append(ad)
            raise err(429, "TooManyRequests")
        b = self.run_hunter(launch, timeout=1.5)
        self.assertEqual(len(calls), 1)  # no shot every few seconds into a closed limit
        self.assertIsNone(b.edge)
        self.assertEqual(b.pause, 60)
        self.assertEqual(b.today["throttled"], 1)
        self.assertGreater(b.next_try.timestamp(), time.time() + 50)  # the normal launch waits too

    def test_network_blip_on_shot_retries_soon(self):
        answers = [ConnectionError("reset")]

        def launch(name, ad):
            if answers:
                raise answers.pop(0)
            return {"name": name, "public_ip": "1.2.3.4", "region": "eu-test-1"}
        with mock.patch("main._is_transient", lambda e: True):
            b = self.run_hunter(launch, timeout=1.5)
        self.assertIn("arm", b.have)
        self.assertEqual(b.scout["won"], 1)

    def test_false_alarm_mutes_the_scout(self):
        calls = []

        def launch(name, ad):
            calls.append(ad)
            raise err(500, "InternalError")
        b = self.run_hunter(launch, timeout=1.5)
        self.assertEqual(len(calls), 1)  # muted 5 min after the false alarm
        self.assertEqual(b.scout["false"], 1)
        self.assertTrue(any("false alarm" in j[1] for j in b.journal))

    def test_real_error_on_shot_mutes_long_and_keeps_the_pause(self):
        calls = []

        def launch(name, ad):
            calls.append(ad)
            raise err(400, "LimitExceeded")
        b = self.run_hunter(launch, timeout=1.5)
        self.assertEqual(len(calls), 1)  # not again every 10 s
        self.assertEqual(b.pause, 60)    # a shot never changes the pause


if __name__ == "__main__":
    unittest.main()
