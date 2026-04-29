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


def notify_already_exists(instance_name: str, public_ip: str, region: str, state: str) -> None:
    ssh_cmd = f"ssh -i ~/.ssh/oracle_arm_key ubuntu@{public_ip}"
    text = (
        f"<b>Instance already exists — nothing to do</b>\n\n"
        f"Name: <code>{instance_name}</code>\n"
        f"Public IP: <code>{public_ip}</code>\n"
        f"Region: <code>{region}</code>\n"
        f"State: <code>{state}</code>\n\n"
        f"SSH:\n<code>{ssh_cmd}</code>"
    )
    send_message(text)
    _send_ssh_keys(public_ip)


def notify_started() -> None:
    send_message("OracleInstanceHunter started. Polling every 121-147s.", silent=True)


def notify_heartbeat(attempt: int) -> None:
    send_message(f"Still hunting... attempt #{attempt}", silent=True)


def notify_success(instance_name: str, public_ip: str, region: str) -> None:
    ssh_cmd = f"ssh -i ~/.ssh/oracle_arm_key ubuntu@{public_ip}"
    text = (
        f"<b>ARM instance created successfully</b>\n\n"
        f"Name: <code>{instance_name}</code>\n"
        f"Public IP: <code>{public_ip}</code>\n"
        f"Region: <code>{region}</code>\n\n"
        f"SSH:\n<code>{ssh_cmd}</code>"
    )
    send_message(text)
    _send_ssh_keys(public_ip)


def _send_ssh_keys(public_ip: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
    key_files = [
        ("/home/ubuntu/.ssh/oracle_arm_key", f"oracle_arm_key"),
        ("/home/ubuntu/.ssh/oracle_arm_key.pub", f"oracle_arm_key.pub"),
    ]
    for path, filename in key_files:
        try:
            with open(path, "rb") as f:
                requests.post(
                    url,
                    data={"chat_id": config.TELEGRAM_CHAT_ID, "caption": f"SSH key: {filename}\nIP: {public_ip}"},
                    files={"document": (filename, f)},
                    timeout=15,
                )
        except Exception as e:
            send_message(f"Could not send SSH key {filename}: {e}", silent=True)
