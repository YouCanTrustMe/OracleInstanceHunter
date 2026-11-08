import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

OCI_USER = os.environ["OCI_USER"]
OCI_FINGERPRINT = os.environ["OCI_FINGERPRINT"]
OCI_TENANCY = os.environ["OCI_TENANCY"]
OCI_REGION = os.environ["OCI_REGION"]
OCI_KEY_FILE = os.environ["OCI_KEY_FILE"]

COMPARTMENT_ID = os.environ["COMPARTMENT_ID"]
SUBNET_ID = os.environ["SUBNET_ID"]
IMAGE_ID = os.environ.get("IMAGE_ID", "")
# One AD, or several comma-separated: attempts rotate through them, one AD per attempt.
AVAILABILITY_DOMAINS = [ad.strip() for ad in os.environ["AVAILABILITY_DOMAIN"].split(",") if ad.strip()]

# Self-tuning pause between attempts: every clean answer makes it a bit shorter,
# every 429 TooManyRequests makes it clearly longer — so each account settles at
# its own OCI limit (which differs per account and time of day). Bounds in seconds.
PAUSE_START_S = int(os.environ.get("PAUSE_START_S", "60"))
PAUSE_MIN_S = int(os.environ.get("PAUSE_MIN_S", "30"))
PAUSE_MAX_S = int(os.environ.get("PAUSE_MAX_S", "300"))

INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "arm-instance")
TARGET_INSTANCES = int(os.environ.get("TARGET_INSTANCES", "1"))
OCPUS = float(os.environ.get("OCPUS", "4"))
MEMORY_GB = float(os.environ.get("MEMORY_GB", "24"))
SHAPE = os.environ.get("SHAPE", "VM.Standard.A1.Flex")
# Optional YYYY-MM-DD: when this account started hunting (shown as "Hunting N days").
HUNT_SINCE = os.environ.get("HUNT_SINCE", "").strip()
# Time zone for logs and Telegram (DST handled automatically).
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Berlin")
TZ = ZoneInfo(TIMEZONE)

SSH_PUBLIC_KEY_PATH = os.environ["SSH_PUBLIC_KEY_PATH"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Email alerts (optional — only sent on a win). Leave EMAIL_ENABLED unset to skip.
EMAIL_ENABLED = os.environ.get("EMAIL_ENABLED", "false").lower() in ("1", "true", "yes")
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "") or EMAIL_USER

# Peer watchdog (optional): watch another server's SSH banner and alert
# (Telegram + email) if it stops answering. Leave PEER_HOST empty to disable.
PEER_NAME = os.environ.get("PEER_NAME", "peer")
PEER_HOST = os.environ.get("PEER_HOST", "")
PEER_PORT = int(os.environ.get("PEER_PORT", "22"))
