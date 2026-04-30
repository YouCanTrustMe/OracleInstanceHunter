import time
import random
import logging
import threading
import signal
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


def _handle_signal(signum, frame) -> None:
    logger.info("Signal received (%s), shutting down...", signum)
    _stop_event.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def is_out_of_capacity(error: oci.exceptions.ServiceError) -> bool:
    return OUT_OF_CAPACITY_MSG in str(error.message)


HEARTBEAT_INTERVAL = 3600  # send heartbeat every 1 hour
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
    import datetime
    try:
        today = datetime.date.today().isoformat()
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
    return f"Attempt #{attempt}, running for {h}h {m:02d}m"


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
    logger.info("=== OracleInstanceHunter started. Random delay: 121-147s between attempts ===")
    threading.Thread(target=_bot_listener, daemon=True).start()
    notifier.notify_started()

    existing = oci_client.find_existing_instance()
    if existing:
        logger.info("Instance already exists: %s | IP: %s | State: %s", existing["name"], existing["public_ip"], existing["state"])
        notifier.notify_already_exists(existing["name"], existing["public_ip"], existing["region"], existing["state"])
        logger.info("=== OracleInstanceHunter finished. Nothing to do ===")
        return

    _state["attempt"] = 0
    _state["start_time"] = time.time()
    last_heartbeat = time.time()

    while not _stop_event.is_set():
        _state["attempt"] += 1
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            notifier.notify_heartbeat(_state["attempt"])
            last_heartbeat = now

        try:
            result = oci_client.launch_instance()
            logger.info("Instance created: %s | IP: %s", result["name"], result["public_ip"])
            notifier.notify_success(result["name"], result["public_ip"], result["region"])
            logger.info("=== OracleInstanceHunter finished. Instance is ready ===")
            break

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity(e):
                delay = random.randint(121, 147)
                logger.info("Out of capacity. Retrying in %d seconds...", delay)
                _stop_event.wait(delay)
                continue
            else:
                logger.error("OCI service error: %s", e)

        except Exception as e:
            logger.error("Unexpected error: %s", e)

        delay = random.randint(121, 147)
        _stop_event.wait(delay)

    if _stop_event.is_set():
        logger.info("=== OracleInstanceHunter stopped by signal ===")


if __name__ == "__main__":
    run()
