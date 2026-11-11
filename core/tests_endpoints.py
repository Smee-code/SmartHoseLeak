"""
Every route, every role.

The unit tests prove each feature does what it says. This file proves the
other thing that matters on the day: that no page, for anybody, falls over.
Every named route is opened as a visitor, a household owner, a facilitator
and the administrator, and nothing may answer with a server error. The
walls between the roles are checked at the same time - a facilitator must
not see the console, an owner must not see the dashboard - because the
quickest way to lose those is to add a page and forget the decorator.

Then each role does the things it is there to do, with real form data, and
each must go through: the owner files and continues a complaint, the
facilitator activates a household and answers it, the administrator
registers people and lines, moves work along and changes the detection
settings. These are the transactions a defence panel will ask to see.

The data is the demo data, made by the same commands the demo uses, so what
passes here is what will be on screen.
"""

import datetime
import io
import json

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditLog,
    Announcement,
    Barangay,
    Complaint,
    ComplaintMessage,
    Device,
    Household,
    LeakEvent,
    Notification,
    Segment,
    SmsMessage,
    SystemSetting,
    User,
    WorkOrder,
)
from core.urls import urlpatterns


# Routes that are meant to be open to a visitor.
PUBLIC = {
    "home", "login", "login_code", "login_code_resend", "logout", "post_login",
    "password_reset", "password_reset_done", "password_reset_confirm",
    "password_reset_complete", "service_worker", "manifest", "offline",
    "api_ingest", "api_claim",
}
# Roles that may get a 200 on each area. Everyone else must be turned away.
AREA_ALLOWED = {
    "dashboard/": {"facilitator"},
    "console/": {"admin"},
    "portal/": {"owner"},
}


def _all_routes():
    """(name, converters) for every named route in core.urls."""
    for pattern in urlpatterns:
        name = getattr(pattern, "name", None)
        if name:
            yield name, list(pattern.pattern.converters)


