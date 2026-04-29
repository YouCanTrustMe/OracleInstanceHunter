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


def launch_instance() -> dict | None:
    oci_config = _build_oci_config()
    compute = oci.core.ComputeClient(oci_config)
    network = oci.core.VirtualNetworkClient(oci_config)

    ssh_key = _read_ssh_public_key()

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=config.AVAILABILITY_DOMAIN,
        compartment_id=config.COMPARTMENT_ID,
        display_name=config.INSTANCE_NAME,
        shape=config.SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=config.OCPUS,
            memory_in_gbs=config.MEMORY_GB,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=config.IMAGE_ID,
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

    return {
        "name": instance.display_name,
        "public_ip": vnic.public_ip,
        "region": config.OCI_REGION,
    }
