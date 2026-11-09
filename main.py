import time
import random
import logging
import threading
import signal
import datetime
import glob
import os
import re
import requests
import oci.exceptions

import config
import oci_client
import notifier
import server_stats
import status_board
import watchdog

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M")
_TZ = config.TZ  # e.g. Europe/Berlin: summer/winter time switches by itself
_fmt.converter = lambda ts: datetime.datetime.fromtimestamp(ts, _TZ).timetuple()

class _RedactSecrets(logging.Filter):
    """Mask the Telegram bot token (it appears inside request URLs in errors)."""
    def filter(self, record: logging.LogRecord) -> bool:
        token = config.TELEGRAM_BOT_TOKEN
        if token:
            msg = record.getMessage()
            if token in msg:
                record.msg, record.args = msg.replace(token, "***"), None
        return True


_fh = logging.FileHandler("hunter.log")
_fh.setFormatter(_fmt)
_fh.addFilter(_RedactSecrets())
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_sh.addFilter(_RedactSecrets())

logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_fh)
logging.getLogger().addHandler(_sh)

logger = logging.getLogger(__name__)

OUT_OF_CAPACITY_CODE = "InternalError"
OUT_OF_CAPACITY_MSG = "Out of host capacity"

_stop_event = threading.Event()
_board: status_board.Board | None = None

def _local_now() -> datetime.datetime:
    return datetime.datetime.now(_TZ)


LOG_DIR = "logs"
LOG_KEEP_DAYS = 30


_DATED = re.compile(r"\d{4}-\d{2}-\d{2} ")


def _rotate_log() -> None:
    """Move every line dated before today out of hunter.log into logs/hunter_<date>.log
    (appended; 30 days kept). Runs at start-up and after local midnight, so a restart
    across midnight still files each line under its own day. Holds the file handler's
    lock, so no thread can log between the read and the rewrite (nothing is lost)."""
    today = _local_now().date().isoformat()
    try:
        _fh.acquire()
        try:
            _fh.flush()
            with open("hunter.log") as f:
                lines = f.readlines()
            past, keep, day = {}, [], None
            for line in lines:
                if _DATED.match(line):
                    day = line[:10]
                if day and day < today:  # undated lines (tracebacks) follow the line above
                    past.setdefault(day, []).append(line)
                else:
                    keep.append(line)
            if past:
                os.makedirs(LOG_DIR, exist_ok=True)
                for d, day_lines in past.items():
                    with open(os.path.join(LOG_DIR, f"hunter_{d}.log"), "a") as f:
                        f.writelines(day_lines)
                with open("hunter.log", "w") as f:  # the handler appends (O_APPEND) after this
                    f.writelines(keep)
        finally:
            _fh.release()
        if past:
            for old in sorted(glob.glob(os.path.join(LOG_DIR, "hunter_*.log")))[:-LOG_KEEP_DAYS]:
                os.remove(old)
            logger.info("=== Log rotated (%s archived) ===", ", ".join(sorted(past)))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Log rotation failed: %s", e)


def _archived_days() -> list:
    return sorted(os.path.basename(p)[7:17] for p in glob.glob(os.path.join(LOG_DIR, "hunter_*.log")))


def _parse_day(arg: str) -> datetime.date | None:
    """'2026-11-07', '07.11.2026' or '07.11' (the latest such day) → date; None if not a date."""
    today = _local_now().date()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(arg, fmt).date()
        except ValueError:
            pass
    for year in (today.year, today.year - 1):  # parse with the year, so 29.02 works in leap years
        try:
            d = datetime.datetime.strptime(f"{arg}.{year}", "%d.%m.%Y").date()
        except ValueError:
            continue
        if d <= today:
            return d
    return None


def _handle_signal(signum, frame) -> None:
    logger.info("Signal received (%s), shutting down...", signum)
    _stop_event.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def is_out_of_capacity(error: oci.exceptions.ServiceError) -> bool:
    return OUT_OF_CAPACITY_MSG in str(error.message)


def _notify_success_safe(result: dict) -> None:
    """Notify success without ever raising — the instance is already created, so
    a Telegram failure must not prevent the loop from stopping."""
    try:
        notifier.notify_success(result["name"], result["public_ip"], result["region"])
    except Exception as e:
        logger.error("notify_success failed (instance IS created): %s", e)


