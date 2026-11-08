"""status_board.py — one pinned Telegram message, edited in place after every attempt.

Replaces the hourly "Still hunting..." messages: the chat stays quiet, the pinned
message always shows the live picture (today / total / last try / server / journal).
Counters live in hunter_state.json so a restart doesn't lose them.
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
JOURNAL_SIZE = 6
TZ = config.TZ  # same as the log; DST switches by itself
REFUSALS_BEFORE_RESEND = 3  # Telegram refusing edits this many times in a row → send a new board


def _now() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _zero_day() -> dict:
    # throttled = OCI 429 only; network = connection blips and OCI 5xx
    return {"tries": 0, "capacity": 0, "throttled": 0, "network": 0, "errors": 0}


def _breakdown(t: dict, summary: bool = False) -> str:
    """'303 no capacity · 9 throttled · 0 errors' — one wording for the board and the day summary."""
    parts = [f"{_num(t['capacity'])} no capacity"]
    if t["throttled"]:
        parts.append(f"{_num(t['throttled'])} throttled" + (" (429)" if summary else ""))
    if t["network"]:
        parts.append(f"{_num(t['network'])} network")
    if t["errors"] or not summary:
        parts.append(f"{_num(t['errors'])} error{'' if t['errors'] == 1 else 's'}")
    return " · ".join(parts)


class Board:
    def __init__(self, slots: list, ads_label: str):
        self.slots = slots
        self.ads_label = ads_label
        self.have: set = set()
        self.phase = "hunting"  # hunting | done | stopped
        self.last_try: datetime.datetime | None = None
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
        self.journal = [list(j[:2]) + [j[2] if len(j) > 2 else 1] for j in s.get("journal", [])]
        # current self-tuning pause (main._next_delay), kept across restarts
        self.pause = float(s.get("pause") or config.PAUSE_START_S)
        self.pause = min(max(self.pause, config.PAUSE_MIN_S), config.PAUSE_MAX_S)

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

    def _save(self) -> None:
        state = {"msg_id": self.msg_id, "day": self.day, "today": self.today, "total": self.total,
                 "total_since": self.total_since, "journal": self.journal, "pause": self.pause}
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
            # Same event again (e.g. a 429 every hour): bump its counter instead of
            # pushing everything else out of the journal.
            last = self.journal[-1] if self.journal else None
            if last and last[1] == text:
                last[0] = time.time()
                last[2] += 1
            else:
                self.journal = (self.journal + [[time.time(), text, 1]])[-JOURNAL_SIZE:]
            self._save()

    def attempt(self, outcome: str, delay: int | None = None) -> None:
        """Count one attempt. outcome: capacity | throttled | network | errors | won."""
        self.roll_day_if_new()
        with self._lock:
            self.today["tries"] += 1
            if outcome in self.today:
                self.today[outcome] += 1
            self.total += 1
            self.last_try = _now()
            self.next_try = self.last_try + datetime.timedelta(seconds=delay) if delay else None
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
        verdict = "⚠️ there were errors — see /logs" if t["errors"] else "✅ no errors"
        notifier.send_message(
            f"📊 <b>Day {d}</b> · {config.OCI_REGION}\n"
            f"{_num(t['tries'])} tries: {_breakdown(t, summary=True)}\n"
            f"{verdict}\n"
            f"<tg-spoiler>Σ {_num(self.total)} tries since {self._since()}</tg-spoiler>",
            silent=True)

    # ---- rendering -------------------------------------------------------------
    def _since(self) -> str:
        return datetime.date.fromisoformat(self.total_since).strftime("%d.%m.%Y")

    def render(self) -> str:
        now = _now()
        target = len(self.slots)
        won = len(self.have & set(self.slots))
        size = f"{config.OCPUS:g} OCPU · {config.MEMORY_GB:g} GB"
        head = {"hunting": f"🟡 Hunting <b>{won}/{target}</b>",
                "done": f"🟢 Done <b>{won}/{target}</b>",
                "stopped": f"⚪️ Stopped <b>{won}/{target}</b>"}[self.phase]
        slots = "  ".join(("✅ " if n in self.have else "⬜️ ") + n for n in self.slots)

        t = self.today

        lines = [
            f"🎯 <b>OCI ARM Hunter</b> · {config.OCI_REGION}",
            f"{head} · {size}",
            slots,
            "",
            f"📅 Today <b>{_num(t['tries'])}</b> tries",
            f"      {_breakdown(t)}",
        ]
        if self.phase == "hunting" and self.last_try:
            nxt = f" · next ≈ {self.next_try:%H:%M}" if self.next_try else ""
            lines.append(f"⏱ Last {self.last_try:%H:%M}{nxt} · {self.ads_label}")
        if self.phase == "hunting":
            share = f" · 429 today {100 * t['throttled'] / t['tries']:.0f}%" if t["tries"] else ""
            lines.append(f"🎚 Pause {self.pause:.0f} s (auto){share}")

        try:
            mem = server_stats.memory()
            dsk = server_stats.disk("/")
            srv = f"🖥 RAM {mem['percent']:.0f}%"
            if mem["swap_total_mb"]:
                srv += f" · swap {mem['swap_used_mb']} MB"
            srv += f" · disk {dsk['percent']:.0f}% · up {server_stats.uptime_str()}"
            lines.append(srv)
        except Exception:
            pass

        if self.journal:
            lines += ["", "📝 <b>Journal</b>"]
            for ts, text, count in self.journal:
                when = datetime.datetime.fromtimestamp(ts, TZ)
                stamp = f"{when:%H:%M}" if when.date() == now.date() else f"{when:%d.%m %H:%M}"
                times = f" ×{count}" if count > 1 else ""
                lines.append(f"<code>{stamp}</code> {text}{times}")
        hidden = [f"Σ Total {_num(self.total)} tries since {self._since()}"]
        if config.HUNT_SINCE:
            try:
                since = datetime.date.fromisoformat(config.HUNT_SINCE)
                hidden.append(f"🗓 Hunting {(now.date() - since).days} days (since {since:%d.%m.%Y})")
            except ValueError:
                pass
        lines += ["", f"<tg-spoiler>{chr(10).join(hidden)}</tg-spoiler>",
                  f"<i>updated {now:%H:%M:%S} ({now:%Z})</i>"]
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
