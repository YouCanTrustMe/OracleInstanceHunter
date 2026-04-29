import time
import logging
import oci.exceptions

import config
import oci_client
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("hunter.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

OUT_OF_CAPACITY_CODE = "InternalError"
OUT_OF_CAPACITY_MSG = "Out of host capacity"


def is_out_of_capacity(error: oci.exceptions.ServiceError) -> bool:
    return OUT_OF_CAPACITY_MSG in str(error.message)


def run() -> None:
    logger.info("Starting OracleInstanceHunter. Polling every %d seconds.", config.POLL_INTERVAL)

    while True:
        try:
            result = oci_client.launch_instance()
            logger.info("Instance created: %s | IP: %s", result["name"], result["public_ip"])
            notifier.notify_success(result["name"], result["public_ip"], result["region"])
            break

        except oci.exceptions.ServiceError as e:
            if is_out_of_capacity(e):
                logger.info("Out of capacity. Retrying in %d seconds...", config.POLL_INTERVAL)
            else:
                logger.error("OCI service error: %s", e)

        except Exception as e:
            logger.error("Unexpected error: %s", e)

        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    run()
