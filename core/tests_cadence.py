"""
How often a board talks to the server, and how a valve press still arrives
within seconds when it talks rarely.

One global upload interval (System page), an Offline window derived from it,
and the "somebody is at a valve screen" watch that turns the board's fast
command poll on and off.
"""

import json

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Barangay, Device, Household, LatestReading, Segment, SystemSetting, User,
    ValveState, offline_after_seconds,
)


class _Fixture(TestCase):
    def setUp(self):
        self.brgy = Barangay.objects.create(name="Poblacion")
        self.device = Device.objects.create(device_id="esp32-cad", label="cadence board")
        self.segment = Segment.objects.create(
            name="HH-700 line", barangay=self.brgy, device=self.device, module_index=1,
        )
        self.owner_user = User.objects.create_user(username="own7", password="pw", role=User.Role.OWNER)
        self.household = Household.objects.create(
            household_id="HH-700", owner_name="Cadence Owner", address="Purok 7",
            contact_number="09171234700", user=self.owner_user, segment=self.segment,
            barangay=self.brgy, language="en",
        )
        self.admin = User.objects.create_user(username="adm7", password="pw", role=User.Role.ADMIN)

    def _ingest(self, **extra):
        body = {"device_id": self.device.device_id, "readings": [{
            "module": 1, "pressure_bar": 1.0, "flow_lpm": 0.0, "status": "NORMAL",
            "valve_state": "open", "valve_reason": "", **extra,
        }]}
        return self.client.post(
            reverse("api_ingest"), json.dumps(body), content_type="application/json",
            HTTP_X_DEVICE_KEY=self.device.api_key,
        )


