import html
import logging
import smtplib
from email.message import EmailMessage

import requests
import config

logger = logging.getLogger(__name__)


def esc(value) -> str:
    """Make any text (exception messages, log lines) safe inside parse_mode=HTML."""
    return html.escape(str(value), quote=False)


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
            send_message(f"Email alert failed: {esc(e)}", silent=True)
        except Exception:
            pass


def _api(method: str, files: dict = None, to_chat: bool = True, **payload) -> dict | None:
    """Call the Telegram Bot API (multipart when `files` is given). Never raises;
    returns the decoded reply or None if Telegram couldn't be reached."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    if to_chat:
        payload = {"chat_id": config.TELEGRAM_CHAT_ID, **payload}
    try:
        if files:
            reply = requests.post(url, data=payload, files=files, timeout=30).json()
        else:
            reply = requests.post(url, json=payload, timeout=10).json()
    except Exception as e:
        logger.warning("Telegram %s failed: %s", method, e)
        return None
    if not reply.get("ok"):
        logger.warning("Telegram %s refused: %s", method, reply.get("description"))
    return reply


def send_message(text: str, silent: bool = False) -> int | None:
    """Never raises — a Telegram outage must not crash the hunter. Returns message_id."""
    reply = _api("sendMessage", text=text, parse_mode="HTML",
                 disable_notification=silent, disable_web_page_preview=True)
    return (reply or {}).get("result", {}).get("message_id")


def edit_message(message_id: int, text: str) -> str:
    """Edit a message in place: "ok"; "gone" (deleted / too old — send a new one);
    "refused" (Telegram said no for another reason); "failed" (no answer: network)."""
    reply = _api("editMessageText", message_id=message_id, text=text, parse_mode="HTML",
                 disable_web_page_preview=True)
    if reply is None:
        return "failed"
    desc = str(reply.get("description", ""))
    if reply.get("ok") or "not modified" in desc:
        return "ok"
    if reply.get("error_code") == 429:  # "Too Many Requests: retry after N" — temporary
        return "failed"
    return "gone" if ("not found" in desc or "can't be edited" in desc) else "refused"


def send_document(filename: str, content: bytes, caption: str = None, silent: bool = True,
                  html_caption: bool = False) -> bool:
    """Upload a file to the chat. Never raises; False (and a log line) if it didn't arrive."""
    extra = {"caption": caption} if caption else {}
    if caption and html_caption:
        extra["parse_mode"] = "HTML"
    reply = _api("sendDocument", files={"document": (filename, content)},
                 disable_notification=silent, **extra)
    return bool(reply and reply.get("ok"))


def set_commands(commands: list) -> None:
    """The bot's "/" menu, from the code, so every bot shows the same current list."""
    _api("setMyCommands", to_chat=False,
         commands=[{"command": c, "description": d} for c, d in commands])


def pin_message(message_id: int) -> None:
    _api("pinChatMessage", message_id=message_id, disable_notification=True)


def unpin_message(message_id: int) -> None:
    _api("unpinChatMessage", message_id=message_id)


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
    for path, filename in _SSH_KEY_FILES:
        try:
            with open(path, "rb") as f:
                content = f.read()
        except Exception as e:
            send_message(f"Could not send SSH key {filename}: {esc(e)}", silent=True)
            continue
        if not send_document(filename, content, caption=f"SSH key: {filename}\nIP: {public_ip}", silent=False):
            send_message(f"Could not send SSH key {filename} (see /log)", silent=True)
