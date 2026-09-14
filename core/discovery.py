"""
Answers "where is the server?" for devices on the same network.

The ESP32 has to be told an IP address to post readings to, and this laptop's
address changes whenever it joins a different network - four times in one day,
at which point the board keeps posting into the void until somebody notices.

So the board asks instead. It broadcasts a short probe on the local network,
this replies with the address it should use, and the board remembers it. When
the laptop's address changes the uploads start failing, the board asks again,
and it heals itself.

Deliberately tiny: one UDP socket, a fixed question and a JSON answer. It
carries no data and accepts no commands, so the worst a stranger on the same
WiFi can do is learn the dashboard's address - which they could find by
scanning the network anyway.
"""

import json
import logging
import socket
import threading

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 18830
PROBE = b"SHL-DISCOVER?"
MAX_DATAGRAM = 256


def address_facing(peer_ip):
    """
    This machine's address on the interface that can reach peer_ip.

    A laptop usually has several - WiFi, a dormant hotspot adapter, maybe
    Bluetooth - and only the one on the device's own network is any use.
    Connecting a UDP socket sends nothing but makes the OS pick that one.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer_ip, 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _serve(http_port, sock):
    while True:
        try:
            data, sender = sock.recvfrom(MAX_DATAGRAM)
        except OSError:
            return  # socket closed on shutdown

        if PROBE not in data:
            continue

        local_ip = address_facing(sender[0])
        if not local_ip:
            continue

        answer = json.dumps(
            {"ingest": f"http://{local_ip}:{http_port}/api/ingest/"}
        ).encode()
        try:
            sock.sendto(answer, sender)
            logger.info("Discovery: told %s to post to %s:%s", sender[0], local_ip, http_port)
        except OSError:
            pass


def start(http_port):
    """
    Begin answering probes, in a daemon thread so it dies with the server.

    Returns the thread, or None if the port was unavailable - discovery is a
    convenience, and failing to start it must never stop the site serving.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        logger.warning("Discovery listener not started on %s: %s", DISCOVERY_PORT, exc)
        return None

    thread = threading.Thread(
        target=_serve, args=(http_port, sock), name="shl-discovery", daemon=True
    )
    thread.start()
    return thread
