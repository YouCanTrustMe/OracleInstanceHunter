import time
import random
import logging
import threading
import signal
import datetime
import requests
import oci.exceptions

import config
import oci_client
import notifier
import server_stats
import watchdog

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M")
_fmt.converter = lambda ts: time.gmtime(ts + 7200)  # UTC+2

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
_state: dict = {"attempt": 0, "start_time": 0.0}

_TZ = datetime.timezone(datetime.timedelta(hours=2))


def _local_now() -> datetime.datetime:
    return datetime.datetime.now(_TZ)


def _count_today_attempts() -> int:
    today = _local_now().date().isoformat()
    try:
        with open("hunter.log", "r") as f:
            lines = f.readlines()
        return sum(1 for l in lines if l.startswith(today) and "Out of capacity. Retrying in" in l)
    except FileNotFoundError:
        return 0


def _rotate_log(date: datetime.date, total_attempts: int) -> None:
    date_str = date.isoformat()
    try:
        with open("hunter.log", "r") as f:
            lines = f.readlines()
        day_lines = [l for l in lines if l.startswith(date_str)]

        capacity_errors = sum(1 for l in day_lines if "Out of capacity. Retrying in" in l)
        oci_errors = sum(1 for l in day_lines if "OCI service error:" in l)
        unexpected = sum(1 for l in day_lines if "Unexpected error:" in l)

        summary = (
            f"Daily summary {date_str}:\n"
            f"Attempts: {total_attempts}\n"
            f"Out of capacity: {capacity_errors}\n"
            f"OCI errors: {oci_errors}\n"
            f"Unexpected errors: {unexpected}"
        )
        notifier.send_message(summary, silent=True)

        with open("hunter.log", "w"):
            pass
        logger.info("=== Log rotated for %s ===", date_str)
    except Exception as e:
        logger.warning("Log rotation failed: %s", e)


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


TRANSIENT_ALERT_AFTER = 5  # consecutive network failures (~12 min) before a Telegram alert


def _launch_any_ad(name: str) -> dict:
    """Try every configured AD in turn. Capacity errors move on to the next AD;
    any other error stops the round immediately (the caller re-checks for dupes)."""
    last_capacity_error = None
    for i, ad in enumerate(config.AVAILABILITY_DOMAINS):
        if i:
            _stop_event.wait(2)
        try:
            return oci_client.launch_instance(name, ad)
        except oci.exceptions.ServiceError as e:
            if not is_out_of_capacity(e):
                raise
            last_capacity_error = e
    raise last_capacity_error


def _ad_short(ad: str) -> str:
    return ad.rsplit("-", 1)[-1] if "-AD-" in ad.upper() else ad


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
        notifier.send_message(f"<pre>{tail}</pre>", silent=True)
    except Exception as e:
        notifier.send_message(f"Could not read log: {e}", silent=True)


def _send_log_file() -> None:
    try:
        today = _local_now().date().isoformat()
        with open("hunter.log", "r") as f:
            lines = f.readlines()
        today_lines = [l for l in lines if l.startswith(today)]
        content = "".join(today_lines) or "No entries for today."
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
        requests.post(url, data={"chat_id": config.TELEGRAM_CHAT_ID, "disable_notification": True},
                      files={"document": (f"hunter_{today}.log", content.encode())}, timeout=15)
    except Exception as e:
        notifier.send_message(f"Could not send log file: {e}", silent=True)


