"""Shared test setup: fake config env, a throw-away working directory (main.py writes
hunter.log / hunter_state.json into the cwd) and a Telegram stub that records calls
instead of sending them. Import this first in every test module."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for key in ("OCI_USER", "OCI_FINGERPRINT", "OCI_TENANCY", "OCI_KEY_FILE", "COMPARTMENT_ID",
            "SUBNET_ID", "SSH_PUBLIC_KEY_PATH", "TELEGRAM_BOT_TOKEN"):
    os.environ[key] = "test"
os.environ.update(OCI_REGION="eu-test-1", AVAILABILITY_DOMAIN="x:EU-TEST-1-AD-1",
                  TELEGRAM_CHAT_ID="42", INSTANCE_NAME="arm", TARGET_INSTANCES="2",
                  OCPUS="1", MEMORY_GB="6", HUNT_SINCE="2026-01-01", TIMEZONE="Europe/Berlin",
                  PAUSE_START_S="60", PAUSE_MIN_S="15", PAUSE_MAX_S="300", EMAIL_ENABLED="false")
os.chdir(tempfile.mkdtemp(prefix="hunter-tests-"))

import notifier  # noqa: E402

sent = []  # (method, payload) of every Telegram call


def _fake_api(method, files=None, to_chat=True, **payload):
    sent.append((method, payload))
    return {"ok": True, "result": {"message_id": len(sent)}}


notifier._api = _fake_api


def fresh_state() -> None:
    """Forget the saved board state and recorded Telegram calls."""
    sent.clear()
    for name in ("hunter_state.json", "hunter_state.json.tmp"):
        if os.path.exists(name):
            os.remove(name)
