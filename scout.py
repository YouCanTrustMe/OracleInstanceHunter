"""scout.py — asks OCI "is there A1 capacity?" every few seconds and wakes the hunter
the moment the answer is yes.

The compute capacity report has its own rate limit, far higher than LaunchInstance's
(measured 2026-11-09: no 429 at 1 call/s for 3 min or every 2 s for 20 min, and the
Frankfurt hunter's launch 429s did not go up meanwhile). So the scout can look ~40×
as often as the launch rhythm allows. It only adds shots: the hunter's normal
launches go on unchanged, in case the report turns out to be late or wrong.
"""

import logging
import threading
import time

import oci.exceptions

import config
import oci_client

logger = logging.getLogger(__name__)

RETRY_NET_S = 10          # a scout shot hit a network blip / 5xx: again in 10 s if still available
ERROR_MUTE_S = 300        # a scout shot got a real error (it would only repeat): ignore the scout 5 min
MAX_INTERVAL_S = 60       # the report itself got a 429: interval doubles, up to this
CLEAN_TO_SPEED_UP = 100   # this many clean answers after slowing down → halve the interval again
ERROR_WAIT_S = 30         # network / other error from the report: wait a bit longer


def _short(ad: str) -> str:
    return "AD-" + ad.rsplit("-", 1)[-1] if "-AD-" in ad.upper() else ad


class Scout:
    def __init__(self, ads: list, interval: float, check=None, on_check=None, on_seen=None):
        self.ads = ads
        self.base = self.interval = interval
        self.check = check or oci_client.capacity_status
        self.on_check = on_check or (lambda: None)   # counts checks on the board
        self.on_seen = on_seen or (lambda: None)     # capacity newly seen in an AD
        self.wake = threading.Event()                # set on "yes" when not muted
        self._lock = threading.Lock()
        self._seen = {}       # ad → when the current "yes" window started
        self._last_yes = {}   # ad → time of the latest "yes"
        self._muted_until = 0.0
        self._turn = 0
        self._clean = 0
        self._errors = 0
        self._odd = set()     # statuses other than yes/no, logged once each
        self._false = set()   # ADs whose "yes" a launch just called false: ignored until "no"
        self._last_error = ""
        self._next_wait = interval

    def fresh_s(self) -> float:
        """A "yes" counts while no newer answer for that AD could have come in."""
        return self.interval * len(self.ads) + 5

    def take(self) -> str | None:
        """The AD with the freshest "yes", or None (none, or muted)."""
        now = time.time()
        with self._lock:
            if now < self._muted_until:
                return None
            fresh = {ad: t for ad, t in self._last_yes.items()
                     if now - t <= self.fresh_s() and ad not in self._false}
            return max(fresh, key=fresh.get) if fresh else None

    def mute(self, seconds: float) -> None:
        with self._lock:
            self._muted_until = max(self._muted_until, time.time() + seconds)

    def false_alarm(self, ad: str) -> None:
        """The launch in `ad` said "out of capacity" right after a "yes". If the report
        keeps saying yes there, it's wrong for us: ignore that AD until it says no
        (otherwise: a new "capacity seen", a shot, a false alarm, again and again).
        The other ADs stay watched."""
        with self._lock:
            self._last_yes.pop(ad, None)
            self._false.add(ad)

    def health(self) -> str:
        """"" while all is well; else what keeps failing or slowing the scout (for /info)."""
        if self._errors:
            return f"report failing ({self._errors} in a row): {self._last_error}"
        if self.interval > self.base:
            return f"report rate limited, checking every {self.interval:g} s for now"
        return ""

    def step(self) -> None:
        """One report call for the next AD in turn."""
        ad = self.ads[self._turn % len(self.ads)]
        self._turn += 1
        self._next_wait = self.interval
        try:
            status = self.check(ad)
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                self.interval = min(MAX_INTERVAL_S, self.interval * 2)
                self._next_wait = self.interval
                self._clean = 0
                logger.warning("Scout: capacity report rate limited, checking every %g s now", self.interval)
            else:
                self._error(f"{e.status} {e.code}")
            return
        except Exception as e:
            self._error(f"{type(e).__name__}: {e}")
            return
        self._errors = 0
        self.on_check()
        if self.interval > self.base:
            self._clean += 1
            if self._clean >= CLEAN_TO_SPEED_UP:
                self.interval = max(self.base, self.interval / 2)
                self._clean = 0
        if status not in ("AVAILABLE", "OUT_OF_HOST_CAPACITY") and status not in self._odd:
            self._odd.add(status)
            logger.info("Scout: report answered %s for %s", status, _short(ad))

        now = time.time()
        fire, new, started = False, False, None
        with self._lock:
            if status == "AVAILABLE":
                new = ad not in self._seen
                if new:
                    self._seen[ad] = now
                self._last_yes[ad] = now
                fire = now >= self._muted_until and ad not in self._false
            else:
                started = self._seen.pop(ad, None)
                self._last_yes.pop(ad, None)
                self._false.discard(ad)
        if status == "AVAILABLE":
            if new:
                logger.info("Scout: capacity AVAILABLE in %s", _short(ad))
                self.on_seen()
        elif started:
            logger.info("Scout: %s out of capacity again after %d s", _short(ad), now - started)
        if fire:
            self.wake.set()

    def _error(self, what: str) -> None:
        self._errors += 1
        self._last_error = what
        self._next_wait = max(self.interval, ERROR_WAIT_S)
        if self._errors in (1, 10) or self._errors % 100 == 0:
            logger.warning("Scout: capacity report failed (%d in a row): %s", self._errors, what)

    def run(self, stop: threading.Event) -> None:
        logger.info("Scout: checking A1 capacity every %g s (%s)", self.interval,
                    ", ".join(_short(ad) for ad in self.ads))
        while not stop.is_set():
            try:
                self.step()
            except Exception as e:  # never let the scout thread die
                self._error(f"{type(e).__name__}: {e}")
            stop.wait(self._next_wait)
