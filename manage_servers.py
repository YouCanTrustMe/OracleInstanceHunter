#!/usr/bin/env python3
import os
import oci

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

ACCOUNTS = {
    "zurich": {
        "user":        "ocid1.user.oc1..aaaaaaaav424qqumee3vdy3pheqje63r42daes23rm5simgh6bpc2g2l55ma",
        "fingerprint": "1a:0a:8d:95:ad:4f:24:02:d2:3f:bd:35:c2:99:12:51",
        "tenancy":     "ocid1.tenancy.oc1..aaaaaaaaaktwmjy24lgcrkttpzrozhlaftzhnizfpxe6ul6kmpgbrn5mjv2a",
        "region":      "eu-zurich-1",
        "key_file":    os.path.join(SCRIPT_DIR, "oci_key_zurich.pem"),
    },
    "amsterdam": {
        "user":        "ocid1.user.oc1..aaaaaaaab3qjv6qwmnmfb4qrcfze5zwjnzznognnzejhgjdxbv33ma3hzzya",
        "fingerprint": "ed:0c:11:07:20:74:d4:28:a3:d7:81:8c:a2:6f:f0:6f",
        "tenancy":     "ocid1.tenancy.oc1..aaaaaaaa3rdeu6njw4jr3nh225sm26karitxfzvinqu5yecwdbvh3qg5zg6q",
        "region":      "eu-amsterdam-1",
        "key_file":    os.path.join(SCRIPT_DIR, "oci_key_amsterdam.pem"),
    },
}


def _oci_config(account):
    cfg = ACCOUNTS[account]
    return {
        "user":        cfg["user"],
        "fingerprint": cfg["fingerprint"],
        "tenancy":     cfg["tenancy"],
        "region":      cfg["region"],
        "key_file":    cfg["key_file"],
    }


def _get_public_ip(compute, network, instance_id, compartment_id):
    try:
        attachments = compute.list_vnic_attachments(
            compartment_id=compartment_id, instance_id=instance_id
        ).data
        if not attachments:
            return "—"
        vnic = network.get_vnic(attachments[0].vnic_id).data
        return vnic.public_ip or "—"
    except Exception:
        return "—"


def list_all_instances():
    rows = []
    for account_name in ACCOUNTS:
        cfg_dict = _oci_config(account_name)
        compartment_id = ACCOUNTS[account_name]["tenancy"]
        compute = oci.core.ComputeClient(cfg_dict)
        network = oci.core.VirtualNetworkClient(cfg_dict)

        instances = compute.list_instances(compartment_id=compartment_id).data
        for inst in instances:
            if inst.lifecycle_state == "TERMINATED":
                continue
            ip = _get_public_ip(compute, network, inst.id, compartment_id)
            rows.append({
                "account":    account_name,
                "id":         inst.id,
                "name":       inst.display_name,
                "shape":      inst.shape,
                "status":     inst.lifecycle_state,
                "ip":         ip,
                "oci_config": cfg_dict,
            })
    return rows


def reboot_instance(oci_config, instance_id, name):
    compute = oci.core.ComputeClient(oci_config)
    compute.instance_action(instance_id, "RESET")
    print(f"Reboot sent to {name}.")


def main():
    print("Fetching instances from all accounts...\n")
    instances = list_all_instances()

    if not instances:
        print("No active instances found.")
        return

    col = "{:<4} {:<12} {:<26} {:<28} {:<14} {}"
    print(col.format("#", "Account", "Name", "Shape", "Status", "IP"))
    print("-" * 95)
    for i, inst in enumerate(instances, 1):
        print(col.format(i, inst["account"], inst["name"], inst["shape"], inst["status"], inst["ip"]))

    print()
    choice = input("Enter # to reboot (or Enter to cancel): ").strip()
    if not choice:
        print("Cancelled.")
        return

    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(instances)):
            print("Invalid choice.")
            return
        inst = instances[idx]
        confirm = input(f"Reboot '{inst['name']}' ({inst['account']}, {inst['status']})? [y/N]: ").strip().lower()
        if confirm == "y":
            reboot_instance(inst["oci_config"], inst["id"], inst["name"])
        else:
            print("Cancelled.")
    except ValueError:
        print("Invalid input.")


if __name__ == "__main__":
    main()
