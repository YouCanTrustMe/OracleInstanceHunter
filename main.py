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
import scout
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


def _wait_for_turn(deadline: float, sc: "scout.Scout | None") -> None:
    """Sleep until `deadline` (unix time), a scout "yes" or a stop signal."""
    while not _stop_event.is_set():
        left = deadline - time.time()
        if left <= 0:
            return
        if sc is None:
            _stop_event.wait(left)
        elif sc.wake.wait(min(left, 1)):  # 1 s steps: a stop signal still gets through
            sc.wake.clear()
            return


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


CAPTION_MAX = 1000  # Telegram caption limit is 1024 characters


def _log_caption(content: bytes) -> str:
    """Last lines of the log that fit into a file caption."""
    tail = ""
    for line in reversed(content.decode("utf-8", "replace").splitlines()):
        if len(tail) + len(line) + 1 > CAPTION_MAX:
            break
        tail = line + "\n" + tail
    return f"<pre>{notifier.esc(tail.rstrip())}</pre>" if tail.strip() else ""


def _send_log(arg: str = "") -> None:
    """/log → today's log file, its last lines as the caption; /log 07.11 → that
    day's archived log; /log list → which days exist."""
    try:
        today = _local_now().date()
        if arg == "list":
            days = _archived_days()
            text = ("Archived logs (/log DD.MM):\n" + "\n".join(days[-LOG_KEEP_DAYS:])) if days else "No archived logs yet."
            notifier.send_message(text, silent=True)
            return
        day = _parse_day(arg) if arg else today
        if day is None:
            notifier.send_message("Usage: /log, /log 07.11, /log list", silent=True)
            return
        if day == today:
            _rotate_log()  # just after midnight hunter.log may still hold yesterday
        path = "hunter.log" if day == today else os.path.join(LOG_DIR, f"hunter_{day.isoformat()}.log")
        if not os.path.exists(path):
            notifier.send_message(f"No log for {day:%d.%m.%Y}. /log list shows what's there.", silent=True)
            return
        with open(path, "rb") as f:
            content = f.read() or b"Empty log."
        if not notifier.send_document(f"hunter_{day.isoformat()}.log", content,
                                      caption=_log_caption(content) or None, html_caption=True):
            notifier.send_message("Could not send the log file (Telegram refused it).", silent=True)
    except Exception as e:
        notifier.send_message(f"Could not send log file: {notifier.esc(e)}", silent=True)


COMMANDS = [("info", "Totals, pause and OCI limit, server, code version"),
            ("log", "Today's log file (/log 07.11 for a day, /log list)")]


def _handle_command(text: str) -> None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()  # "/log@SomeBot" in groups
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/log":
        _send_log(arg)
    elif cmd == "/info":
        notifier.send_message(_board.info() if _board else "Starting up...", silent=True)
    else:
        notifier.send_message("Commands: /info · /log (today) · /log 07.11 · /log list", silent=True)


