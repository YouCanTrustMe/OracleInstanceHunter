import requests
import config


def send_message(text: str, silent: bool = False) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    requests.post(url, json=payload, timeout=10)


def notify_started(interval: int) -> None:
    send_message(f"OracleInstanceHunter started. Polling every {interval}s.", silent=True)


def notify_heartbeat(attempt: int) -> None:
    send_message(f"Still hunting... attempt #{attempt}", silent=True)


def notify_success(instance_name: str, public_ip: str, region: str) -> None:
    text = (
        f"<b>ARM instance created successfully</b>\n\n"
        f"Name: <code>{instance_name}</code>\n"
        f"Public IP: <code>{public_ip}</code>\n"
        f"Region: <code>{region}</code>"
    )
    send_message(text)
