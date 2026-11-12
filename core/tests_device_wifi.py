"""
The owner setting up or moving their board from the portal.

New board: the portal page builds a link to the board's own page with the
WiFi, server and code filled in (the WiFi never reaches the server). Online
board: a WiFi change is parked on the board's record, handed over on the
upload reply, and the board's report - by change id - clears it and tells
the household how it went.
"""

import json

from django.test import TestCase
from django.urls import reverse

from core.models import Barangay, Device, Household, Notification, Segment, User


class _Portal(TestCase):
    def setUp(self):
        self.brgy = Barangay.objects.create(name="Poblacion")
        self.device = Device.objects.create(device_id="esp32-w", label="w")
        self.segment = Segment.objects.create(name="HH-500 line", barangay=self.brgy, device=self.device, module_index=1)
        self.owner = User.objects.create_user(username="own5", password="pw", role=User.Role.OWNER)
        self.household = Household.objects.create(
            household_id="HH-500", owner_name="Owner Five", address="Purok 5",
            contact_number="09171234505", user=self.owner, segment=self.segment,
            barangay=self.brgy, language="en",
        )
        self.client.force_login(self.owner)

    def _ingest(self, **top):
        body = {"device_id": self.device.device_id, "readings": [{
            "module": 1, "pressure_bar": 1.0, "flow_lpm": 0.0, "status": "NORMAL",
            "valve_state": "open", "valve_reason": "",
        }], **top}
        return self.client.post(
            reverse("api_ingest"), json.dumps(body), content_type="application/json",
            HTTP_X_DEVICE_KEY=self.device.api_key,
        )


class GuidedSetupTests(_Portal):
    def test_the_settings_page_carries_the_link_builder(self):
        r = self.client.get(reverse("owner_settings"))
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn("data-setup-builder", page)
        self.assertIn('data-setup-server=""', page)          # plain http: the board finds a LAN server itself
        self.assertIn("http://192.168.4.1/", page)

    def test_a_hosted_site_gives_its_address(self):
        r = self.client.get(reverse("owner_settings"), secure=True)
        self.assertIn('data-setup-server="https://testserver"', r.content.decode())

    def test_the_wifi_block_waits_for_a_first_contact(self):
        r = self.client.get(reverse("owner_settings"))
        self.assertIn('data-device="wifi" hidden', r.content.decode())
        r = self.client.post(reverse("owner_device_wifi"), {"ssid": "HomeNet", "password": "secret123"})
        self.assertEqual(r.status_code, 302)
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_ssid, "")                # refused: never connected


class WifiChangeTests(_Portal):
    def setUp(self):
        super().setUp()
        self._ingest(wifi_ssid="OldNet")                                   # the board is online
        self.device.refresh_from_db()

    def test_the_board_reports_its_network(self):
        self.assertEqual(self.device.wifi_ssid, "OldNet")
        state = self.client.get(reverse("owner_device_status")).json()
        self.assertTrue(state["can_change_wifi"])
        self.assertEqual(state["wifi_ssid"], "OldNet")

    def test_a_change_rides_on_the_reply_until_reported(self):
        r = self.client.post(reverse("owner_device_wifi"), {"ssid": "NewNet", "password": "secret123"})
        self.assertRedirects(r, reverse("owner_settings"))
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_ssid, "NewNet")
        change_id = self.device.pending_wifi_id
        self.assertTrue(change_id)

        config = self._ingest(wifi_ssid="OldNet").json()["config"]["1"]
        self.assertEqual(config["wifi_ssid"], "NewNet")
        self.assertEqual(config["wifi_password"], "secret123")
        self.assertEqual(config["wifi_change"], change_id)

        state = self.client.get(reverse("owner_device_status")).json()
        self.assertEqual(state["wifi_pending"], "NewNet")

        # Joined: the password is gone, the result and a notice are there.
        config = self._ingest(wifi_ssid="NewNet", wifi_change=change_id, wifi_ok=True).json()["config"]["1"]
        self.assertNotIn("wifi_change", config)
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_password, "")
        self.assertEqual(self.device.wifi_ssid, "NewNet")
        self.assertIn("Connected to NewNet", self.device.wifi_result)
        self.assertTrue(Notification.objects.filter(recipient=self.household, title__icontains="new WiFi").exists())

    def test_a_failed_change_says_so(self):
        self.client.post(reverse("owner_device_wifi"), {"ssid": "Typo", "password": "secret123"})
        self.device.refresh_from_db()
        change_id = self.device.pending_wifi_id
        self._ingest(wifi_ssid="OldNet", wifi_change=change_id, wifi_ok=False)
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_id, "")
        self.assertIn("Could not join Typo", self.device.wifi_result)
        self.assertIn("still on OldNet", self.device.wifi_result)
        self.assertTrue(Notification.objects.filter(recipient=self.household, title__icontains="could not join").exists())

    def test_a_stale_or_wrong_id_is_ignored(self):
        self.client.post(reverse("owner_device_wifi"), {"ssid": "NewNet", "password": "secret123"})
        self._ingest(wifi_ssid="OldNet", wifi_change="deadbeef", wifi_ok=True)
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_ssid, "NewNet")           # still pending

    def test_bad_input_is_refused(self):
        self.client.post(reverse("owner_device_wifi"), {"ssid": "", "password": "secret123"})
        self.client.post(reverse("owner_device_wifi"), {"ssid": "x" * 33, "password": "secret123"})
        self.client.post(reverse("owner_device_wifi"), {"ssid": "Net", "password": "short"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_ssid, "")
        self.client.post(reverse("owner_device_wifi"), {"ssid": "OpenNet", "password": ""})
        self.device.refresh_from_db()
        self.assertEqual(self.device.pending_wifi_ssid, "OpenNet")        # open network allowed
