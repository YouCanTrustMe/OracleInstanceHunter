"""Loads per-account OCI config from the gitignored accounts.json.

Keeps real OCIDs / fingerprints / subnet IDs out of tracked source. The private
API keys (*.pem) and Telegram tokens (local.env) are also gitignored and never
committed. Copy accounts.example.json -> accounts.json and fill in your values.
"""
import os
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.json")


def load_accounts() -> dict:
    with open(_ACCOUNTS_FILE) as f:
        accounts = json.load(f)
    for cfg in accounts.values():
        if "key_file" in cfg:
            cfg["key_file"] = os.path.join(SCRIPT_DIR, cfg["key_file"])
    return accounts
