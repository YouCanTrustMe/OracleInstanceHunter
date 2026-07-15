#!/usr/bin/env python3
import os
import sys
import subprocess
import requests
import oci
from dotenv import load_dotenv

from accounts_config import load_accounts

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "local.env"))

ACCOUNTS = load_accounts()
for _name, _cfg in ACCOUNTS.items():
    _cfg["telegram_token"] = os.environ.get(f"TELEGRAM_TOKEN_{_name.upper()}", "")
    _cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "")


def _send_telegram(token, chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def _send_telegram_file(token, chat_id, path, caption=""):
    with open(path, "rb") as f:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": f},
            timeout=30,
        )


def _find_ubuntu_amd64_image(compute, compartment_id):
    images = compute.list_images(
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape="VM.Standard.E2.1.Micro",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise RuntimeError("Ubuntu 22.04 image for VM.Standard.E2.1.Micro not found")
    return images[0].id


def _generate_ssh_key(instance_name):
    key_path = os.path.join(SCRIPT_DIR, f"ssh_{instance_name}_key")
    for path in (key_path, key_path + ".pub"):
        if os.path.exists(path):
            os.remove(path)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", ""],
        check=True, capture_output=True,
    )
    with open(key_path + ".pub") as f:
        pub_key = f.read().strip()
    return key_path, pub_key


def create_instance(account_name):
    cfg = ACCOUNTS[account_name]
    oci_config = {
        "user":        cfg["user"],
        "fingerprint": cfg["fingerprint"],
        "tenancy":     cfg["tenancy"],
        "region":      cfg["region"],
        "key_file":    cfg["key_file"],
    }
    compartment_id = cfg["tenancy"]
    compute = oci.core.ComputeClient(oci_config)
    network = oci.core.VirtualNetworkClient(oci_config)

    print(f"Generating SSH key for {cfg['instance_name']}...")
    key_path, pub_key = _generate_ssh_key(cfg["instance_name"])

    print("Finding Ubuntu 22.04 AMD64 image...")
    image_id = _find_ubuntu_amd64_image(compute, compartment_id)
    print(f"Image: {image_id}")

    print(f"Launching {cfg['instance_name']} (VM.Standard.E2.1.Micro)...")
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=cfg["availability_domain"],
        compartment_id=compartment_id,
        display_name=cfg["instance_name"],
        shape="VM.Standard.E2.1.Micro",
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            source_type="image",
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=cfg["subnet_id"],
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": pub_key},
    )
    resp = compute.launch_instance(details)
    instance_id = resp.data.id
    print(f"Instance ID: {instance_id}")

    print("Waiting for RUNNING state...")
    oci.wait_until(
        compute,
        compute.get_instance(instance_id),
        "lifecycle_state",
        "RUNNING",
        max_wait_seconds=300,
    )
    print("RUNNING.")

    print("Getting public IP...")
    attachments = compute.list_vnic_attachments(
        compartment_id=compartment_id, instance_id=instance_id
    ).data
    vnic = network.get_vnic(attachments[0].vnic_id).data
    public_ip = vnic.public_ip

    ssh_cmd = f"ssh -i ssh_{cfg['instance_name']}_key ubuntu@{public_ip}"
    msg = (
        f"✅ {cfg['instance_name']} created\n"
        f"IP: {public_ip}\n"
        f"Region: {cfg['region']}\n"
        f"Shape: VM.Standard.E2.1.Micro\n"
        f"SSH: {ssh_cmd}"
    )
    _send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg)
    _send_telegram_file(cfg["telegram_token"], cfg["telegram_chat_id"],
                        key_path, caption=f"Private key — {cfg['instance_name']}")
    _send_telegram_file(cfg["telegram_token"], cfg["telegram_chat_id"],
                        key_path + ".pub", caption=f"Public key — {cfg['instance_name']}")
    print(f"Done. IP: {public_ip}")
    print(f"Keys saved: {key_path} / {key_path}.pub")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ACCOUNTS:
        print(f"Usage: python create_e2_micro.py [{'|'.join(ACCOUNTS)}]")
        sys.exit(1)
    create_instance(sys.argv[1])


if __name__ == "__main__":
    main()
