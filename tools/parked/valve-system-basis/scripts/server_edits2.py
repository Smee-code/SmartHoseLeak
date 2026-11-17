import io, re, sys, os

os.chdir(sys.argv[1])


def load(p):
    t = io.open(p, encoding='utf-8', newline='').read()
    return t, ('\r\n' if '\r\n' in t else '\n')


def save(p, t):
    io.open(p, 'w', encoding='utf-8', newline='').write(t)


def rep(t, nl, old, new, count=1):
    old = old.replace('\n', nl)
    new = new.replace('\n', nl)
    assert t.count(old) == count, (old[:70], t.count(old))
    return t.replace(old, new)


# ---------------------------------------------------------------- api.py
# A close the board made itself is also the board's to release: an old build
# that reopens (serial OPEN without a sequence number) must not leave the
# screens saying "closed, not confirmed" for ever.
p = 'core/api.py'
t, nl = load(p)
t = rep(t, nl, '''    changed = False
    if (valve_reason == ValveReason.DECAY and valve_state == ValveState.CLOSED
            and segment.valve_target != Segment.ValveTarget.CLOSED):
''', '''    changed = False
    # A close the board made itself is also the board's to release. Older
    # firmware has no sequence numbers, so a board that reports open after a
    # close the system adopted from it (and that nobody else has since taken
    # over) is believed, rather than nagged with "not confirmed" for ever.
    if (valve_state == ValveState.OPEN and valve_reason != ValveReason.DECAY
            and segment.valve_target == Segment.ValveTarget.CLOSED
            and segment.valve_closed_by == Segment.ValveClosedBy.BOARD):
        segment.valve_target = Segment.ValveTarget.OPEN
        segment.valve_target_at = now
        segment.valve_closed_by = Segment.ValveClosedBy.NOBODY
        segment.valve_command = Segment.ValveCommand.NONE
        changed = True
    if (valve_reason == ValveReason.DECAY and valve_state == ValveState.CLOSED
            and segment.valve_target != Segment.ValveTarget.CLOSED):
''')
save(p, t)
print("api ok")

# ---------------------------------------------------------------- tests.py
p = 'core/tests.py'
t, nl = load(p)
t = rep(t, nl, '''    def _ingest(self, valve_state, valve_reason=None):
        reading = {
            "module": 1, "pressure_bar": 0.8, "flow_lpm": 0.0,
            "status": "NORMAL", "valve_state": valve_state,
        }
        if valve_reason is not None:
            reading["valve_reason"] = valve_reason
''', '''    def _ingest(self, valve_state, valve_reason=None, local_seq=None):
        reading = {
            "module": 1, "pressure_bar": 0.8, "flow_lpm": 0.0,
            "status": "NORMAL", "valve_state": valve_state,
        }
        if valve_reason is not None:
            reading["valve_reason"] = valve_reason
        if local_seq is not None:
            reading["valve_local_seq"] = local_seq
''')
t = rep(t, nl, '    def test_a_household_cannot_touch_another_line(self):\n', '''    # ---- the system is the basis: its status rides on every reply, and the
    # ---- screens say whether the board has confirmed it

    def test_the_system_status_rides_on_every_reply_not_just_while_pending(self):
        self.client.login(username="admin", password="pw")
        self._command("close")
        cfg = self._ingest("open").json()["config"]["1"]
        self.assertEqual(cfg["valve_target"], "closed")
        self._ingest("closed", "manual")              # confirmed: the one-shot command clears...
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.valve_command, Segment.ValveCommand.NONE)
        cfg = self._ingest("closed", "manual").json()["config"]["1"]
        self.assertEqual(cfg["valve_target"], "closed")   # ...but the status is restated for ever

    def test_a_board_still_reporting_the_other_position_is_flagged_after_half_a_minute(self):
        self.client.login(username="admin", password="pw")
        self._command("close")
        self._ingest("open")
        live = self._live_segment()
        self.assertEqual(live["valve_agreement"], "pending")
        self.assertFalse(live["valve_warn"])
        Segment.objects.filter(pk=self.segment.pk).update(
            valve_target_at=timezone.now() - timedelta(seconds=90)
        )
        self._ingest("open")                          # a reboot, a swapped wire, an old build
        live = self._live_segment()
        self.assertEqual(live["valve_target"], "closed")
        self.assertEqual(live["valve_agreement"], "unconfirmed")
        self.assertTrue(live["valve_warn"])
        self.assertIn("has not confirmed", live["valve_detail"])

    def test_the_boards_own_test_close_is_not_a_mismatch(self):
        self.client.login(username="admin", password="pw")
        self._ingest("closed", "test")
        live = self._live_segment()
        self.assertEqual(live["valve_target"], "open")
        self.assertEqual(live["valve_agreement"], "board_hold")
        self.assertFalse(live["valve_warn"])
        self.assertIn("nightly leak test", live["valve_detail"])

    def test_a_leak_found_by_the_board_becomes_the_system_status_until_somebody_opens(self):
        self._ingest("closed", "decay")
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.valve_target, Segment.ValveTarget.CLOSED)
        self.assertEqual(self.segment.valve_closed_by, Segment.ValveClosedBy.BOARD)
        self.assertFalse(self.segment.valve_locked)
        self.client.login(username="own80", password="pw")
        self._command("open")
        cfg = self._ingest("closed", "decay").json()["config"]["1"]
        self.assertEqual(cfg["valve_target"], "open")

    def test_a_close_typed_at_the_board_is_adopted_once(self):
        def valve_notes():
            return self.household.notifications.filter(kind=Notification.Kind.VALVE).count()
        self._ingest("closed", "manual", local_seq=1)
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.valve_target, Segment.ValveTarget.CLOSED)
        self.assertEqual(self.segment.valve_closed_by, Segment.ValveClosedBy.BOARD)
        self.assertEqual(self.segment.valve_local_seq, 1)
        self.assertEqual(valve_notes(), 1)
        self._ingest("closed", "manual", local_seq=1)   # the same upload again
        self.assertEqual(valve_notes(), 1)
        self.client.login(username="admin", password="pw")
        live = self._live_segment()
        self.assertEqual(live["valve_agreement"], "confirmed")
        self.assertIn("at the board itself", live["valve_detail"])

    def test_an_open_at_the_board_cannot_undo_an_administrators_close(self):
        self.client.login(username="admin", password="pw")
        self._command("close")
        self._ingest("closed", "manual")
        cfg = self._ingest("open", "", local_seq=5).json()["config"]["1"]
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.valve_target, Segment.ValveTarget.CLOSED)
        self.assertTrue(self.segment.valve_locked)
        self.assertEqual(self.segment.valve_local_seq, 5)
        self.assertEqual(cfg["valve_target"], "closed")  # the reply sends the board back

    def test_a_board_that_has_just_restarted_says_so(self):
        self.client.login(username="admin", password="pw")
        self._ingest("unknown")
        live = self._live_segment()
        self.assertEqual(live["valve_agreement"], "waiting")
        self.assertIn("just restarted", live["valve_detail"])

    def test_a_household_cannot_touch_another_line(self):
''')
if re.search(r'^from datetime import', t, re.M) is None:
    t = rep(t, nl, 'import json\n', 'import json\nfrom datetime import timedelta\n')
if re.search(r'^\s+Notification,$', t, re.M) is None:
    t = rep(t, nl, 'from core.models import (\n', 'from core.models import (\n    Notification,\n')
save(p, t)
print("tests ok")
