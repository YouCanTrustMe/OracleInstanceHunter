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

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M")
_fmt.converter = lambda ts: time.gmtime(ts + 7200)  # UTC+2

_fh = logging.FileHandler("hunter.log")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)

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


def _have_slots() -> set:
    """Which of our target slots are already filled by a running instance."""
    try:
        running = oci_client.running_instance_names()
    except Exception as e:
        logger.warning("Instance listing failed: %s", e)
        return set()
    return set(_slot_names()) & running


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
    notifier.notify_started()

    slots = _slot_names()
    have = _have_slots()
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
            result = oci_client.launch_instance(next_name)
            have.add(result["name"])
            logger.info("Instance created: %s | IP: %s (%d/%d)", result["name"], result["public_ip"], len(have), target)
            _notify_success_safe(result)
            notified.add(result["name"])
            if len(have) >= target:
                logger.info("=== OracleInstanceHunter finished. All %d instance(s) ready ===", target)
                break

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity(e):
                delay = random.randint(121, 147)
                logger.info("Out of capacity. Retrying in %d seconds...", delay)
                _stop_event.wait(delay)
                continue
            # Non-capacity error (e.g. LimitExceeded). Re-check what actually
            # exists: the attempt may have created an instance, or the account
            # cap may already be met — announce those and stop before spamming.
            have = _have_slots()
            _announce_unnotified(have, notified)
            if len(have) >= target:
                break
            if len(have) > prev_count:
                logger.info("Instance created despite error; hunting remaining slot(s)")
            else:
                logger.error("OCI service error: %s", e)
                notifier.send_message(f"OCI service error:\n<code>{e}</code>")

        except Exception as e:
            have = _have_slots()
            _announce_unnotified(have, notified)
            if len(have) >= target:
                break
            if len(have) > prev_count:
                logger.info("Instance created despite error; hunting remaining slot(s)")
            else:
                logger.error("Unexpected error: %s", e)
                notifier.send_message(f"Unexpected error:\n<code>{e}</code>")

        delay = random.randint(121, 147)
        _stop_event.wait(delay)

    if _stop_event.is_set():
        logger.info("=== OracleInstanceHunter stopped by signal ===")


if __name__ == "__main__":
    run()
