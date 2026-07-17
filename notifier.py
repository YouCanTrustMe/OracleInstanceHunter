import smtplib
from email.message import EmailMessage

import requests
import config


def send_email(subject: str, body: str, attachments: list = None) -> None:
    """Send a plain-text email via SMTP. No-op unless EMAIL_ENABLED and creds are set.
    Never raises — a win must not be lost to an email failure."""
    if not (config.EMAIL_ENABLED and config.EMAIL_USER and config.EMAIL_PASSWORD and config.EMAIL_TO):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_USER
        msg["To"] = config.EMAIL_TO
        msg.set_content(body)
        for path, filename in attachments or []:
            try:
                with open(path, "rb") as f:
                    msg.add_attachment(f.read(), maintype="application", subtype="octet-stream", filename=filename)
            except Exception:
                pass
        with smtplib.SMTP_SSL(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT, timeout=20) as smtp:
            smtp.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        try:
            send_message(f"Email alert failed: {e}", silent=True)
        except Exception:
            pass


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
    send_email(
        f"[OracleHunter] Instance already exists: {instance_name}",
        f"Instance already exists — nothing to do\n\n"
        f"Name: {instance_name}\nPublic IP: {public_ip}\nRegion: {region}\nState: {state}\n\n"
        f"SSH:\n{ssh_cmd}",
        attachments=_ssh_key_attachments(),
    )


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
    send_email(
        f"[OracleHunter] ARM instance created: {instance_name}",
        f"ARM instance created successfully\n\n"
        f"Name: {instance_name}\nPublic IP: {public_ip}\nRegion: {region}\n\n"
        f"SSH:\n{ssh_cmd}",
        attachments=_ssh_key_attachments(),
    )


_SSH_KEY_FILES = [
    ("/home/ubuntu/.ssh/oracle_arm_key", "oracle_arm_key"),
    ("/home/ubuntu/.ssh/oracle_arm_key.pub", "oracle_arm_key.pub"),
]


def _ssh_key_attachments() -> list:
    return _SSH_KEY_FILES


def _send_ssh_keys(public_ip: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
    key_files = _SSH_KEY_FILES
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
