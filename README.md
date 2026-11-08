# OracleInstanceHunter

Hunts for Oracle Cloud **Always Free ARM** capacity. It keeps trying to create a **VM.Standard.A1.Flex** instance (Ubuntu 22.04 aarch64) and tells you on Telegram (and optionally by email) the moment it succeeds.

## Features

- **Polling with jitter**: self-tuning pause (starts at 60 s, −3 s after each normal answer, ×1.5 after each 429, bounded 30–300 s) so each account runs at its own OCI rate limit; ADs rotate one per attempt, silent retry on "Out of host capacity"
- **Multiple availability domains**: give a comma-separated list and attempts rotate through them, one AD per attempt
- **Multiple instances**: `TARGET_INSTANCES=2` hunts `arm-instance` + `arm-instance-2` (e.g. two 1 OCPU / 6 GB boxes), resumes correctly after a restart
- **Duplicate-safe**: never retries once a VM was created; re-checks existing instances after any error
- **Win notification**: Telegram message with name, public IP, SSH command, plus the SSH key files; optional email with the keys attached (works even if you mute the Telegram chat)
- **Resilient**: Telegram/SMTP outages never crash the hunter; short network blips are retried quietly (alert only after 5 in a row)
- **Telegram bot commands**: `/status`, `/logs` (last 10 lines), `/logfile` (today's log; `/logfile 07.11` for an earlier day, `/logfile list`), `/load` (CPU/RAM/disk)
- **Housekeeping**: one pinned status message edited in place (no hourly spam), silent daily summary, logs archived per day for 30 days, times in `TIMEZONE` (default Europe/Berlin, DST-aware), graceful shutdown on SIGTERM, bot token masked in logs

> Since 2026-06-15 the Always Free A1 allowance is **2 OCPU / 12 GB per account** (was 4 / 24).

## Setup

### 1. OCI Console preparation

- **VCN**: create with Internet Gateway + route rule `0.0.0.0/0 → IGW`
- **IAM Policy** (Identity & Security → Policies → root compartment):
  ```
  Allow any-user to manage instance-family in tenancy
  Allow any-user to manage virtual-network-family in tenancy
  ```
- **API Key**: generate RSA key pair, add public key to your OCI user (Identity → Users → API Keys)

### 2. Generate SSH key for the ARM instance (on the server)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/oracle_arm_key -N ""
```

### 3. Install

```bash
git clone https://github.com/YouCanTrustMe/OracleInstanceHunter
cd OracleInstanceHunter
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
# fill in .env, copy OCI API .pem to OCI_KEY_FILE path
chmod 600 ~/.oci/oci_api_key.pem
```

### 4. Configure `.env`

| Variable | Description |
|---|---|
| `OCI_USER` | Your OCI user OCID |
| `OCI_FINGERPRINT` | API key fingerprint |
| `OCI_TENANCY` | Tenancy OCID |
| `OCI_REGION` | e.g. `eu-zurich-1` |
| `OCI_KEY_FILE` | Path to OCI API private key `.pem` |
| `COMPARTMENT_ID` | Use tenancy OCID for root compartment |
| `SUBNET_ID` | Subnet OCID from your VCN |
| `AVAILABILITY_DOMAIN` | e.g. `xxxx:EU-ZURICH-1-AD-1`; several comma-separated to rotate through all ADs of the region |
| `IMAGE_ID` | Leave empty — auto-detects latest Ubuntu 22.04 aarch64 |
| `SSH_PUBLIC_KEY_PATH` | Path to `.pub` key for the ARM instance |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your chat ID (get via Telegram `getUpdates`) |
| `INSTANCE_NAME` | Base display name (default `arm-instance`) |
| `TARGET_INSTANCES` | How many instances to hunt (default `1`) |
| `OCPUS` / `MEMORY_GB` | Size of each instance (total must fit 2 OCPU / 12 GB) |
| `EMAIL_*` | Optional win email over SMTP — see `.env.example` |

### 5. Run

```bash
# foreground
venv/bin/python main.py

# live log
tail -f hunter.log
```

### 6. Run as systemd service (auto-start on reboot)

```bash
sudo cp oracle-hunter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable oracle-hunter
sudo systemctl start oracle-hunter
sudo systemctl status oracle-hunter
sudo journalctl -u oracle-hunter -f
```

### 7. Add swap (recommended on 1 GB micro instances)

Without swap a 1 GB E2.1.Micro can freeze completely when memory runs out.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Project structure

```
├── main.py                 # Polling loop + Telegram bot command listener
├── config.py               # Loads all config from .env
├── oci_client.py           # OCI API: launch instance, detect image, get public IP
├── notifier.py             # Telegram notifications (silent/loud)
├── server_stats.py         # Server load snapshot (/load command)
├── watchdog.py             # Peer watchdog: alert if the other server stops responding
├── oracle-hunter.service   # systemd unit file
├── .env.example            # Config template
├── accounts.example.json   # Template for gitignored accounts.json (used by tools/)
├── requirements.txt
├── tools/                  # Helper scripts (run manually, not by the service)
│   ├── manage_servers.py   # List / reboot instances across accounts
│   ├── create_e2_micro.py  # One-shot E2.1.Micro creation
│   ├── test_launch.py      # Manual launch/terminate test (run on a hunter server)
│   └── accounts_config.py  # Loads gitignored accounts.json
└── secrets/                # Gitignored: OCI API keys (*.pem) + generated SSH keys
```
