"""status_board.py — one pinned Telegram message, edited in place after every attempt.

The chat stays quiet; the pinned message shows only what matters right now (next
try, today's counts, 429s, problems, journal). Totals, settings and server details
live in /info. Counters live in hunter_state.json so a restart doesn't lose them.
Times are <tg-time> entities: Telegram shows them relative ("in 2 minutes") in the
viewer's own time zone and keeps them ticking; the HH:MM fallback is what a copy,
a notification or an old client shows.
"""

import datetime
import json
import logging
import os
import threading
import time

import config
import notifier
import server_stats

logger = logging.getLogger(__name__)

STATE_FILE = "hunter_state.json"
JOURNAL_SIZE = 8  # newest first, in a collapsed quote
TZ = config.TZ  # same as the log; DST switches by itself
REFUSALS_BEFORE_RESEND = 3  # Telegram refusing edits this many times in a row → send a new board
RESTART_MERGE_S = 300  # "stopped" then "started" within this → one "restarted" line
OUTAGE_PREFIX = "📡 OCI unreachable since "
HIGH_PCT = 90  # RAM/disk at or above this (or swap half full) → server line on the board


def _now() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def tg_time(when: datetime.datetime, fmt: str = "r") -> str:
    """Telegram date/time entity (Bot API 9.5). fmt "r" = relative, "t" = HH:MM,
    "dt" = date + time. The fallback text must never be empty: an empty entity shows
    nothing at all."""
    fallback = f"{when:%d.%m %H:%M}" if fmt == "dt" else f"{when:%H:%M}"
    return f'<tg-time unix="{int(when.timestamp())}" format="{fmt}">{fallback}</tg-time>'


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "0%"


def _problems(t: dict) -> str:
    """'Errors 1 · network 2' — empty when the day is clean."""
    parts = []
    if t["errors"]:
        parts.append(f"errors <b>{_num(t['errors'])}</b>")
    if t["network"]:
        parts.append(f"network <b>{_num(t['network'])}</b>")
    text = " · ".join(parts)
    return text[:1].upper() + text[1:]


