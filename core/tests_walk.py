"""
A tester walking the whole site, as each role, before a deployment.

Not a specification of any one screen - the other test modules do that. This
one behaves like a person with time on their hands: sign in, follow every
link on every page, and on every form press Save three times - with nothing
filled in, with junk (letters in number boxes, a script tag in text boxes,
a bad phone number), and with plausible values - and see what the site does.

What counts as a problem:
  - any page or submission that errors (500), or shows a traceback or an
    unrendered template tag;
  - junk echoed back unescaped (the script tag arriving intact);
  - a box for a phone number, email, number or date accepting junk (a 302
    after an invalid value), which is listed for review rather than failed,
    because some of those are deliberate.
It also walks every page again in Filipino.

Forms that would change who can sign in, or wipe data, are skipped on
purpose (logout, delete/archive, suspend, restore, maintenance mode).
"""

import io
import json
import re

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import html as lxml_html

from .models import Complaint, Household, LeakEvent, User, WorkOrder

MARKER = '<script>alert("xss")</script>'
JUNK = {
    "tel": "123abc",
    "email": "not-an-email",
    "number": "abc",
    "date": "31/31/2026",
    "time": "99:99",
    "url": "not a url",
    "text": MARKER,
    "search": MARKER,
    "textarea": MARKER + " " + "x" * 3000,
    "password": "pw",
}
VALID = {
    "tel": "09171234599",
    "email": "tester@example.com",
    "number": "1",
    "date": "2026-11-12",
    "time": "08:00",
    "url": "https://example.com",
    "text": "Tester value",
    "search": "Tester",
    "textarea": "Written by the walking tester.",
    "password": "tester-pass-123",
}
STRICT_TYPES = ("tel", "email", "number", "date", "time")

SKIP_URL = (
    "/logout", "/i18n/", "/delete/", "/restore/", "/suspend/",
    "/system/maintenance/", "/data/backup/", "/password-change/", "/static/",
    "/sw.js", "/manifest", ".csv", "/new-key/", "/offline",
    "/admin/",            # Django's own admin, not the system's screens
)
MAX_PAGES = 250


def _forms(doc):
    for form in doc.xpath("//form"):
        fields = []
        for el in form.xpath(".//input|.//select|.//textarea"):
            name = el.get("name")
            if not name:
                continue
            tag = el.tag
            ftype = (el.get("type") or "text").lower() if tag == "input" else tag
            options = None
            if tag == "select":
                options = [o.get("value", o.text or "") for o in el.xpath(".//option")]
            fields.append({
                "name": name, "type": ftype, "value": el.get("value", ""),
                "options": options, "required": el.get("required") is not None,
            })
        yield {
            "action": form.get("action") or "",
            "method": (form.get("method") or "get").lower(),
            "fields": fields,
            "has_file": bool(form.xpath(".//input[@type='file']")),
        }


def _payload(form, mode):
    data = {}
    seen_strict = []
    for f in form["fields"]:
        t = f["type"]
        if t in ("hidden",):
            data[f["name"]] = f["value"]
        elif t in ("submit", "button", "reset", "file"):
            continue
        elif t == "checkbox":
            if mode != "empty":
                data[f["name"]] = f["value"] or "on"
        elif t == "radio":
            if mode != "empty":
                data.setdefault(f["name"], f["value"])
        elif t == "select":
            if mode != "empty":
                opts = [o for o in (f["options"] or []) if o != ""]
                if opts:
                    data[f["name"]] = opts[-1] if mode == "junk" else opts[0]
        else:
            if mode == "empty":
                continue
            table = JUNK if mode == "junk" else VALID
            data[f["name"]] = table.get(t, table["text"])
            if mode == "junk" and t in STRICT_TYPES:
                seen_strict.append(f"{f['name']}:{t}")
    return data, seen_strict