class UploadIntervalTests(_Fixture):
    def test_the_interval_rides_back_on_the_upload_reply(self):
        setting = SystemSetting.load()
        setting.upload_interval_seconds = 20
        setting.save()
        r = self._ingest()
        self.assertEqual(r.status_code, 200)
        config = r.json()["config"]["1"]
        self.assertEqual(config["upload_interval_seconds"], 20)
        self.assertEqual(config["command_poll_seconds"], 0)   # nobody at a valve screen

    def test_offline_window_is_three_missed_uploads_plus_slack(self):
        setting = SystemSetting.load()
        self.assertEqual(setting.upload_interval_seconds, 15)
        self.assertEqual(offline_after_seconds(), 50)
        setting.upload_interval_seconds = 30
        setting.save()                                          # resets the cache
        self.assertEqual(offline_after_seconds(), 95)

    def test_a_board_is_online_until_the_window_passes(self):
        self.device.last_seen = timezone.now() - timezone.timedelta(seconds=40)
        self.device.save(update_fields=["last_seen"])
        self.assertTrue(self.device.is_online)                  # 40 s < 50 s
        self.device.last_seen = timezone.now() - timezone.timedelta(seconds=60)
        self.device.save(update_fields=["last_seen"])
        self.assertFalse(self.device.is_online)

    def test_a_reading_older_than_the_window_reads_offline(self):
        self.segment.status = Segment.Status.NORMAL
        self.segment.save(update_fields=["status"])
        LatestReading.objects.create(
            segment=self.segment, pressure_bar=1.0, flow_lpm=0.0,
            device_status=Segment.Status.NORMAL, valve_state=ValveState.OPEN,
            updated_at=timezone.now() - timezone.timedelta(seconds=40),
        )
        self.client.force_login(self.owner_user)
        seg = self.client.get(reverse("api_live")).json()["segments"][0]
        self.assertEqual(seg["status"], "NORMAL")
        LatestReading.objects.filter(segment=self.segment).update(
            updated_at=timezone.now() - timezone.timedelta(seconds=60)
        )
        seg = self.client.get(reverse("api_live")).json()["segments"][0]
        self.assertEqual(seg["status"], "OFFLINE")

    def test_the_admin_sets_it_on_the_system_page(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("detection_settings"), {
            "flow_warn_minutes": 30, "flow_leak_minutes": 60, "valve_test_hour": 2,
            "valve_test_grace_minutes": 60, "valve_test_drop_bar": "0.15",
            "auto_close_on_flow_leak": "on", "upload_interval_seconds": 30,
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SystemSetting.load().upload_interval_seconds, 30)
        r = self.client.post(reverse("detection_settings"), {
            "flow_warn_minutes": 30, "flow_leak_minutes": 60, "valve_test_hour": 2,
            "valve_test_grace_minutes": 60, "valve_test_drop_bar": "0.15",
            "upload_interval_seconds": 1,                       # too fast
        })
        self.assertEqual(r.status_code, 200)                    # form re-shown
        self.assertEqual(SystemSetting.load().upload_interval_seconds, 30)


@override_settings(COMMAND_POLL_SECONDS=2, VALVE_WATCH_SECONDS=45)
class ValveWatchTests(_Fixture):
    def test_commands_needs_the_device_key(self):
        r = self.client.get(reverse("api_commands"))
        self.assertEqual(r.status_code, 401)
        r = self.client.get(reverse("api_commands"), HTTP_X_DEVICE_KEY="wrong")
        self.assertEqual(r.status_code, 401)

    def test_commands_carries_the_pending_command_and_nothing_heavy(self):
        self.segment.valve_command = Segment.ValveCommand.CLOSE
        self.segment.valve_test_requested = True
        self.segment.save(update_fields=["valve_command", "valve_test_requested"])
        r = self.client.get(reverse("api_commands"), HTTP_X_DEVICE_KEY=self.device.api_key)
        self.assertEqual(r.status_code, 200)
        config = r.json()["config"]["1"]
        self.assertEqual(config["valve_command"], "close")
        self.assertTrue(config["run_valve_test"])
        self.assertEqual(config["command_poll_seconds"], 0)
        self.assertNotIn("flow_warn_minutes", config)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.last_seen)                # a poll is not a reading

    def test_an_active_valve_screen_turns_the_fast_poll_on(self):
        self.client.force_login(self.owner_user)
        self.client.get(reverse("api_live"))                    # plain poll: not watching
        self.segment.refresh_from_db()
        self.assertIsNone(self.segment.valve_watch_until)

        self.client.get(reverse("api_live"), HTTP_X_VALVE_WATCH="1")
        self.segment.refresh_from_db()
        self.assertTrue(self.segment.valve_watched)
        remaining = (self.segment.valve_watch_until - timezone.now()).total_seconds()
        self.assertTrue(40 < remaining <= 45, remaining)

        self.client.logout()
        r = self.client.get(reverse("api_commands"), HTTP_X_DEVICE_KEY=self.device.api_key)
        self.assertEqual(r.json()["config"]["1"]["command_poll_seconds"], 2)
        r = self._ingest()
        self.assertEqual(r.json()["config"]["1"]["command_poll_seconds"], 2)

    def test_the_watch_lapses(self):
        self.segment.valve_watch_until = timezone.now() - timezone.timedelta(seconds=1)
        self.segment.save(update_fields=["valve_watch_until"])
        r = self.client.get(reverse("api_commands"), HTTP_X_DEVICE_KEY=self.device.api_key)
        self.assertEqual(r.json()["config"]["1"]["command_poll_seconds"], 0)

    def test_a_watch_is_refreshed_sparingly(self):
        self.client.force_login(self.owner_user)
        self.client.get(reverse("api_live"), HTTP_X_VALVE_WATCH="1")
        self.segment.refresh_from_db()
        first = self.segment.valve_watch_until
        self.client.get(reverse("api_live"), HTTP_X_VALVE_WATCH="1")  # 2 s later in life
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.valve_watch_until, first)   # not rewritten
        Segment.objects.filter(pk=self.segment.pk).update(
            valve_watch_until=timezone.now() + timezone.timedelta(seconds=10)
        )
        self.client.get(reverse("api_live"), HTTP_X_VALVE_WATCH="1")
        self.segment.refresh_from_db()
        self.assertGreater(self.segment.valve_watch_until, first)  # about to lapse: pushed out

    def test_a_facilitator_watch_covers_only_their_barangay(self):
        other = Segment.objects.create(name="elsewhere", barangay=Barangay.objects.create(name="Far"))
        faci = User.objects.create_user(
            username="faci7", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        self.client.force_login(faci)
        self.client.get(reverse("api_live"), HTTP_X_VALVE_WATCH="1")
        self.segment.refresh_from_db(); other.refresh_from_db()
        self.assertTrue(self.segment.valve_watched)
        self.assertIsNone(other.valve_watch_until)