def _slot_names() -> list:
    """Target instance names: base, then base-2, base-3 ... up to TARGET_INSTANCES."""
    base = config.INSTANCE_NAME
    return [base] + [f"{base}-{i}" for i in range(2, config.TARGET_INSTANCES + 1)]


def _have_slots(previous: set = None):
    """Which of our target slots are already filled by a running instance.
    If the listing fails, return `previous` unchanged — never forget a slot we
    already won (that would re-launch its name and create a duplicate)."""
    try:
        running = oci_client.running_instance_names()
    except Exception as e:
        logger.warning("Instance listing failed: %s", e)
        return previous
    return set(_slot_names()) & running


def _is_transient(e: Exception) -> bool:
    """Network-level failure (connection reset, SSL EOF, timeout) or a temporary
    OCI answer (429 rate limit, 5xx) — worth a quiet retry, not an alert."""
    if isinstance(e, oci.exceptions.ServiceError):
        return e.status == 429 or e.status >= 500
    return isinstance(e, (requests.exceptions.RequestException, oci.exceptions.RequestException,
                          oci.exceptions.ConnectTimeout, ConnectionError, TimeoutError))


TRANSIENT_ALERT_AFTER = 5  # consecutive temporary failures (~3-25 min, depends on the pause) before an alert


def _ad_short(ad: str) -> str:
    return ad.rsplit("-", 1)[-1] if "-AD-" in ad.upper() else ad


# Self-tuning pause with a memory of OCI's limit ("edge" = the pause that last got a 429).
# OCI lets roughly one launch call through per N seconds and turns the rest away, so the
# best place is just above the edge. Simulated on the real Zurich log: ~39 accepted
# checks/h vs ~32 with the old "-3 s / x1.5" rule (ceiling ~42), and fewer 429s.
PAUSE_FAST_STEP_S = 3     # clean answer far above the edge (or no edge known): 3 s shorter
PAUSE_NEAR_STEP_S = 0.5   # clean answer close to the edge: creep down slowly
PAUSE_NEAR_ZONE = 1.2     # "close" = less than 20% above the edge
PAUSE_BACKOFF = 1.05      # after a 429: 5% above the pause that got it ...
PAUSE_HOLD_TRIES = 20     # ... and stay there for 20 clean answers before creeping down
PAUSE_REPEAT_X = 1.5      # 429 twice in a row: OCI got stricter, back off hard
PAUSE_EDGE_STALE = 5      # 5 clean answers below the edge: the limit moved, go fast again


def _next_delay(board: status_board.Board, verdict: str) -> int:
    """Next pause in seconds (+0-5 s jitter). verdict: "ok" (OCI answered normally),
    "429" (rate limited), "slow" (a real error: back off so it doesn't alert every few
    seconds; the edge is untouched), "keep" (network blip / 5xx: says nothing about
    our rate, pause unchanged)."""
    p = board.pause
    if verdict == "ok":
        board.prev_429 = False
        if board.edge and p < board.edge:
            board.below_edge += 1
        if board.hold:
            board.hold -= 1
        else:
            fast = (not board.edge or p > board.edge * PAUSE_NEAR_ZONE
                    or board.below_edge >= PAUSE_EDGE_STALE)
            p -= PAUSE_FAST_STEP_S if fast else PAUSE_NEAR_STEP_S
    elif verdict == "429":
        board.edge = p
        p *= PAUSE_REPEAT_X if board.prev_429 else PAUSE_BACKOFF
        board.hold = PAUSE_HOLD_TRIES
        board.below_edge = 0
        board.prev_429 = True
    elif verdict == "slow":
        p *= PAUSE_REPEAT_X
    board.pause = min(config.PAUSE_MAX_S, max(config.PAUSE_MIN_S, p))
    board.save()
    return int(board.pause) + random.randint(0, 5)


def _announce_unnotified(have: set, notified: set) -> None:
    """Send a Telegram notice + SSH keys for any filled slot not yet announced
    (covers instances a failed attempt created but never reported)."""
    for name in sorted(have - notified):
        try:
            info = oci_client.find_existing_instance(name)
        except Exception as e:
            logger.warning("Lookup of %s failed: %s", name, e)
            info = None
        if info:
            logger.info("Instance already running: %s | IP: %s", info["name"], info["public_ip"])
            notifier.notify_already_exists(info["name"], info["public_ip"], info["region"], info["state"])
        notified.add(name)


LOG_LINES = 10