def _format_status() -> str:
    attempt = _state["attempt"]
    elapsed = int(time.time() - _state["start_time"])
    h, m = divmod(elapsed // 60, 60)
    today = _local_now().date().isoformat()
    return f"Attempt #{attempt} today ({today}), running for {h}h {m:02d}m"


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
                    _send_log_file()
                elif text.startswith("/status"):
                    notifier.send_message(_format_status(), silent=True)
                elif text.startswith("/load"):
                    notifier.send_message(f"<pre>{server_stats.format_report()}</pre>", silent=True)
        except Exception as e:
            logger.warning("Bot listener error: %s", e)
            time.sleep(5)


def run() -> None:
    target = config.TARGET_INSTANCES
    logger.info("=== OracleInstanceHunter started. Target: %d instance(s). Random delay: 121-147s ===", target)
    threading.Thread(target=_bot_listener, daemon=True).start()
    watchdog.start(_stop_event)
    notifier.notify_started()

    slots = _slot_names()
    # Must know what already exists before launching anything — otherwise a
    # listing failure at startup could re-create an existing slot (duplicate).
    have = _have_slots()
    while have is None and not _stop_event.is_set():
        _stop_event.wait(60)
        have = _have_slots()
    if have is None:
        return
    notified: set = set()
    if have:
        _announce_unnotified(have, notified)
    if len(have) >= target:
        logger.info("Already have %d/%d instance(s): %s. Nothing to do.", len(have), target, sorted(have))
        return

    _state["attempt"] = _count_today_attempts()
    _state["start_time"] = time.time()
    last_heartbeat_hour = _local_now().hour
    last_day = _local_now().date()
    transient_streak = 0
    ads_label = ",".join(_ad_short(ad) for ad in config.AVAILABILITY_DOMAINS)

    while not _stop_event.is_set() and len(have) < target:
        now = _local_now()
        if now.date() != last_day:
            _rotate_log(last_day, _state["attempt"])
            _state["attempt"] = 0
            last_day = now.date()
        _state["attempt"] += 1
        current_hour = now.hour
        if current_hour != last_heartbeat_hour:
            notifier.notify_heartbeat(_state["attempt"])
            last_heartbeat_hour = current_hour

        next_name = next(n for n in slots if n not in have)
        prev_count = len(have)

        try:
            result = _launch_any_ad(next_name)
            transient_streak = 0
            have.add(result["name"])
            logger.info("Instance created: %s | IP: %s (%d/%d)", result["name"], result["public_ip"], len(have), target)
            _notify_success_safe(result)
            notified.add(result["name"])
            if len(have) >= target:
                logger.info("=== OracleInstanceHunter finished. All %d instance(s) ready ===", target)
                break

        except Exception as e:
            is_service = isinstance(e, oci.exceptions.ServiceError)
            if is_service and is_out_of_capacity(e):
                if transient_streak >= TRANSIENT_ALERT_AFTER:
                    notifier.send_message("Connection to OCI restored, hunting normally.", silent=True)
                transient_streak = 0
                delay = random.randint(121, 147)
                logger.info("Out of capacity. Retrying in %d seconds... [%s]", delay, ads_label)
                _stop_event.wait(delay)
                continue
            # Any other error: re-check what actually exists — the attempt may
            # have created an instance, or the account cap may already be met.
            # Announce those and stop before spamming or creating duplicates.
            have = _have_slots(have)
            _announce_unnotified(have, notified)
            if len(have) >= target:
                break
            if len(have) > prev_count:
                logger.info("Instance created despite error; hunting remaining slot(s)")
            elif _is_transient(e):
                # Network blip / rate limit: retry quietly; alert only if it persists.
                transient_streak += 1
                logger.warning("Temporary error (%d in a row): %s", transient_streak,
                               f"{e.status} {e.code}" if is_service else e)
                if transient_streak == TRANSIENT_ALERT_AFTER:
                    notifier.send_message(
                        f"Can't reach OCI for {transient_streak} attempts in a row (still retrying):\n<code>{e}</code>")
            else:
                label = "OCI service error" if is_service else "Unexpected error"
                logger.error("%s: %s", label, e)
                notifier.send_message(f"{label}:\n<code>{e}</code>")

        delay = random.randint(121, 147)
        _stop_event.wait(delay)

    if _stop_event.is_set():
        logger.info("=== OracleInstanceHunter stopped by signal ===")


if __name__ == "__main__":
    run()
