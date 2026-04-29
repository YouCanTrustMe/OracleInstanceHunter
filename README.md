# OracleInstanceHunter

Polls OCI API every 30 seconds and automatically creates a `VM.Standard.A1.Flex` ARM instance as soon as capacity becomes available. Sends a Telegram notification with the instance details on success.

## Requirements

- Python 3.10+
- An existing OCI AMD E2.1.Micro instance (Ubuntu 22.04) to run the script on
- OCI API key configured
- Telegram bot token and chat ID

## Setup

### 1. Generate SSH key pair (for the new ARM instance)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/oracle_arm_key -N ""
```

This creates:
- `~/.ssh/oracle_arm_key` — private key (use this to SSH into the ARM instance later)
- `~/.ssh/oracle_arm_key.pub` — public key (passed to OCI API on instance creation)

### 2. Configure OCI API access

Place your OCI API private key at `~/.oci/oci_api_key.pem` and ensure permissions are correct:

```bash
chmod 600 ~/.oci/oci_api_key.pem
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in all values. To find your `IMAGE_ID` for Ubuntu 22.04 in your region, go to OCI Console → Compute → Images → Platform Images.

### 5. Run

```bash
python main.py
```

Logs are written to `hunter.log` and also printed to stdout.

---

## Run as a systemd service

Create the service file:

```bash
sudo nano /etc/systemd/system/oracle-hunter.service
```

```ini
[Unit]
Description=OracleInstanceHunter
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/OracleInstanceHunter
ExecStart=/usr/bin/python3 /home/ubuntu/OracleInstanceHunter/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable oracle-hunter
sudo systemctl start oracle-hunter
sudo systemctl status oracle-hunter
```

View live logs:

```bash
journalctl -u oracle-hunter -f
```

---

## Project structure

```
├── main.py          # Entry point, polling loop
├── config.py        # Loads all configuration from .env
├── oci_client.py    # OCI API: launch instance, get public IP
├── notifier.py      # Telegram notification on success
├── .env.example     # Configuration template
└── requirements.txt
```