def _send_log_tail() -> None:
    try:
        with open("hunter.log", "r") as f:
            lines = f.readlines()
        tail = "".join(lines[-LOG_LINES:]) or "Log is empty."
        notifier.send_message(f"<pre>{notifier.esc(tail)}</pre>", silent=True)
    except Exception as e:
        notifier.send_message(f"Could not read log: {notifier.esc(e)}", silent=True)


def _send_log_file(arg: str = "") -> None:
    """/logfile → today's log; /logfile 07.11 → that day's archived log; /logfile list → days."""
    try:
        today = _local_now().date()
        if arg == "list":
            days = _archived_days()
            text = ("Archived logs (/logfile DD.MM):\n" + "\n".join(days[-LOG_KEEP_DAYS:])) if days else "No archived logs yet."
            notifier.send_message(text, silent=True)
            return
        day = _parse_day(arg) if arg else today
        if day is None:
            notifier.send_message("Usage: /logfile, /logfile 07.11, /logfile list", silent=True)
            return
        if day == today:
            _rotate_log()  # just after midnight hunter.log may still hold yesterday
        path = "hunter.log" if day == today else os.path.join(LOG_DIR, f"hunter_{day.isoformat()}.log")
        if not os.path.exists(path):
            notifier.send_message(f"No log for {day:%d.%m.%Y}. /logfile list shows what's there.", silent=True)
            return
        with open(path, "rb") as f:
            content = f.read() or b"Empty log."
        if not notifier.send_document(f"hunter_{day.isoformat()}.log", content):
            notifier.send_message("Could not send the log file (Telegram refused it, see /logs).", silent=True)
    except Exception as e:
        notifier.send_message(f"Could not send log file: {notifier.esc(e)}", silent=True)


