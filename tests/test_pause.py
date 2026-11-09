"""Self-tuning pause with a memory of OCI's 429 edge (main._next_delay)."""

import random
import unittest

import _setup
import main
import status_board


def board(pause=100.0, edge=None, hold=0):
    _setup.fresh_state()
    b = status_board.Board(["arm", "arm-2"], "AD-1")
    b.pause, b.edge, b.hold = pause, edge, hold
    return b


class PauseRules(unittest.TestCase):
    def test_first_429_remembers_edge_steps_5pct_up_and_holds(self):
        b = board(pause=86)
        main._next_delay(b, "429")
        self.assertEqual(b.edge, 86)
        self.assertAlmostEqual(b.pause, 86 * 1.05)
        self.assertEqual(b.hold, main.PAUSE_HOLD_TRIES)

    def test_hold_then_creep_down_half_a_second(self):
        b = board(pause=86)
        main._next_delay(b, "429")
        held = b.pause
        for _ in range(main.PAUSE_HOLD_TRIES):
            main._next_delay(b, "ok")
        self.assertAlmostEqual(b.pause, held)
        main._next_delay(b, "ok")
        self.assertAlmostEqual(b.pause, held - 0.5)

    def test_far_above_edge_or_no_edge_goes_3s(self):
        b = board(pause=200, edge=100)
        main._next_delay(b, "ok")
        self.assertEqual(b.pause, 197)
        b = board(pause=60)
        main._next_delay(b, "ok")
        self.assertEqual(b.pause, 57)

    def test_second_429_in_a_row_backs_off_hard(self):
        b = board(pause=86)
        main._next_delay(b, "429")
        p = b.pause
        main._next_delay(b, "429")
        self.assertAlmostEqual(b.pause, p * 1.5)
        self.assertEqual(b.edge, p)

    def test_clean_answer_resets_the_in_a_row(self):
        b = board(pause=86)
        main._next_delay(b, "429")
        main._next_delay(b, "ok")
        p = b.pause
        main._next_delay(b, "429")
        self.assertAlmostEqual(b.pause, p * 1.05)

    def test_limit_moved_down_speeds_up_after_5_below_edge(self):
        b = board(pause=84, edge=86)  # already below the edge, no hold
        for _ in range(main.PAUSE_EDGE_STALE - 1):
            main._next_delay(b, "ok")
        self.assertAlmostEqual(b.pause, 84 - 0.5 * (main.PAUSE_EDGE_STALE - 1))
        p = b.pause
        main._next_delay(b, "ok")
        self.assertAlmostEqual(b.pause, p - 3)

    def test_bounds(self):
        b = board(pause=16)
        main._next_delay(b, "ok")
        self.assertEqual(b.pause, 15)
        b = board(pause=250)
        main._next_delay(b, "429")
        main._next_delay(b, "429")
        self.assertEqual(b.pause, 300)

    def test_network_keeps_and_real_error_slows_without_touching_edge(self):
        b = board(pause=90, edge=86, hold=3)
        main._next_delay(b, "keep")
        self.assertEqual((b.pause, b.edge, b.hold), (90, 86, 3))
        main._next_delay(b, "slow")
        self.assertEqual((b.pause, b.edge), (135, 86))

    def test_delay_has_0_to_5s_jitter(self):
        b = board(pause=100, edge=100, hold=5)
        delays = {main._next_delay(b, "ok") for _ in range(5)}
        self.assertTrue(delays <= set(range(100, 106)))

    def test_edge_and_hold_survive_a_restart(self):
        b = board(pause=86)
        main._next_delay(b, "429")
        again = status_board.Board(["arm", "arm-2"], "AD-1")
        self.assertEqual(again.edge, 86)
        self.assertEqual(again.hold, main.PAUSE_HOLD_TRIES)
        self.assertAlmostEqual(again.pause, b.pause)


class AgainstModeledOCI(unittest.TestCase):
    """OCI modeled as a turnstile letting one call through per E seconds. This
    model reproduced the real Zurich night (old rule: ~32 accepted/h, ~7% 429)."""

    def run_model(self, E=85.0, hours=48):
        b = board(pause=60)
        random.seed(1)
        t, tokens, last, ok, n429 = 0.0, 1.0, 0.0, 0, 0
        while t < hours * 3600:
            tokens = min(1.0, tokens + (t - last) / E)
            last = t
            if tokens >= 1.0:
                tokens -= 1.0
                ok += 1
                t += main._next_delay(b, "ok")
            else:
                n429 += 1
                t += main._next_delay(b, "429")
        return ok / hours, n429 / (ok + n429)

    def test_close_to_the_ceiling_with_few_429s(self):
        per_hour, share_429 = self.run_model()
        self.assertGreater(per_hour, 37)            # old rule: ~32; ceiling 3600/85 ≈ 42.4
        self.assertLess(share_429, 0.06)


if __name__ == "__main__":
    unittest.main()
