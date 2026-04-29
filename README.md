# OracleInstanceHunter

Polls the OCI API and automatically creates a **VM.Standard.A1.Flex** ARM instance (4 OCPUs, 24 GB RAM, Ubuntu 22.04) the moment capacity becomes available. Sends a Telegram notification on success.

## How it works

- Tries to launch the ARM instance every `POLL_INTERVAL` seconds (120 recommended)
- Silently retries on "Out of host capacity"
- Sends a **silent** Telegram message on startup and every 30 min (heartbeat)
- Sends a **loud** Telegram notification with instance name and public IP on success
- Responds to bot commands: `/logs` (last 10 lines), `/logfile` (today's log as file)

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
| `AVAILABILITY_DOMAIN` | e.g. `saWo:EU-ZURICH-1-AD-1` |
| `IMAGE_ID` | Leave empty — auto-detects latest Ubuntu 22.04 aarch64 |
| `SSH_PUBLIC_KEY_PATH` | Path to `.pub` key for the ARM instance |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your chat ID (get via Telegram `getUpdates`) |
| `POLL_INTERVAL` | Seconds between attempts (120 recommended) |

### 5. Run

```bash
# foreground
venv/bin/python main.py

# background (survives SSH disconnect)
nohup venv/bin/python main.py > hunter.log 2>&1 &

# check it's running
pgrep -f main.py

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

## Project structure

```
├── main.py                 # Polling loop + Telegram bot command listener
├── config.py               # Loads all config from .env
├── oci_client.py           # OCI API: launch instance, detect image, get public IP
├── notifier.py             # Telegram notifications (silent/loud)
├── oracle-hunter.service   # systemd unit file
├── .env.example            # Config template
└── requirements.txt
```
