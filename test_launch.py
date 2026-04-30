"""
One-shot test: launch VM.Standard.E2.1.Micro (AMD x86_64), send Telegram notification,
print instance OCID so you can delete it afterwards in OCI Console.

Run: python test_launch.py
Delete: OCI Console → Compute → Instances → test-amd-delete-me → Terminate
"""

import oci
import config
import notifier


TEST_SHAPE = "VM.Standard.E2.1.Micro"
TEST_NAME = "test-amd-delete-me"


def _build_oci_config() -> dict:
    return {
        "user": config.OCI_USER,
        "fingerprint": config.OCI_FINGERPRINT,
        "tenancy": config.OCI_TENANCY,
        "region": config.OCI_REGION,
        "key_file": config.OCI_KEY_FILE,
    }


def _find_ubuntu_amd_image(compute: oci.core.ComputeClient) -> str:
    images = compute.list_images(
        compartment_id=config.COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=TEST_SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise RuntimeError(f"No Ubuntu 22.04 image found for shape {TEST_SHAPE}")
    return images[0].id


def main() -> None:
    with open(config.SSH_PUBLIC_KEY_PATH) as f:
        ssh_key = f.read().strip()

    oci_config = _build_oci_config()
    compute = oci.core.ComputeClient(oci_config)
    network = oci.core.VirtualNetworkClient(oci_config)

    print(f"Finding Ubuntu 22.04 image for {TEST_SHAPE}...")
    image_id = _find_ubuntu_amd_image(compute)
    print(f"Image: {image_id}")

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=config.AVAILABILITY_DOMAIN,
        compartment_id=config.COMPARTMENT_ID,
        display_name=TEST_NAME,
        shape=TEST_SHAPE,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            source_type="image",
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=config.SUBNET_ID,
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )

    print("Launching instance...")
    instance = compute.launch_instance(details).data
    print(f"Instance ID: {instance.id}")
    print("Waiting for RUNNING state (up to 5 min)...")

    oci.wait_until(
        compute,
        compute.get_instance(instance.id),
        "lifecycle_state",
        "RUNNING",
        max_wait_seconds=300,
    )

    vnic_attachments = compute.list_vnic_attachments(
        compartment_id=config.COMPARTMENT_ID,
        instance_id=instance.id,
    ).data
    vnic = network.get_vnic(vnic_attachments[0].vnic_id).data
    public_ip = vnic.public_ip

    print(f"\nInstance is RUNNING!")
    print(f"Name: {TEST_NAME}")
    print(f"Public IP: {public_ip}")
    print(f"SSH: ssh -i ~/.ssh/oracle_arm_key ubuntu@{public_ip}")
    print(f"\nTo delete: OCI Console → Compute → Instances → {TEST_NAME} → Terminate")

    notifier.notify_success(TEST_NAME, public_ip, config.OCI_REGION)
    print("Telegram notification sent.")


if __name__ == "__main__":
    main()
