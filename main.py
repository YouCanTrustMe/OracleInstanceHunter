import time
import logging
import threading
import requests
import oci.exceptions

import config
import oci_client
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("hunter.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

OUT_OF_CAPACITY_CODE = "InternalError"
OUT_OF_CAPACITY_MSG = "Out of host capacity"


def is_out_of_capacity(error: oci.exceptions.ServiceError) -> bool:
    return OUT_OF_CAPACITY_MSG in str(error.message)


HEARTBEAT_INTERVAL = 1800  # send heartbeat every 30 minutes
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


def _bot_listener() -> None:
    url_base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    offset = 0
    while True:
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
        except Exception:
            time.sleep(5)


def run() -> None:
    logger.info("Starting OracleInstanceHunter. Polling every %d seconds.", config.POLL_INTERVAL)
    threading.Thread(target=_bot_listener, daemon=True).start()
    notifier.notify_started(config.POLL_INTERVAL)

    attempt = 0
    last_heartbeat = time.time()

    while True:
        attempt += 1
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            notifier.notify_heartbeat(attempt)
            last_heartbeat = now

        try:
            result = oci_client.launch_instance()
            logger.info("Instance created: %s | IP: %s", result["name"], result["public_ip"])
            notifier.notify_success(result["name"], result["public_ip"], result["region"])
            break

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity(e):
                logger.info("Out of capacity. Retrying in %d seconds...", config.POLL_INTERVAL)
            else:
                logger.error("OCI service error: %s", e)

        except Exception as e:
            logger.error("Unexpected error: %s", e)

        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    run()