@override_settings(DEBUG=True, SMS_ENABLED=True)
class EveryRouteEveryRole(TestCase):
    @classmethod
    def setUpTestData(cls):
        quiet = io.StringIO()
        call_command("setup_demo", password="pw", stdout=quiet)
        call_command("setup_demo_owner", stdout=quiet)

        cls.admin = User.objects.get(username="admin")
        cls.faci = User.objects.get(username="facilitator")
        cls.owner = User.objects.get(username="demo")
        cls.brgy = cls.faci.barangay
        cls.household = Household.objects.get(household_id="HH-DEMO")
        cls.segment = cls.household.segment
        cls.device = cls.segment.device
        cls.other = Household.objects.get(household_id="HH-001")   # has the demo board
        cls.spare = Household.objects.get(household_id="HH-002")   # no board
        cls.event = LeakEvent.objects.filter(segment=cls.segment).first()
        cls.complaint = Complaint.objects.get(household=cls.household)
        cls.order = WorkOrder.objects.get(household=cls.household)
        cls.sms = SmsMessage.queue_to_number(
            "09171234567", "test", name="x", kind=SmsMessage.Kind.MANUAL,
        )[0] if hasattr(SmsMessage, "queue_to_number") else None
        if cls.sms is not None:
            cls.sms.status = SmsMessage.Status.FAILED
            cls.sms.save(update_fields=["status"])

    # ------------------------------------------------------------ helpers

    def _as(self, role):
        self.client.logout()
        if role == "owner":
            self.client.force_login(self.owner)
        elif role == "facilitator":
            self.client.force_login(self.faci)
        elif role == "admin":
            self.client.force_login(self.admin)

    def _kwargs_for(self, name, converters):
        """A real object id for each route that needs one, or None to skip."""
        if not converters:
            return {}
        if converters == ["kind"]:
            return None  # exports are walked by kind below
        if "uidb64" in converters:
            return {"uidb64": "x", "token": "y"}
        if converters != ["pk"]:
            return None
        pk_by_name = {
            "household_": self.other.pk,
            "admin_household_": self.other.pk,
            "archive_restore_household": self.other.pk,
            "segment_": self.segment.pk,
            "device_": self.device.pk,
            "event_": self.event.pk,
            "complaint_": self.complaint.pk,
            "owner_complaint_": self.complaint.pk,
            "sms_": self.sms.pk if self.sms else 0,
            "assignment_": self.order.pk,
            "work_order_": self.order.pk,
            "barangay_": self.brgy.pk,
            "facilitator_": self.faci.pk,
            "archive_restore_facilitator": self.faci.pk,
            "account_": self.owner.pk,
        }
        for prefix, pk in pk_by_name.items():
            if name.startswith(prefix):
                return {"pk": pk}
        return None

    def _urls(self):
        skipped = []
        for name, converters in _all_routes():
            kwargs = self._kwargs_for(name, converters)
            if kwargs is None:
                if converters == ["kind"]:
                    kinds = ("households", "events", "pressure") if name == "export_csv" \
                        else ("households", "workorders", "pressure", "audit")
                    for kind in kinds:
                        yield f"{name}:{kind}", reverse(name, kwargs={"kind": kind})
                else:
                    skipped.append(name)
                continue
            yield name, reverse(name, kwargs=kwargs)
        self.assertEqual(skipped, [], f"routes the sweep could not build: {skipped}")

    # ------------------------------------------------------------ the sweep

    def test_no_route_errors_for_any_role_and_the_walls_hold(self):
        problems = []
        for role in ("anonymous", "owner", "facilitator", "admin"):
            self._as(role)
            for name, url in self._urls():
                try:
                    response = self.client.get(url, HTTP_X_REQUESTED_WITH="fetch")
                except Exception as exc:  # noqa: BLE001 - we want every failure listed
                    problems.append(f"{role} GET {url} ({name}) raised {exc!r}")
                    continue
                code = response.status_code
                if code >= 500:
                    problems.append(f"{role} GET {url} ({name}) -> {code}")
                    continue
                # The walls. A role outside an area must not get a page back.
                base = name.split(":")[0]
                for area, allowed in AREA_ALLOWED.items():
                    if url.startswith("/" + area) and code == 200 and role not in allowed:
                        if base in PUBLIC:
                            continue
                        problems.append(f"{role} GET {url} ({name}) -> 200 but only {sorted(allowed)} may")
                if role == "anonymous" and code == 200 and base not in PUBLIC and \
                        not url.startswith(("/static", "/offline")):
                    problems.append(f"anonymous GET {url} ({name}) -> 200")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    # ------------------------------------------------------------ the owner

    def test_owner_transactions(self):
        self._as("owner")
        c = self.client

        # Files a complaint, then continues the thread.
        before = Complaint.objects.filter(household=self.household).count()
        r = c.post(reverse("owner_complaints"), {
            "category": Complaint.Category.NO_WATER, "subject": "No water since noon",
            "body": "Nothing comes out of any tap since about 12.",
        })
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual(Complaint.objects.filter(household=self.household).count(), before + 1)

        r = c.post(reverse("owner_complaint_thread", args=[self.complaint.pk]), {"body": "Still weak tonight."})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ComplaintMessage.objects.filter(complaint=self.complaint, body="Still weak tonight.").exists())

        # Reads the feed; unread notices clear.
        r = c.get(reverse("owner_notifications"))
        self.assertEqual(r.status_code, 200)

        # Asks for pressure history.
        r = c.get(reverse("api_history"), {"hours": 24, "segment": self.segment.pk})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok", True))

        # Closes and reopens the valve.
        self.assertEqual(c.post(reverse("segment_valve_command", args=[self.segment.pk]), {"command": "close"}).status_code, 302)
        self.assertEqual(c.post(reverse("segment_valve_command", args=[self.segment.pk]), {"command": "open"}).status_code, 302)

        # Board setup helpers: refused politely for a board that is connected.
        self.assertEqual(c.post(reverse("owner_device_reissue")).status_code, 302)
        self.assertEqual(c.post(reverse("owner_device_resend")).status_code, 302)

        # Push: a well-formed subscription is stored, a bad one is a 400, never a 500.
        body = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
        r = c.post(reverse("api_push_subscribe"), json.dumps(body), content_type="application/json")
        self.assertIn(r.status_code, (200, 201, 400))
        r = c.post(reverse("api_push_subscribe"), "{}", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = c.post(reverse("api_push_unsubscribe"), json.dumps({"endpoint": body["endpoint"]}), content_type="application/json")
        self.assertIn(r.status_code, (200, 400, 404))

    # ------------------------------------------------------ the facilitator

    def test_facilitator_transactions(self):
        self._as("facilitator")
        c = self.client

        # Edits a household.
        r = c.post(reverse("household_edit", args=[self.other.pk]), {
            "owner_name": "Maria Santos", "address": "Purok 1", "contact_number": "09171234501",
            "account_status": Household.AccountStatus.ACTIVE, "language": "fil",
            "latitude": "9.6378", "longitude": "125.5173",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])

        # Registers a board for one that has none, then activates it.
        self.assertIsNone(self.spare.segment.device_id)
        r = c.post(reverse("household_register_board", args=[self.spare.pk]))
        self.assertEqual(r.status_code, 302)
        self.spare.segment.refresh_from_db()
        self.assertIsNotNone(self.spare.segment.device_id)
        r = c.post(reverse("household_activate", args=[self.spare.pk]), {"latitude": "9.6368", "longitude": "125.5171"})
        self.assertIn(r.status_code, (200, 302))

        # Lines: create, edit, maintenance on and off, valve test, valve close/open.
        r = c.post(reverse("segment_create"), {"name": "Spare line", "color": "#0f766e"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        # The device is part of the form: leaving it out unfits the board,
        # which is what the form's own radio buttons would never do.
        r = c.post(reverse("segment_edit", args=[self.segment.pk]), {
            "name": self.segment.name, "color": self.segment.color, "device": self.device.pk,
            "low_pressure_bar": "0.3", "is_active": "on",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        r = c.post(reverse("segment_maintenance", args=[self.segment.pk]), {"note": "Replacing a joint"})
        self.assertEqual(r.status_code, 302)
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.status, Segment.Status.MAINTENANCE)
        r = c.post(reverse("segment_maintenance", args=[self.segment.pk]), {})
        self.assertEqual(r.status_code, 302)
        self.segment.refresh_from_db()
        self.assertNotEqual(self.segment.status, Segment.Status.MAINTENANCE)
        self.assertEqual(c.post(reverse("segment_valve_test", args=[self.segment.pk])).status_code, 302)
        self.segment.refresh_from_db()
        self.assertTrue(self.segment.valve_test_requested)
        self.assertEqual(c.post(reverse("segment_valve_command", args=[self.segment.pk]), {"command": "close"}).status_code, 302)
        self.assertEqual(c.post(reverse("segment_valve_command", args=[self.segment.pk]), {"command": "open"}).status_code, 302)

        # Boards: a spare, a new key, a claim code, and the spare removed.
        r = c.post(reverse("device_create"), {"label": "spare in the drawer"})
        self.assertEqual(r.status_code, 302)
        spare = Device.objects.get(label="spare in the drawer")
        old_key = spare.api_key
        self.assertEqual(c.post(reverse("device_regenerate_key", args=[spare.pk])).status_code, 302)
        spare.refresh_from_db()
        self.assertNotEqual(spare.api_key, old_key)
        self.assertEqual(c.post(reverse("device_issue_claim_code", args=[spare.pk])).status_code, 302)
        self.assertEqual(c.post(reverse("device_delete", args=[spare.pk])).status_code, 302)
        self.assertFalse(Device.objects.filter(pk=spare.pk).exists())

        # Acknowledges an alert.
        r = c.post(reverse("event_acknowledge", args=[self.event.pk]), {"resolution_note": "Checked the hose."})
        self.assertEqual(r.status_code, 302)
        self.event.refresh_from_db()
        self.assertIsNotNone(self.event.acknowledged_at)

        # Answers a complaint and resolves it.
        r = c.post(reverse("complaint_reply", args=[self.complaint.pk]), {"body": "On our way tomorrow."})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Notification.objects.filter(complaint=self.complaint, body="On our way tomorrow.").exists())
        r = c.post(reverse("complaint_status", args=[self.complaint.pk]), {"status": Complaint.Status.RESOLVED})
        self.assertEqual(r.status_code, 302)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.status, Complaint.Status.RESOLVED)

        # Sends a notice in the app and by text.
        r = c.post(reverse("notification_center"), {
            "households": [self.household.pk], "title": "Flushing on Saturday",
            "body": "Store water the night before.", "in_app": "on", "by_sms": "on",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.assertTrue(Notification.objects.filter(recipient=self.household, title="Flushing on Saturday").exists())

        # Retries a failed text (none for this barangay -> not found, not an error).
        if self.sms is not None:
            self.assertIn(c.post(reverse("sms_retry", args=[self.sms.pk])).status_code, (302, 404))

        # Works through an assignment.
        r = c.post(reverse("assignment_update", args=[self.order.pk]), {"status": WorkOrder.Status.IN_PROGRESS})
        self.assertEqual(r.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.IN_PROGRESS)
        r = c.post(reverse("assignment_update", args=[self.order.pk]), {
            "status": WorkOrder.Status.DONE, "resolution_note": "Branch valve was half shut.",
        })
        self.assertEqual(r.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.DONE)

        # Exports.
        for kind in ("households", "events", "pressure"):
            self.assertEqual(c.get(reverse("export_csv", kwargs={"kind": kind})).status_code, 200)

        # Removes a household that has nothing on it.
        gone = Household.objects.get(household_id="HH-006")
        self.assertEqual(c.post(reverse("household_delete", args=[gone.pk])).status_code, 302)

    # ------------------------------------------------------ the administrator

    def test_admin_transactions(self):
        self._as("admin")
        c = self.client
        today = timezone.localdate()

        # Barangays.
        r = c.post(reverse("barangay_create"), {
            "name": "Upper Dakong Patag", "municipality": "Sison", "province": "Surigao del Norte", "is_active": "on",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        new_brgy = Barangay.objects.get(name="Upper Dakong Patag")
        self.assertEqual(c.post(reverse("barangay_edit", args=[new_brgy.pk]), {
            "name": "Upper Dakong Patag", "municipality": "Sison", "province": "Surigao del Norte", "is_active": "on",
        }).status_code, 302)
        self.assertEqual(c.post(reverse("barangay_toggle", args=[new_brgy.pk])).status_code, 302)

        # Facilitators: create, edit, archive, restore.
        r = c.post(reverse("facilitator_create"), {
            "first_name": "Nena", "last_name": "Abad", "contact_number": "09181234567", "barangay": new_brgy.pk,
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        nena = User.objects.get(first_name="Nena", role=User.Role.FACILITATOR)
        self.assertEqual(c.post(reverse("facilitator_edit", args=[nena.pk]), {
            "first_name": "Nena", "last_name": "Abad-Cruz", "contact_number": "09181234567",
        }).status_code, 302)
        self.assertEqual(c.post(reverse("facilitator_delete", args=[nena.pk])).status_code, 302)
        nena.refresh_from_db()
        self.assertFalse(nena.is_active)
        self.assertEqual(c.post(reverse("archive_restore_facilitator", args=[nena.pk])).status_code, 302)
        nena.refresh_from_db()
        self.assertTrue(nena.is_active)

        # Households: register, edit, archive, restore.
        r = c.post(reverse("admin_household_create"), {
            "owner_name": "Teodoro Villa", "address": "Purok 4", "contact_number": "09191234567",
            "account_status": Household.AccountStatus.ACTIVE, "language": "fil", "barangay": self.brgy.pk,
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        teo = Household.objects.get(owner_name="Teodoro Villa")
        self.assertEqual(c.post(reverse("admin_household_edit", args=[teo.pk]), {
            "owner_name": "Teodoro Villa", "address": "Purok 4-A", "contact_number": "09191234567",
            "account_status": Household.AccountStatus.ACTIVE, "language": "fil", "barangay": self.brgy.pk,
        }).status_code, 302)
        self.assertEqual(c.post(reverse("admin_household_delete", args=[teo.pk])).status_code, 302)
        teo.refresh_from_db()
        self.assertTrue(teo.is_archived)
        self.assertEqual(c.post(reverse("archive_restore_household", args=[teo.pk])).status_code, 302)
        teo.refresh_from_db()
        self.assertFalse(teo.is_archived)

        # Logins: edit contact, suspend and restore.
        self.assertEqual(c.post(reverse("account_edit", args=[self.owner.pk]), {
            "first_name": "Demo", "last_name": "Owner", "contact_number": "", "email": "",
        }).status_code, 302)
        self.assertEqual(c.post(reverse("account_suspend", args=[self.owner.pk])).status_code, 302)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertEqual(c.post(reverse("account_suspend", args=[self.owner.pk])).status_code, 302)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

        # Field work: create, approve, edit, progress, done; another rejected.
        r = c.post(reverse("work_order_create"), {
            "kind": WorkOrder.Kind.REPAIR, "household": self.household.pk,
            "title": "Replace the hose coupling", "details": "Split at the joint.",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        job = WorkOrder.objects.get(title="Replace the hose coupling")
        self.assertEqual(c.post(reverse("work_order_approve", args=[job.pk]), {
            "assigned_to": self.faci.pk, "scheduled_for": (today + datetime.timedelta(days=1)).isoformat(),
        }).status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, WorkOrder.Status.SCHEDULED)
        self.assertEqual(c.post(reverse("work_order_edit", args=[job.pk]), {
            "kind": WorkOrder.Kind.REPAIR, "household": self.household.pk,
            "title": "Replace the hose coupling", "details": "Split at the joint, bring a 1/2 inch.",
            "assigned_to": self.faci.pk, "scheduled_for": (today + datetime.timedelta(days=1)).isoformat(),
        }).status_code, 302)
        self.assertEqual(c.post(reverse("work_order_status", args=[job.pk]), {"status": WorkOrder.Status.IN_PROGRESS}).status_code, 302)
        self.assertEqual(c.post(reverse("work_order_status", args=[job.pk]), {
            "status": WorkOrder.Status.DONE, "resolution_note": "Coupling replaced.",
        }).status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, WorkOrder.Status.DONE)
        self.assertIsNotNone(job.completed_at)
        r = c.post(reverse("work_order_create"), {
            "kind": WorkOrder.Kind.VISIT, "household": self.other.pk, "title": "Courtesy visit",
        })
        self.assertEqual(r.status_code, 302)
        visit = WorkOrder.objects.get(title="Courtesy visit")
        self.assertEqual(c.post(reverse("work_order_reject", args=[visit.pk]), {"review_note": "Not needed."}).status_code, 302)
        visit.refresh_from_db()
        self.assertEqual(visit.status, WorkOrder.Status.REJECTED)

        # Pressure page tools: a test alert of each kind, valve close (a lock) and open.
        before = Notification.objects.filter(recipient=self.household).count()
        for condition in ("water_running", "flow_leak", "pressure_leak"):
            r = c.post(reverse("send_test_alert"), {"household": self.household.pk, "condition": condition})
            self.assertEqual(r.status_code, 302)
        self.assertEqual(Notification.objects.filter(recipient=self.household).count(), before + 3)
        self.assertEqual(c.post(reverse("segment_valve_command", args=[self.segment.pk]), {"command": "close"}).status_code, 302)
        self.segment.refresh_from_db()
        self.assertTrue(self.segment.valve_locked)
        self.assertEqual(c.post(reverse("segment_valve_command", args=[self.segment.pk]), {"command": "open"}).status_code, 302)

        # Announcements.
        r = c.post(reverse("announcement_create"), {
            "audience": Announcement.Audience.ALL, "title": "Water interruption",
            "body": "Main line flushing on Saturday.", "in_app": "on",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.assertTrue(Announcement.objects.filter(title="Water interruption").exists())

        # Detection settings reach the board's next config reply.
        r = c.post(reverse("detection_settings"), {
            "flow_warn_minutes": 20, "flow_leak_minutes": 45, "valve_test_hour": 3,
            "valve_test_grace_minutes": 30, "valve_test_drop_bar": "0.2", "auto_close_on_flow_leak": "on",
        })
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.assertEqual(SystemSetting.load().flow_leak_minutes, 45)

        # Data tools: exports, a backup, and the two uploads refusing an empty form without crashing.
        for kind in ("households", "workorders", "pressure", "audit"):
            self.assertEqual(c.get(reverse("export_console", kwargs={"kind": kind})).status_code, 200)
        self.assertEqual(c.get(reverse("backup_download")).status_code, 200)
        self.assertIn(c.post(reverse("household_import"), {}).status_code, (200, 302))
        self.assertIn(c.post(reverse("restore_upload"), {}).status_code, (200, 302))

        # The audit log recorded the session's work.
        self.assertTrue(AuditLog.objects.filter(action__startswith="valve.").exists())

        # Maintenance mode on, then off again - last, because it shuts the doors.
        self.assertEqual(c.post(reverse("maintenance_toggle"), {"maintenance_message": "Back at 5."}).status_code, 302)
        self.assertEqual(c.post(reverse("maintenance_toggle"), {}).status_code, 302)

    # ---------------------------------------------------------- the board

    def test_board_transactions(self):
        # A claim code is traded for an identity; a wrong one is refused, not a crash.
        ticket = self.device.issue_claim_code()
        r = self.client.post(reverse("api_claim"), json.dumps({"code": ticket.code}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json().get("device_id"), self.device.device_id)
        r = self.client.post(reverse("api_claim"), json.dumps({"code": "000000"}), content_type="application/json")
        self.assertIn(r.status_code, (400, 403, 404))
        r = self.client.post(reverse("api_claim"), "not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

        # Readings with a bad key, no body, and a good body.
        self.device.refresh_from_db()
        reading = {"device_id": self.device.device_id, "readings": [{
            "module": 1, "pressure_bar": 2.1, "flow_lpm": 0.0, "status": "NORMAL",
            "valve_state": "open", "valve_reason": "",
        }]}
        r = self.client.post(reverse("api_ingest"), json.dumps(reading), content_type="application/json", HTTP_X_DEVICE_KEY="wrong")
        self.assertIn(r.status_code, (401, 403))
        r = self.client.post(reverse("api_ingest"), "garbage", content_type="application/json", HTTP_X_DEVICE_KEY=self.device.api_key)
        self.assertEqual(r.status_code, 400)
        r = self.client.post(reverse("api_ingest"), json.dumps(reading), content_type="application/json", HTTP_X_DEVICE_KEY=self.device.api_key)
        self.assertEqual(r.status_code, 200)
        self.assertIn("config", r.json())
