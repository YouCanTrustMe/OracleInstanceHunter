import os
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
AVAILABILITY_DOMAIN = os.environ["AVAILABILITY_DOMAIN"]

INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "arm-instance")
TARGET_INSTANCES = int(os.environ.get("TARGET_INSTANCES", "1"))
OCPUS = float(os.environ.get("OCPUS", "4"))
MEMORY_GB = float(os.environ.get("MEMORY_GB", "24"))
SHAPE = os.environ.get("SHAPE", "VM.Standard.A1.Flex")

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

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