def _bot_listener() -> None:
    url_base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    offset = 0
    menu_set = False
    while not _stop_event.is_set():
        try:
            if not menu_set:  # retried until Telegram takes it (a blip at start-up)
                menu_set = notifier.set_commands(COMMANDS)
            resp = requests.get(f"{url_base}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                # Only our chat: anyone can find a bot and send it commands.
                if str(msg.get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
                    continue
                text = msg.get("text", "")
                if text.startswith("/"):
                    _handle_command(text)
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

    sc = None
    if config.SCOUT_INTERVAL_S > 0:
        sc = scout.Scout(ads, config.SCOUT_INTERVAL_S, on_check=board.scout_checked,
                         on_seen=lambda: board.scout_note("seen"))
        board.scout_interval = config.SCOUT_INTERVAL_S
        board.scout_health = sc.health
        threading.Thread(target=sc.run, args=(_stop_event,), daemon=True, name="scout").start()

    # A restart usually comes right after an attempt; going again at once got a 429
    # on Zurich after every deploy. Wait the remembered pause first.
    first_wait = int(board.pause)
    next_regular = time.time() + first_wait  # the normal launch rhythm
    board.next_try = _local_now() + datetime.timedelta(seconds=first_wait)
    board.poke()

    def calm() -> None:
        """OCI answered (anything but a network/5xx failure): the outage streak is over."""
        nonlocal transient_streak
        if transient_streak >= TRANSIENT_ALERT_AFTER:
            notifier.send_message("Connection to OCI restored, hunting normally.", silent=True)
        board.outage_over()  # also an outage line left open by a restart
        transient_streak = 0

    def plan(verdict: str, shot: bool) -> int:
        """Seconds until the next normal launch, after this one. A scout shot fires off
        the rhythm, so it never teaches the pause ("keep")."""
        nonlocal next_regular
        if shot:
            # When may the scout fire again? OCI seems to count rejected calls too, so a
            # shot soon after any call that reached it (a 429 included) would only keep its
            # limit shut: wait the pause, like the normal rhythm. A network blip (the call
            # may never have arrived): 10 s. A real error would only repeat: 5 min.
            sc.mute({"slow": scout.ERROR_MUTE_S, "keep": scout.RETRY_NET_S}.get(verdict, board.pause))
        if shot and verdict == "429":
            # OCI's limit is closed right now: the normal launch must not follow at once
            # (it would get a 429 too and teach the pause an edge the shot caused).
            next_regular = max(next_regular, time.time() + board.pause)
            return max(1, int(next_regular - time.time()))
        delay = _next_delay(board, "keep" if shot else verdict)
        now_ts = time.time()
        next_regular = max(next_regular, now_ts + delay) if shot else now_ts + delay
        return max(1, int(next_regular - now_ts))

    last_day = _local_now().date()
    transient_streak = 0
    streak_since = _local_now()  # when the current run of network/5xx failures began
    ad_turn = 0  # one AD per attempt, round-robin: 1 call/attempt keeps us under the 429 limit

    while not _stop_event.is_set() and len(have) < target:
        _wait_for_turn(next_regular, sc)
        if _stop_event.is_set():
            break
        scout_ad = sc.take() if sc else None
        if not scout_ad and time.time() < next_regular:
            continue  # a stale wake-up (muted or already gone)
        shot = scout_ad is not None
        now = _local_now()
        if now.date() != last_day:  # the day summary is sent by the board (attempt / scout check)
            _rotate_log()
            last_day = now.date()

        next_name = next(n for n in slots if n not in have)
        prev_count = len(have)

        if shot:
            ad = scout_ad
            ad_label = f"AD-{_ad_short(ad)}, scout"
            logger.info("Scout saw capacity in AD-%s, launching now", _ad_short(ad))
            board.event(f"🔭 capacity seen in AD-{_ad_short(ad)}, launching")
        else:
            ad = ads[ad_turn % len(ads)]
            ad_turn += 1
            ad_label = f"AD-{_ad_short(ad)}" + (f" of {len(ads)}" if len(ads) > 1 else "")
            board.ads_label = ad_label

        try:
            result = oci_client.launch_instance(next_name, ad)
            calm()
            have.add(result["name"])
            board.have = set(have)  # a copy: the board renders from other threads
            logger.info("Instance created: %s | IP: %s (%d/%d)", result["name"], result["public_ip"], len(have), target)
            _notify_success_safe(result)
            notified.add(result["name"])
            board.event(f"🎉 created {result['name']} ({result['public_ip']})")
            if sc:
                board.scout_note("won" if shot else "missed")
            delay = plan("ok", shot)
            board.attempt("won", delay)
            if len(have) >= target:
                logger.info("=== OracleInstanceHunter finished. All %d instance(s) ready ===", target)
                _finish(board, "done", "🏁 all slots filled")
                return

        except Exception as e:
            is_service = isinstance(e, oci.exceptions.ServiceError)
            if is_service and is_out_of_capacity(e):
                calm()
                delay = plan("ok", shot)
                if shot:
                    sc.false_alarm(ad)
                    board.scout_note("false")
                    board.event(f"🔭 false alarm in AD-{_ad_short(ad)}")
                    logger.info("Scout false alarm: launch said out of capacity [%s]; that AD's "
                                "\"yes\" ignored until the report says no", ad_label)
                else:
                    logger.info("Out of capacity. Retrying in %d seconds... [%s]", delay, ad_label)
                board.attempt("capacity", delay)
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
            delay = plan("429" if rate_limited else "keep" if _is_transient(e) else "slow", shot)
            if sc and len(have) > prev_count:
                board.scout_note("won" if shot else "missed")
            if len(have) >= target:
                board.attempt("won")
                _finish(board, "done", "🏁 all slots filled")
                return
            if len(have) > prev_count:
                logger.info("Instance created despite error; hunting remaining slot(s)")
                calm()
                board.event("🎉 instance created despite an error")
                board.attempt("won", delay)
            elif _is_transient(e):
                # Rate limit / network blip: retry quietly. A 429 is the self-tuning
                # pause working as meant (OCI is reachable), so only network/5xx
                # failures build the "can't reach OCI" streak that alerts.
                short = f"{e.status} {e.code}" if is_service else type(e).__name__
                if rate_limited and shot:
                    logger.warning("Rate limited: %s on the scout shot, next try in %d s",
                                   short, board.pause)
                elif rate_limited:
                    logger.warning("Rate limited: %s (pause now %d s)", short, board.pause)
                else:
                    transient_streak += 1
                    if transient_streak == 1:
                        streak_since = _local_now()
                    logger.warning("Temporary error (%d in a row): %s", transient_streak, short if is_service else e)
                # No journal line per blip: 429s and network blips are counted on the
                # board; only a real outage (below) gets a journal line.
                board.attempt("throttled" if rate_limited else "network", delay)
                if not rate_limited and transient_streak == TRANSIENT_ALERT_AFTER:
                    board.event(f"{status_board.OUTAGE_PREFIX}{streak_since:%H:%M}")
                    notifier.send_message(
                        f"Can't reach OCI for {transient_streak} attempts in a row (still retrying):\n"
                        f"<code>{notifier.esc(e)}</code>")
            else:
                label = "OCI service error" if is_service else "Unexpected error"
                calm()  # "in a row" means in a row
                logger.error("%s: %s", label, e)
                notifier.send_message(f"{label}:\n<code>{notifier.esc(e)}</code>")
                board.event(f"❌ {label}: {(e.code if is_service else type(e).__name__)}")
                board.attempt("errors", delay)

    if _stop_event.is_set():
        logger.info("=== OracleInstanceHunter stopped by signal ===")
        _finish(board, "stopped", "⏹ stopped")


def _finish(board: status_board.Board, phase: str, note: str) -> None:
    board.phase = phase
    board.event(note)
    board.refresh()


if __name__ == "__main__":
    run()
