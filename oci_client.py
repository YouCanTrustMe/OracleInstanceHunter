import oci
import config


def _read_ssh_public_key() -> str:
    with open(config.SSH_PUBLIC_KEY_PATH, "r") as f:
        return f.read().strip()


def _build_oci_config() -> dict:
    return {
        "user": config.OCI_USER,
        "fingerprint": config.OCI_FINGERPRINT,
        "tenancy": config.OCI_TENANCY,
        "region": config.OCI_REGION,
        "key_file": config.OCI_KEY_FILE,
    }


def _find_ubuntu_arm_image(compute: oci.core.ComputeClient) -> str:
    images = compute.list_images(
        compartment_id=config.COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=config.SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise RuntimeError("No Ubuntu 22.04 image found for shape " + config.SHAPE)
    return images[0].id


def running_instance_names() -> set[str]:
    """Names of all instances currently RUNNING/PROVISIONING/STARTING."""
    oci_config = _build_oci_config()
    compute = oci.core.ComputeClient(oci_config)
    instances = compute.list_instances(compartment_id=config.COMPARTMENT_ID).data
    return {i.display_name for i in instances
            if i.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING")}


def find_existing_instance(instance_name: str = None) -> dict | None:
    instance_name = instance_name or config.INSTANCE_NAME
    oci_config = _build_oci_config()
    compute = oci.core.ComputeClient(oci_config)
    network = oci.core.VirtualNetworkClient(oci_config)

    instances = compute.list_instances(
        compartment_id=config.COMPARTMENT_ID,
        display_name=instance_name,
    ).data

    for inst in instances:
        if inst.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING"):
            vnic_attachments = compute.list_vnic_attachments(
                compartment_id=config.COMPARTMENT_ID,
                instance_id=inst.id,
            ).data
            public_ip = None
            if vnic_attachments:
                vnic = network.get_vnic(vnic_attachments[0].vnic_id).data
                public_ip = vnic.public_ip
            return {
                "name": inst.display_name,
                "public_ip": public_ip or "unknown",
                "region": config.OCI_REGION,
                "state": inst.lifecycle_state,
            }
    return None


def _wait_for_public_ip(compute, network, instance_id) -> str | None:
    """Best-effort: wait for RUNNING and fetch the public IP. Never raises — the
    instance is already created, so a failure here must not bubble up and make
    the caller retry (which would create a duplicate)."""
    try:
        oci.wait_until(
            compute,
            compute.get_instance(instance_id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=300,
        )
        vnic_attachments = compute.list_vnic_attachments(
            compartment_id=config.COMPARTMENT_ID,
            instance_id=instance_id,
        ).data
        if vnic_attachments:
            vnic = network.get_vnic(vnic_attachments[0].vnic_id).data
            return vnic.public_ip
    except Exception:
        pass
    return None


def launch_instance(instance_name: str = None) -> dict:
    instance_name = instance_name or config.INSTANCE_NAME
    oci_config = _build_oci_config()
    compute = oci.core.ComputeClient(oci_config)
    network = oci.core.VirtualNetworkClient(oci_config)

    ssh_key = _read_ssh_public_key()
    image_id = config.IMAGE_ID if config.IMAGE_ID else _find_ubuntu_arm_image(compute)

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=config.AVAILABILITY_DOMAIN,
        compartment_id=config.COMPARTMENT_ID,
        display_name=instance_name,
        shape=config.SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=config.OCPUS,
            memory_in_gbs=config.MEMORY_GB,
        ),
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

    response = compute.launch_instance(details)
    instance = response.data

    # Instance now exists — fetch its IP best-effort (never raises, so the caller
    # won't retry and create a duplicate). "pending" can be looked up later.
    public_ip = _wait_for_public_ip(compute, network, instance.id)

    return {
        "name": instance.display_name,
        "public_ip": public_ip or "pending",
        "region": config.OCI_REGION,
    }