def _bot_listener() -> None:
    url_base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    offset = 0
    while not _stop_event.is_set():
        try:
            resp = requests.get(f"{url_base}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                if text.startswith("/logs"):
                    _send_log_tail()
                elif text.startswith("/logfile"):
                    _send_log_file(text.split(maxsplit=1)[1].strip() if " " in text else "")
                elif text.startswith("/status"):
                    if _board:
                        notifier.send_message(_board.render(), silent=True)
                    else:
                        notifier.send_message("Starting up...", silent=True)
                elif text.startswith("/load"):
                    notifier.send_message(f"<pre>{server_stats.format_report()}</pre>", silent=True)
        except Exception as e:
            logger.warning("Bot listener error: %s", e)
            time.sleep(5)


def run() -> None:
    global _board
    target = config.TARGET_INSTANCES
    logger.info("=== OracleInstanceHunter started. Target: %d instance(s). Self-tuning pause %d-%ds ===",
                target, config.PAUSE_MIN_S, config.PAUSE_MAX_S)
    threading.Thread(target=_bot_listener, daemon=True).start()
    watchdog.start(_stop_event)
    slots = _slot_names()
    ads = config.AVAILABILITY_DOMAINS
    _board = board = status_board.Board(slots, ",".join(_ad_short(ad) for ad in ads))
    board.event("▶️ started")
    board.refresh()
    board.start_worker()  # from here on Telegram edits run off the hunting thread
    _rotate_log()  # a restart across midnight must still archive yesterday

    # Must know what already exists before launching anything — otherwise a
    # listing failure at startup could re-create an existing slot (duplicate).
    have = _have_slots()
    while have is None and not _stop_event.is_set():
        _stop_event.wait(60)
        have = _have_slots()
    if have is None:
        _finish(board, "stopped", "⏹ stopped")
        return
    board.have = set(have)  # a copy: the board renders from other threads
    board.poke()  # show the real slots now, not after the first attempt
    notified: set = set()
    if have:
        _announce_unnotified(have, notified)
    if len(have) >= target:
        logger.info("Already have %d/%d instance(s): %s. Nothing to do.", len(have), target, sorted(have))
        _finish(board, "done", "🏁 all slots already filled")
        return

    # A restart usually comes right after an attempt; going again at once got a 429
    # on Zurich after every deploy. Wait the remembered pause first.
    first_wait = int(board.pause)
    board.next_try = _local_now() + datetime.timedelta(seconds=first_wait)
    board.poke()
    _stop_event.wait(first_wait)

    last_day = _local_now().date()
    transient_streak = 0
    ad_turn = 0  # one AD per attempt, round-robin: 1 call/attempt keeps us under the 429 limit

    while not _stop_event.is_set() and len(have) < target:
        now = _local_now()
        if now.date() != last_day:  # the day summary itself is sent by board.attempt()
            _rotate_log()
            last_day = now.date()

        next_name = next(n for n in slots if n not in have)
        prev_count = len(have)

        ad = ads[ad_turn % len(ads)]
        ad_turn += 1
        ad_label = f"AD-{_ad_short(ad)}" + (f" of {len(ads)}" if len(ads) > 1 else "")
        board.ads_label = ad_label

        try:
            result = oci_client.launch_instance(next_name, ad)
            transient_streak = 0
            have.add(result["name"])
            board.have = set(have)  # a copy: the board renders from other threads
            logger.info("Instance created: %s | IP: %s (%d/%d)", result["name"], result["public_ip"], len(have), target)
            _notify_success_safe(result)
            notified.add(result["name"])
            board.event(f"🎉 created {result['name']} ({result['public_ip']})")
            delay = _next_delay(board, "ok")
            board.attempt("won", delay)
            if len(have) >= target:
                logger.info("=== OracleInstanceHunter finished. All %d instance(s) ready ===", target)
                _finish(board, "done", "🏁 all slots filled")
                return

        except Exception as e:
            is_service = isinstance(e, oci.exceptions.ServiceError)
            if is_service and is_out_of_capacity(e):
                if transient_streak >= TRANSIENT_ALERT_AFTER:
                    notifier.send_message("Connection to OCI restored, hunting normally.", silent=True)
                    board.event("🔌 connection to OCI restored")
                transient_streak = 0
                delay = _next_delay(board, "ok")
                logger.info("Out of capacity. Retrying in %d seconds... [%s]", delay, ad_label)
                board.attempt("capacity", delay)
                _stop_event.wait(delay)
                continue
            # Any other error: re-check what actually exists — the attempt may
            # have created an instance, or the account cap may already be met.
            # Announce those and stop before spamming or creating duplicates.
            # A 429 means OCI rejected the call outright (nothing created), so no
            # re-list then: it would only add API calls while we're throttled.
            # 🔴 Holds only because LaunchInstance has NO SDK retry (oci: "will not retry
            # by default"). If a retry_strategy is ever added, a lost reply + retried 429
            # could hide a created VM — then re-list on 429 too.
            rate_limited = is_service and e.status == 429
            if not rate_limited:
                have = _have_slots(have)
                board.have = set(have)
                _announce_unnotified(have, notified)
            delay = _next_delay(board, "429" if rate_limited else "keep" if _is_transient(e) else "slow")
            if len(have) >= target:
                board.attempt("won")
                _finish(board, "done", "🏁 all slots filled")
                return
            if len(have) > prev_count:
                logger.info("Instance created despite error; hunting remaining slot(s)")
                transient_streak = 0
                board.event("🎉 instance created despite an error")
                board.attempt("won", delay)
            elif _is_transient(e):
                # Rate limit / network blip: retry quietly. A 429 is the self-tuning
                # pause working as meant (OCI is reachable), so only network/5xx
                # failures build the "can't reach OCI" streak that alerts.
                short = f"{e.status} {e.code}" if is_service else type(e).__name__
                if rate_limited:
                    logger.warning("Rate limited: %s (pause now %d s)", short, board.pause)
                else:
                    transient_streak += 1
                    logger.warning("Temporary error (%d in a row): %s", transient_streak, short if is_service else e)
                board.event(f"⏳ {short}, retried")
                board.attempt("throttled" if rate_limited else "network", delay)
                if not rate_limited and transient_streak == TRANSIENT_ALERT_AFTER:
                    notifier.send_message(
                        f"Can't reach OCI for {transient_streak} attempts in a row (still retrying):\n"
                        f"<code>{notifier.esc(e)}</code>")
            else:
                label = "OCI service error" if is_service else "Unexpected error"
                transient_streak = 0  # "in a row" means in a row
                logger.error("%s: %s", label, e)
                notifier.send_message(f"{label}:\n<code>{notifier.esc(e)}</code>")
                board.event(f"❌ {label}: {(e.code if is_service else type(e).__name__)}")
                board.attempt("errors", delay)

        _stop_event.wait(delay)

    if _stop_event.is_set():
        logger.info("=== OracleInstanceHunter stopped by signal ===")
        _finish(board, "stopped", "⏹ stopped")


def _finish(board: status_board.Board, phase: str, note: str) -> None:
    board.phase = phase
    board.event(note)
    board.refresh()


if __name__ == "__main__":
    run()
