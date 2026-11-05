"""Peer watchdog: detect a hung/offline server from the outside.

A frozen server can't report itself, so each hunter watches the other one.
Check = open TCP to the peer's SSH port and wait for the "SSH-" banner. This
catches a fully frozen box (the 2026-11-05 Zurich hang accepted TCP but never
sent the banner), a powered-off box and a network outage.
"""
import logging
import socket
import threading

import config
import notifier

logger = logging.getLogger(__name__)

CHECK_EVERY = 120   # seconds between checks
ALERT_AFTER = 5     # consecutive failures before alerting (~10 min)


def check_peer(host: str, port: int, timeout: float = 15) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def _alert(subject: str, text: str) -> None:
    notifier.send_message(text)
    notifier.send_email(subject, text)


def _loop(stop_event: threading.Event) -> None:
    name, host, port = config.PEER_NAME, config.PEER_HOST, config.PEER_PORT
    fails, alerted = 0, False
    while not stop_event.wait(CHECK_EVERY):
        if check_peer(host, port):
            if alerted:
                logger.info("Peer %s is back", name)
                _alert(f"[OracleHunter] {name} is back",
                       f"✅ {name} ({host}) is responding again.")
            fails, alerted = 0, False
            continue
        fails += 1
        logger.warning("Peer %s (%s) not responding (%d in a row)", name, host, fails)
        if fails == ALERT_AFTER:
            alerted = True
            _alert(f"[OracleHunter] {name} is DOWN",
                   f"🔴 {name} ({host}) has not responded for ~{ALERT_AFTER * CHECK_EVERY // 60} min.\n"
                   f"It may be frozen. Reboot: run tools/manage_servers.py on the Mac and pick {name}.")


def start(stop_event: threading.Event) -> None:
    if not config.PEER_HOST:
        return
    logger.info("Peer watchdog: watching %s (%s:%d)", config.PEER_NAME, config.PEER_HOST, config.PEER_PORT)
    threading.Thread(target=_loop, args=(stop_event,), daemon=True).start()