@override_settings(DEBUG=False, SMS_ENABLED=True)
class WalkingTester(TestCase):
    @classmethod
    def setUpTestData(cls):
        quiet = io.StringIO()
        with override_settings(DEBUG=True):
            call_command("setup_demo", password="pw", stdout=quiet)
            call_command("setup_demo_owner", stdout=quiet)
        cls.users = {
            "owner": User.objects.get(username="demo"),
            "facilitator": User.objects.get(username="facilitator"),
            "admin": User.objects.get(username="admin"),
        }

    # ------------------------------------------------------------ helpers

    def _sign_in(self, role):
        self.client.logout()
        if role != "anonymous":
            self.client.force_login(self.users[role])

    def _check_page(self, role, url, response, problems, what="GET"):
        code = response.status_code
        if code >= 500:
            problems.append(f"{role} {what} {url} -> {code}")
            return None
        if code != 200:
            return None
        ctype = response.get("Content-Type", "")
        if "text/html" not in ctype:
            return None
        body = response.content.decode("utf-8", "replace")
        if "Traceback (most recent call last)" in body:
            problems.append(f"{role} {what} {url} shows a traceback")
        if re.search(r"\{%\s|\{\{\s", body):
            problems.append(f"{role} {what} {url} has an unrendered template tag")
        if MARKER in body:
            problems.append(f"{role} {what} {url} echoes the script tag unescaped")
        try:
            return lxml_html.fromstring(body)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{role} {what} {url} is not parseable HTML: {exc!r}")
            return None

    def _walk(self, role, starts, language=None):
        self._sign_in(role)
        if language:
            self.client.cookies["django_language"] = language
        problems, review = [], []
        seen, queue = set(), list(starts)
        forms_done = set()
        pages = 0
        while queue and pages < MAX_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if any(s in url for s in SKIP_URL):
                continue
            pages += 1
            try:
                response = self.client.get(url, HTTP_X_REQUESTED_WITH="")
            except Exception as exc:  # noqa: BLE001 - every crash is a finding
                problems.append(f"{role} GET {url} raised {type(exc).__name__}: {exc}")
                continue
            doc = self._check_page(role, url, response, problems)
            if doc is None:
                continue
            for el, attr, link, _pos in doc.iterlinks():
                if el.tag != "a" or attr != "href":
                    continue
                link = link.split("#")[0]
                if link.startswith("/") and link not in seen:
                    queue.append(link)
            if language:
                continue                      # the Filipino pass only reads
            for form in _forms(doc):
                action = form["action"] or url
                if not action.startswith("/") or any(s in action for s in SKIP_URL):
                    continue
                if form["has_file"]:
                    continue
                key = (action, tuple(f["name"] for f in form["fields"]))
                if key in forms_done:
                    continue
                forms_done.add(key)
                for mode in ("empty", "junk", "valid"):
                    data, strict = _payload(form, mode)
                    what = f"{form['method'].upper()}[{mode}]"
                    try:
                        if form["method"] == "post":
                            r = self.client.post(action, data)
                        else:
                            r = self.client.get(action, data)
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"{role} {what} {action} {sorted(data)} raised {type(exc).__name__}: {exc}")
                        continue
                    self._check_page(role, action, r, problems, what=what)
                    if mode == "junk" and strict and r.status_code in (301, 302):
                        review.append(f"{role} {action} accepted junk in {', '.join(strict)}")
        return pages, problems, review

    # ------------------------------------------------------------ the walks

    def _run(self, role, starts):
        pages, problems, review = self._walk(role, starts)
        fil_pages, fil_problems, _ = self._walk(role, starts, language="fil")
        for line in review:
            print("REVIEW:", line)
        print(f"WALK: {role}: {pages} pages in English, {fil_pages} in Filipino")
        self.assertEqual(problems + fil_problems, [], "\n" + "\n".join(problems + fil_problems))

    def test_a_visitor(self):
        self._run("anonymous", ["/", reverse("login"), reverse("password_reset")])

    def test_the_error_pages_in_both_languages(self):
        # DEBUG is off for this class, so these are the site's own 404/403
        # pages, not Django's technical ones. Each must render, translated.
        for language, word in (("en", "not found"), ("fil", "hindi")):
            self.client.cookies["django_language"] = language
            r = self.client.get("/no-such-page/")
            self.assertEqual(r.status_code, 404)
            self.assertIn(word, r.content.decode().lower(), language)
        self._sign_in("owner")
        r = self.client.get(reverse("console_dashboard"))
        self.assertIn(r.status_code, (302, 403))

    def test_an_owner(self):
        self._run("owner", [reverse("owner_home")])

    def test_a_facilitator(self):
        self._run("facilitator", [reverse("dashboard")])

    def test_an_admin(self):
        self._run("admin", [reverse("console_dashboard")])


class BoardFuzz(TestCase):
    """A board sending nonsense must get a 4xx, never a 500."""

    @classmethod
    def setUpTestData(cls):
        quiet = io.StringIO()
        with override_settings(DEBUG=True):
            call_command("setup_demo", password="pw", stdout=quiet)
        cls.household = Household.objects.get(household_id="HH-001")
        cls.device = cls.household.segment.device

    def _post(self, body, key=None):
        raw = body if isinstance(body, str) else json.dumps(body)
        return self.client.post(
            reverse("api_ingest"), raw, content_type="application/json",
            HTTP_X_DEVICE_KEY=key or self.device.api_key,
        )

    def test_bad_bodies_are_rejected_not_crashed(self):
        cases = [
            "not json",
            {"device_id": "x"},
            {"readings": "nope"},
            {"readings": []},
            {"readings": [{}]},
            {"readings": [{"module": "one", "pressure_bar": 1}]},
            {"readings": [{"module": 1, "pressure_bar": "high", "flow_lpm": None}]},
            {"readings": [{"module": 1, "pressure_bar": -5, "flow_lpm": 1e9, "status": "WEIRD"}]},
            {"readings": [{"module": 99, "pressure_bar": 1, "flow_lpm": 0}]},
            {"readings": [{"module": 1, "pressure_bar": 1, "flow_lpm": 0, "valve_state": "sideways", "valve_reason": "because"}]},
            {"readings": [{"module": 1, "pressure_bar": float("nan") if False else "NaN", "flow_lpm": 0}]},
        ]
        for body in cases:
            r = self._post(body)
            self.assertLess(r.status_code, 500, f"{body!r} -> {r.status_code}: {r.content[:200]}")

    def test_claim_with_nonsense(self):
        for body in ("not json", {}, {"code": None}, {"code": "x" * 500}, {"code": MARKER}):
            raw = body if isinstance(body, str) else json.dumps(body)
            r = self.client.post(reverse("api_claim"), raw, content_type="application/json")
            self.assertLess(r.status_code, 500, f"{body!r} -> {r.status_code}")

    def test_history_with_nonsense(self):
        self.client.force_login(User.objects.get(username="admin"))
        for params in ({"hours": "abc"}, {"segment": "x"}, {"hours": -1, "segment": 999999}, {"hours": 10**9}):
            r = self.client.get(reverse("api_history"), params)
            self.assertLess(r.status_code, 500, f"{params!r} -> {r.status_code}")
