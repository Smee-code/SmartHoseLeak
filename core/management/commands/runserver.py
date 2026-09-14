"""
Wraps Django's runserver so it prints the addresses you actually need.

The stock command prints "Starting development server at http://0.0.0.0:8000/",
which is not an address anything can connect to. The ESP32 needs this machine's
address on the network, and picking it by hand is error-prone: a Windows laptop
usually has several, including dormant hotspot adapters that look plausible and
silently do not work.

This finds the interface that actually carries traffic and prints the two lines
worth copying - the dashboard URL, and the Server address for the device.

Subclasses the staticfiles runserver, not the plain one, so static files are
still served in DEBUG exactly as before.
"""

import socket

from django.contrib.staticfiles.management.commands.runserver import (
    Command as StaticfilesRunserverCommand,
)

from core import discovery


def primary_lan_ip():
    """
    This machine's address on the network it actually routes through.

    Opening a UDP socket towards a public address sends no packets, but it
    makes the OS choose an interface - the one with the default gateway. That
    is exactly the interface a device on the same WiFi can reach, and it
    sidesteps having to guess among several local addresses.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


class Command(StaticfilesRunserverCommand):
    def inner_run(self, *args, **options):
        self._print_addresses()
        self._start_discovery()
        super().inner_run(*args, **options)

    def _start_discovery(self):
        """
        Answer devices asking where the server is.

        Only when bound to all interfaces: on localhost nothing else can
        reach us anyway, and advertising an address no device can use would
        be worse than staying quiet.
        """
        if self.addr not in ("0.0.0.0", "::", ""):
            return

        if discovery.start(self.port):
            self.stdout.write(
                "  Devices can find this server by themselves "
                f"(UDP {discovery.DISCOVERY_PORT})."
            )
            self.stdout.write("")

    def _print_addresses(self):
        port = self.port
        bound_everywhere = self.addr in ("0.0.0.0", "::", "")
        ip = primary_lan_ip()

        write = self.stdout.write
        write("")
        write(self.style.MIGRATE_HEADING("Addresses for this session"))

        if not bound_everywhere:
            write(
                self.style.WARNING(
                    f"  Bound to {self.addr} only - reachable from this computer, "
                    "nothing else."
                )
            )
            write(
                "  The ESP32 cannot reach the server this way. Stop it and run:\n"
                "      python manage.py runserver 0.0.0.0:%s" % port
            )
            write("")
            return

        write(f"  On this computer   http://127.0.0.1:{port}/dashboard/")

        if ip:
            write(self.style.SUCCESS(f"  On the network     http://{ip}:{port}/dashboard/"))
            write("")
            write("  ESP32 Server address (paste into the device's /wifi page):")
            write(self.style.SUCCESS(f"      http://{ip}:{port}/api/ingest/"))
        else:
            write(
                self.style.WARNING(
                    "  Could not determine this machine's network address - "
                    "no network connection? Run ipconfig and use the IPv4 Address "
                    "that lists a Default Gateway."
                )
            )
        write("")
