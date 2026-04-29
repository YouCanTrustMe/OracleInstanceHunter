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
IMAGE_ID = os.environ["IMAGE_ID"]
AVAILABILITY_DOMAIN = os.environ["AVAILABILITY_DOMAIN"]

INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "arm-instance")
OCPUS = float(os.environ.get("OCPUS", "4"))
MEMORY_GB = float(os.environ.get("MEMORY_GB", "24"))
SHAPE = os.environ.get("SHAPE", "VM.Standard.A1.Flex")

SSH_PUBLIC_KEY_PATH = os.environ["SSH_PUBLIC_KEY_PATH"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
