"""
Interval probe — find how often LaunchInstance can be called before OCI starts
answering 429 TooManyRequests. Runs from the Mac against one account, stepping
through intervals, and prints one line per call plus a per-step summary.

It makes REAL launch attempts under the hunter's next slot name, so it can win:
on success it stops and prints WIN — then just start the server's hunter again,
it finds the instance and announces it (Telegram + keys + email).

    !! Stop that account's hunter first (sudo systemctl stop oracle-hunter),
    !! otherwise both launch the same slot name and could create a duplicate.

    python tools/interval_probe.py frankfurt fra-arm ~/fra_arm_key.pub 90:10,60:10,30:10
      account   = key in accounts.json
      slot name = the hunter's next free slot (INSTANCE_NAME, or INSTANCE_NAME-2 ...)
      pub key   = the hunter server's ~/.ssh/oracle_arm_key.pub (so its SSH key works)
      schedule  = interval_seconds:minutes,...  (each cycle tries every AD, 2 s apart)
"""

import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oci
from accounts_config import load_accounts

OCPUS, MEMORY_GB = 1, 6
SHAPE = "VM.Standard.A1.Flex"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def running_names(compute, compartment) -> set:
    return {i.display_name for i in compute.list_instances(compartment_id=compartment).data
            if i.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING")}


def main() -> None:
    account, slot, pub_path, schedule = sys.argv[1:5]
    cfg = load_accounts()[account]
    tenancy = cfg["tenancy"]
    compartment = cfg.get("compartment_id", tenancy)  # same compartment as the hunter's COMPARTMENT_ID
    compute = oci.core.ComputeClient(cfg)
    ads = [ad.name for ad in oci.identity.IdentityClient(cfg).list_availability_domains(tenancy).data]
    ssh_key = open(os.path.expanduser(pub_path)).read().strip()
    image = compute.list_images(compartment_id=compartment, operating_system="Canonical Ubuntu",
                                operating_system_version="22.04", shape=SHAPE,
                                sort_by="TIMECREATED", sort_order="DESC").data[0].id
    if slot in running_names(compute, compartment):
        sys.exit(f"{slot} already exists — pick the next free slot name")
    steps = [tuple(int(x) for x in s.split(":")) for s in schedule.split(",")]
    log(f"{account}: ADs={len(ads)} slot={slot} steps={steps}")

    totals = []
    for interval, minutes in steps:
        stats = Counter()
        end = time.time() + minutes * 60
        log(f"=== step: every {interval}s for {minutes} min ===")
        while time.time() < end:
            cycle_start = time.time()
            for i, ad in enumerate(ads):
                if i:
                    time.sleep(2)
                details = oci.core.models.LaunchInstanceDetails(
                    availability_domain=ad, compartment_id=compartment, display_name=slot, shape=SHAPE,
                    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
                    source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image, source_type="image"),
                    create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=cfg["subnet_id"], assign_public_ip=True),
                    metadata={"ssh_authorized_keys": ssh_key})
                try:
                    inst = compute.launch_instance(details).data
                    log(f"WIN {inst.display_name} in {ad} — start the server hunter now, it will announce it")
                    return
                except oci.exceptions.ServiceError as e:
                    kind = "capacity" if "Out of host capacity" in str(e.message) else f"{e.status} {e.code}"
                    stats[kind] += 1
                    extra = ""
                    if e.status == 429:
                        retry_after = (e.headers or {}).get("retry-after")
                        extra = f" (retry-after={retry_after})" if retry_after else ""
                    log(f"AD-{ad[-1]} {kind}{extra} · step so far {dict(stats)}")
                    if kind != "capacity":
                        if slot in running_names(compute, compartment):  # dup-safety, like the hunter
                            log(f"WIN {slot} exists after an error — start the server hunter now")
                            return
                        if e.status != 429:
                            log(f"stopping on unexpected error: {e.message}")
                            return
                        break  # rest of the ADs this cycle would just hit the same limit
                except Exception as e:
                    stats[type(e).__name__] += 1
                    log(f"network: {e}")
                    # the request may have reached OCI and created the VM before the reply was lost
                    try:
                        if slot in running_names(compute, compartment):
                            log(f"WIN {slot} exists after a network error — start the server hunter now")
                            return
                    except Exception as list_error:
                        log(f"stopping: can't re-check instances after a network error ({list_error})")
                        return
                    break
            time.sleep(max(0.0, interval - (time.time() - cycle_start)))
        calls = sum(stats.values())
        rate = 100 * stats.get("429 TooManyRequests", 0) / calls if calls else 0
        log(f"=== step {interval}s: {calls} calls, {dict(stats)}, 429 = {rate:.0f}% ===")
        totals.append((interval, calls, dict(stats), rate))

    log("=== SUMMARY ===")
    for interval, calls, stats, rate in totals:
        log(f"every {interval:>3}s: {calls:>4} calls · 429 {rate:3.0f}% · {stats}")


if __name__ == "__main__":
    main()