def _size(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def _server_alert() -> str:
    """Server line, only when something is nearly full (Zurich once hung on RAM)."""
    try:
        mem = server_stats.memory()
        dsk = server_stats.disk("/")
    except Exception:
        return ""
    swap_high = mem["swap_total_mb"] and mem["swap_used_mb"] >= mem["swap_total_mb"] / 2
    if mem["percent"] < HIGH_PCT and dsk["percent"] < HIGH_PCT and not swap_high:
        return ""
    bold = lambda text, high: f"<b>{text}</b>" if high else text
    parts = [bold(f"RAM {mem['percent']:.0f}%", mem["percent"] >= HIGH_PCT)]
    if mem["swap_total_mb"]:
        parts.append(bold(f"swap {_size(mem['swap_used_mb'])}", swap_high))
    parts.append(bold(f"disk {dsk['percent']:.0f}%", dsk["percent"] >= HIGH_PCT))
    return "🖥 " + " · ".join(parts)


def _zero_day() -> dict:
    # throttled = OCI 429 only; network = connection blips and OCI 5xx
    return {"tries": 0, "capacity": 0, "throttled": 0, "network": 0, "errors": 0}


class Board:
    def __init__(self, slots: list, ads_label: str):
        self.slots = slots
        self.ads_label = ads_label
        self.have: set = set()
        self.phase = "hunting"  # hunting | done | stopped
        self.next_try: datetime.datetime | None = None
        self._lock = threading.Lock()       # state (counters, journal, file)
        self._tg_lock = threading.Lock()    # one Telegram refresh at a time
        self._dirty = threading.Event()
        self._worker = False
        self._refusals = 0

        s = self._load()
        self.msg_id = s.get("msg_id")
        self.day = s.get("day") or _now().date().isoformat()
        self.today = {**_zero_day(), **s.get("today", {})}
        self.total = s.get("total", 0)
        self.total_since = s.get("total_since") or self.day
        # [[unix_ts, text, count], ...] newest last; entries without a count get 1
        # (429 lines from older versions are dropped: 429s have their own line now)
        self.journal = [list(j[:2]) + [j[2] if len(j) > 2 else 1] for j in s.get("journal", [])
                        if not str(j[1]).startswith("⏳")]
        self.last_429: float | None = s.get("last_429")  # unix time of the last 429
        # self-tuning pause (main._next_delay), kept across restarts: current pause,
        # edge = the pause that last got a 429, hold = clean answers left to wait there
        self.pause = float(s.get("pause") or config.PAUSE_START_S)
        self.pause = min(max(self.pause, config.PAUSE_MIN_S), config.PAUSE_MAX_S)
        self.edge: float | None = s.get("edge")
        self.hold = int(s.get("hold") or 0)
        self.below_edge = 0   # clean answers in a row below the edge
        self.prev_429 = False

    # ---- state -----------------------------------------------------------------
    @staticmethod
    def _load() -> dict:
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("State file unreadable, starting fresh: %s", e)
            return {}

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        state = {"msg_id": self.msg_id, "day": self.day, "today": self.today, "total": self.total,
                 "total_since": self.total_since, "journal": self.journal, "pause": self.pause,
                 "edge": self.edge, "hold": self.hold, "last_429": self.last_429}
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.warning("State save failed: %s", e)

    # ---- events ----------------------------------------------------------------
    def event(self, text: str) -> None:
        """Add a line to the journal shown at the bottom of the pinned message."""
        with self._lock:
            now = time.time()
            last = self.journal[-1] if self.journal else None
            if (text == "▶️ started" and last and last[1] == "⏹ stopped"
                    and now - last[0] < RESTART_MERGE_S):
                text = "🔄 restarted"  # a deploy: one line instead of two
                self.journal.pop()
                last = self.journal[-1] if self.journal else None
            # Same event again: bump its counter instead of pushing everything
            # else out of the journal.
            if last and last[1] == text:
                last[0] = now
                last[2] += 1
            else:
                self.journal = (self.journal + [[now, text, 1]])[-JOURNAL_SIZE:]
            self._save()

    def outage_over(self) -> None:
        """Turn the open "OCI unreachable since 09:31" line into "09:31–09:40"."""
        with self._lock:
            for entry in reversed(self.journal):
                if entry[1].startswith(OUTAGE_PREFIX):
                    entry[1] = f"📡 OCI unreachable {entry[1][len(OUTAGE_PREFIX):]}–{_now():%H:%M}"
                    self._save()
                    break

    def attempt(self, outcome: str, delay: int | None = None) -> None:
        """Count one attempt. outcome: capacity | throttled | network | errors | won."""
        self.roll_day_if_new()
        with self._lock:
            self.today["tries"] += 1
            if outcome in self.today:
                self.today[outcome] += 1
            self.total += 1
            if outcome == "throttled":
                self.last_429 = time.time()
            self.next_try = _now() + datetime.timedelta(seconds=delay) if delay else None
            self._save()
        self.poke()

    def start_worker(self) -> None:
        """Refresh Telegram from a background thread, so a slow Telegram never
        stretches the pause between attempts. Pokes coalesce: many → one edit."""
        def loop() -> None:
            while True:
                self._dirty.wait()
                self._dirty.clear()
                self.refresh()
        self._worker = True
        threading.Thread(target=loop, daemon=True, name="status-board").start()

    def poke(self) -> None:
        """Ask for a refresh: in the background once the worker runs, else right now."""
        if self._worker:
            self._dirty.set()
        else:
            self.refresh()

    def roll_day_if_new(self) -> None:
        """After local midnight (also across a restart): send the finished day's summary
        and start a new day."""
        with self._lock:
            if _now().date().isoformat() == self.day:
                return
            day, t = self.day, self.today
            self.day = _now().date().isoformat()
            self.today = _zero_day()
            self._save()
        if not t["tries"]:
            return
        d = datetime.date.fromisoformat(day).strftime("%d.%m")
        lines = [f"📊 <b>Day {d}</b> · <b>{_num(t['tries'])}</b> tries"]
        if t["throttled"]:
            lines.append(f"🚦 Throttled (429) <b>{_num(t['throttled'])}</b> · {_pct(t['throttled'], t['tries'])}")
        problems = _problems(t)
        lines.append(f"⚠️ {problems} · see /log" if problems else "✅ No errors")
        if self.edge:
            lines.append(f"🎚 OCI limit ≈ {self.edge:.0f} s")
        notifier.send_message("\n".join(lines), silent=True)

    # ---- rendering -------------------------------------------------------------
    def _since(self) -> str:
        return datetime.date.fromisoformat(self.total_since).strftime("%d.%m.%Y")

    def render(self) -> str:
        now = _now()
        target = len(self.slots)
        won = len(self.have & set(self.slots))
        head = {"hunting": "🟡 <b>Hunting</b>", "done": "🟢 <b>Done</b>",
                "stopped": "⚪️ <b>Stopped</b>"}[self.phase]
        boxes = "".join("✅" if n in self.have else "⬜️" for n in self.slots)
        t = self.today

        lines = [f"{head} · {boxes} <b>{won}/{target}</b>", ""]
        if self.phase == "hunting" and self.next_try:
            # "3 minutes ago" here while hunting = the hunter is stuck
            lines.append(f"⏭ Next {tg_time(self.next_try)} · {self.ads_label} · pause <b>{self.pause:.0f} s</b>")
        lines.append(f"📊 Today <b>{_num(t['tries'])}</b> tries")
        if t["throttled"]:
            last = ""
            if self.last_429:
                last = f" · last {tg_time(datetime.datetime.fromtimestamp(self.last_429, TZ))}"
            lines.append(f"🚦 Throttled (429) <b>{_num(t['throttled'])}</b> · {_pct(t['throttled'], t['tries'])}{last}")
        problems = _problems(t)
        if problems:
            lines.append(f"⚠️ {problems}")
        server = _server_alert()
        if server:
            lines.append(server)

        if self.journal:
            rows = []
            for ts, text, count in reversed(self.journal):
                when = datetime.datetime.fromtimestamp(ts, TZ)
                times = f" ×{count}" if count > 1 else ""
                rows.append(f"{tg_time(when, 't' if when.date() == now.date() else 'dt')} {notifier.esc(text)}{times}")
            lines += ["", "📝 <b>Journal</b>", "<blockquote expandable>" + "\n".join(rows) + "</blockquote>"]
        lines.append(f"<i>updated {tg_time(now)}</i>")
        return "\n".join(lines)

    # ---- telegram --------------------------------------------------------------
    def refresh(self) -> None:
        """Edit the pinned message; if it's gone, send a new one and pin it. Never raises."""
        with self._tg_lock:
            self._refresh()

    def _refresh(self) -> None:
        try:
            text = self.render()
            if self.msg_id:
                result = notifier.edit_message(self.msg_id, text)
                if result == "refused":
                    self._refusals += 1
                if result in ("ok", "failed") or (result == "refused" and self._refusals < REFUSALS_BEFORE_RESEND):
                    if result == "ok":
                        self._refusals = 0
                    return  # done, or a blip — next attempt tries again
                logger.info("Status message %s, sending a new one", "gone" if result == "gone" else "keeps being refused")
            old = self.msg_id
            new = notifier.send_message(text, silent=True)
            if not new:
                return
            if old:
                notifier.unpin_message(old)
            self.msg_id = new
            self._refusals = 0
            with self._lock:
                self._save()
            notifier.pin_message(new)
        except Exception as e:
            logger.warning("Status board refresh failed: %s", e)
