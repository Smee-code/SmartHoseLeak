import io, re, sys, os

ROOT = sys.argv[1]
os.chdir(ROOT)


def load(p):
    t = io.open(p, encoding='utf-8', newline='').read()
    nl = '\r\n' if '\r\n' in t else '\n'
    return t, nl


def save(p, t):
    io.open(p, 'w', encoding='utf-8', newline='').write(t)


def rep(t, nl, old, new, count=1):
    old = old.replace('\n', nl)
    new = new.replace('\n', nl)
    assert t.count(old) == count, (old[:70], t.count(old))
    return t.replace(old, new)


# ---------------------------------------------------------------- models.py
p = 'core/models.py'
t, nl = load(p)
t = rep(t, nl, '        HOUSEHOLD = "household", _("the household")\n',
        '        HOUSEHOLD = "household", _("the household")\n'
        '        BOARD = "board", _("the board itself")\n')
t = rep(t, nl, '''    @property
    def valve_locked(self):
        """True while an administrator's close is in force."""
        return self.valve_closed_by == self.ValveClosedBy.ADMIN
''', '''    @property
    def valve_locked(self):
        """True while an administrator's close is in force."""
        return self.valve_closed_by == self.ValveClosedBy.ADMIN

    # What the SYSTEM says the valve should be. This is the basis: the board
    # follows it, not the other way round. Set by the Open and Close buttons,
    # restated to the board on every reply, and adopted from the board only
    # for the two decisions that are the board's to make - a leak found by
    # the nightly test, and a close or open typed at the board itself. So a
    # reboot, a reflash or a dropped reply ends with the valve where the
    # system says, because the board asks and is told.
    class ValveTarget(models.TextChoices):
        OPEN = "open", _("Open")
        CLOSED = "closed", _("Closed")

    valve_target = models.CharField(
        max_length=6, choices=ValveTarget.choices, default=ValveTarget.OPEN
    )
    # When the status last changed, so a report that still disagrees reads as
    # "the board has not picked it up yet" for the first half minute and as
    # "the board has not confirmed this" after.
    valve_target_at = models.DateTimeField(null=True, blank=True)
    # The last close/open made at the board itself (its page or the serial
    # monitor) that the system adopted. The board numbers them, so one is
    # adopted once however many uploads repeat it.
    valve_local_seq = models.PositiveIntegerField(default=0)

    def set_valve_target(self, target, closed_by, when=None):
        """Record the system's decision, plus the one-shot command older firmware acts on."""
        self.valve_target = target
        self.valve_target_at = when or timezone.now()
        self.valve_closed_by = closed_by
        self.valve_command = (
            self.ValveCommand.CLOSE if target == self.ValveTarget.CLOSED else self.ValveCommand.OPEN
        )
        self.save(update_fields=["valve_target", "valve_target_at", "valve_closed_by", "valve_command"])
''')
start = t.index('def describe_valve(segment, latest):')
endm = '    return gettext("Closed by hand at the board.")' + nl
end = t.index(endm, start) + len(endm)
new_fn = '''# How long after the system's status changes a disagreeing report is still
# "the board has not picked it up yet": one full valve travel (18 s) plus the
# upload that carries the new position.
VALVE_CONFIRM_SECONDS = 30


def valve_agreement(segment, latest, stale=False):
    """
    Does the board's last report agree with what the system says?

    One of:
      "not_fitted"   no valve reported at all
      "confirmed"    the board reports the position the system holds
      "pending"      the status changed under half a minute ago; the board has
                     not reported the new position yet
      "unconfirmed"  the board still reports the other position long after the
                     change - a relay, a wire, or an old build
      "board_hold"   the system says open, but the board has closed the line
                     itself for the nightly test or a flow run (temporary)
      "waiting"      the board has just restarted and is asking the system
      "stale"        the board is not reporting, so nothing can be confirmed
    """
    state = latest.valve_state if latest else ValveState.NOT_FITTED
    reason = latest.valve_reason if latest else ValveReason.NONE
    if state == ValveState.NOT_FITTED:
        return "not_fitted"
    if stale:
        return "stale"
    if state == ValveState.UNKNOWN:
        return "waiting"
    if segment.valve_target == Segment.ValveTarget.CLOSED:
        agrees = state == ValveState.CLOSED
    else:
        if state == ValveState.CLOSED and reason in (ValveReason.TEST, ValveReason.FLOW):
            return "board_hold"
        agrees = state == ValveState.OPEN
    if agrees:
        return "confirmed"
    if state == ValveState.MOVING:
        return "pending"
    changed_at = segment.valve_target_at
    if changed_at is not None and (timezone.now() - changed_at).total_seconds() < VALVE_CONFIRM_SECONDS:
        return "pending"
    return "unconfirmed"


def describe_valve(segment, latest, stale=False):
    """
    One plain sentence about the valve, for every screen that shows it.

    The system's status comes first, because that is what the board is made
    to follow; whether the board has confirmed it comes second. Written once
    here so the owner, the facilitator and the administrator read the same
    words for the same state.
    """
    reason = latest.valve_reason if latest else ValveReason.NONE
    agreement = valve_agreement(segment, latest, stale)
    if agreement == "not_fitted":
        return ""
    if agreement == "waiting":
        return gettext("The board has just restarted and is asking the system where the valve should be.")
    if agreement == "board_hold":
        if reason == ValveReason.FLOW:
            return gettext("Closed automatically because water ran non-stop for too long. "
                           "It reopens on its own after a few minutes.")
        return gettext("Closed for the nightly leak test. It reopens when the test ends.")

    if segment.valve_target == Segment.ValveTarget.CLOSED:
        if agreement == "pending":
            return gettext("Closing. The board picks the command up within a few seconds.")
        if reason == ValveReason.DECAY:
            base = gettext("Closed because the nightly test found a leak: the line lost pressure "
                           "with every tap shut. Check your taps and hose, then open it here once fixed.")
        elif segment.valve_closed_by == Segment.ValveClosedBy.BOARD:
            base = gettext("Closed at the board itself (its own page or the serial monitor).")
        elif segment.valve_closed_by:
            base = gettext("Closed by {who}.").format(who=segment.get_valve_closed_by_display())
            if segment.valve_locked:
                base += " " + gettext("Only the administrator can open it again.")
        else:
            base = gettext("Closed.")
    else:
        if agreement == "pending":
            return gettext("Opening. Water is back within a few seconds.")
        base = gettext("Water can flow.") if agreement == "confirmed" else gettext("Water should be flowing.")

    if agreement == "unconfirmed":
        return base + " " + gettext("The board has not confirmed this. Check the valve and its wiring.")
    if agreement == "stale":
        return base + " " + gettext("The board is not reporting, so this cannot be confirmed.")
    return base
'''.replace('\n', nl)
t = t[:start] + new_fn + t[end:]
save(p, t)
print("models ok")

# ---------------------------------------------------------------- api.py
p = 'core/api.py'
t, nl = load(p)
assert '    describe_valve,' + nl in t
t = rep(t, nl, '    describe_valve,\n', '    describe_valve,\n    valve_agreement,\n')
for name in ('Notification', 'AuditLog', 'ValveReason', 'ValveState'):
    if re.search(r'^\s+%s,$' % name, t, re.M) is None:
        t = rep(t, nl, '    describe_valve,\n', '    %s,\n    describe_valve,\n' % name)
for imp in ('from django.utils import translation',
            'from django.utils.translation import gettext',
            'from django.conf import settings'):
    if imp not in t:
        t = rep(t, nl, 'from .models import (\n', imp + '\nfrom .models import (\n')
t = rep(t, nl, '''        "run_valve_test": segment.valve_test_requested,
        "valve_command": segment.valve_command,
''', '''        "run_valve_test": segment.valve_test_requested,
        "valve_command": segment.valve_command,
        # The system's status, restated on every reply. The board drives the
        # valve to it whenever its own position differs (and it is not in the
        # middle of a nightly test or a flow hold), so a reboot or a lost
        # reply cannot leave the valve somewhere the system does not know.
        "valve_target": segment.valve_target,
''')
t = rep(t, nl, '''    return {
        "id": segment.id,
        "module": segment.module_index,
''', '''    agreement = valve_agreement(segment, latest, stale)

    return {
        "id": segment.id,
        "module": segment.module_index,
''')
t = rep(t, nl, '        "valve_detail": describe_valve(segment, latest),\n',
        '        "valve_detail": describe_valve(segment, latest, stale),\n')
t = rep(t, nl, '        "valve_command": segment.valve_command,\n        "calibrating"',
        '''        "valve_command": segment.valve_command,
        # The system's status, and whether the board's last report agrees
        # with it. valve_warn is the "not confirmed" case every screen shows.
        "valve_target": segment.valve_target,
        "valve_agreement": agreement,
        "valve_warn": agreement == "unconfirmed",
        "calibrating"''')
t = rep(t, nl, '''            elif segment.valve_command == Segment.ValveCommand.OPEN and valve_state == ValveState.OPEN:
                segment.valve_command = Segment.ValveCommand.NONE
                segment.save(update_fields=["valve_command"])
''', '''            elif segment.valve_command == Segment.ValveCommand.OPEN and valve_state == ValveState.OPEN:
                segment.valve_command = Segment.ValveCommand.NONE
                segment.save(update_fields=["valve_command"])

            # The system is the basis for the valve, but two decisions are the
            # board's and the system adopts them: a leak found by the nightly
            # test, and a close or open made at the board itself.
            _adopt_board_valve_decisions(segment, entry, valve_state, valve_reason, now)
''')
t = rep(t, nl, 'def _command_payload(segment):\n', '''def _adopt_board_valve_decisions(segment, entry, valve_state, valve_reason, now):
    """
    Take over the valve decisions only the board can make.

    A leak found by the nightly test: the board holds the line shut, and the
    system adopts "closed" so it does not tell the board to reopen a leaking
    line; anyone with the button opens it once it is fixed. A close or open
    made at the board itself (its own page, or CLOSE/OPEN on the serial
    monitor): the board numbers each one (valve_local_seq) so it is adopted
    once however many uploads repeat it. An administrator's lock stands even
    against the board's own page - the open is not adopted, the reply
    restates closed, and the board shuts the line again.
    """
    changed = False
    if (valve_reason == ValveReason.DECAY and valve_state == ValveState.CLOSED
            and segment.valve_target != Segment.ValveTarget.CLOSED):
        segment.valve_target = Segment.ValveTarget.CLOSED
        segment.valve_target_at = now
        segment.valve_closed_by = Segment.ValveClosedBy.BOARD
        segment.valve_command = Segment.ValveCommand.NONE
        changed = True

    raw_seq = entry.get("valve_local_seq")
    try:
        seq = int(raw_seq) if raw_seq is not None else None
    except (TypeError, ValueError):
        seq = None
    if seq is not None and seq > segment.valve_local_seq:
        segment.valve_local_seq = seq
        wanted = (Segment.ValveTarget.CLOSED if valve_state == ValveState.CLOSED
                  else Segment.ValveTarget.OPEN)
        if segment.valve_locked and wanted == Segment.ValveTarget.OPEN:
            pass                                    # the administrator's close stands
        elif wanted != segment.valve_target:
            segment.valve_target = wanted
            segment.valve_target_at = now
            segment.valve_closed_by = (
                Segment.ValveClosedBy.BOARD if wanted == Segment.ValveTarget.CLOSED
                else Segment.ValveClosedBy.NOBODY
            )
            segment.valve_command = Segment.ValveCommand.NONE
            closed = wanted == Segment.ValveTarget.CLOSED
            verb = "closed" if closed else "opened"
            AuditLog.record(
                None, "valve.close" if closed else "valve.open",
                f"The valve on {segment.name} was {verb} at the board itself",
                target=segment.name,
            )
            _notify_board_valve(segment, closed)
        changed = True

    if changed:
        segment.save(update_fields=[
            "valve_target", "valve_target_at", "valve_closed_by", "valve_command", "valve_local_seq",
        ])


def _notify_board_valve(segment, closed):
    """Tell the household, in their language, that their valve moved at the board."""
    household = getattr(segment, "household", None)
    if household is None:
        return
    with translation.override(household.language or settings.LANGUAGE_CODE):
        body = (
            gettext("Your water valve was closed at the board itself. Your water is off until it is opened again.")
            if closed else gettext("Your water valve was opened at the board itself. Water is back.")
        )
        Notification.objects.create(
            kind=Notification.Kind.VALVE,
            title=gettext("Valve closed") if closed else gettext("Valve opened"),
            body=body,
            recipient=household,
            segment=segment,
        )


def _command_payload(segment):
''')
save(p, t)
print("api ok")

# ---------------------------------------------------------------- views.py
p = 'core/views.py'
t, nl = load(p)
t = rep(t, nl, '''        segment.valve_command = Segment.ValveCommand.CLOSE
        segment.valve_closed_by = role
        segment.save(update_fields=["valve_command", "valve_closed_by"])
''', '''        segment.set_valve_target(Segment.ValveTarget.CLOSED, role)
''')
t = rep(t, nl, '''        segment.valve_command = Segment.ValveCommand.OPEN
        segment.valve_closed_by = Segment.ValveClosedBy.NOBODY
        segment.save(update_fields=["valve_command", "valve_closed_by"])
''', '''        segment.set_valve_target(Segment.ValveTarget.OPEN, Segment.ValveClosedBy.NOBODY)
''')
save(p, t)
print("views ok")

# ---------------------------------------------------------------- app.js
p = 'static/js/app.js'
t, nl = load(p)
t = rep(t, nl, '''        if (fitted) {
          var closed = segment.valve_state === 'closed';
          var moving = segment.valve_state === 'moving';
          var pending = segment.valve_command;
          var label = moving ? gettext('moving')
            : pending === 'close' ? gettext('closing…')
            : pending === 'open' ? gettext('opening…')
            : closed ? gettext('closed') : gettext('open');
          var txt = $('[data-field="valve_state_text"]', valveControl);
          if (txt) txt.textContent = label;
          var role = valveControl.getAttribute('data-valve-role') || 'admin';
          var locked = !!segment.valve_locked && role !== 'admin';
          // Offer Open when shut (or closing); Close when open (or opening).
          var openForm = $('[data-valve-open]', valveControl);
          var closeForm = $('[data-valve-close]', valveControl);
          var lockNote = $('[data-valve-locked]', valveControl);
          var showOpen = closed || pending === 'close';
          if (openForm) openForm.hidden = !showOpen || locked;
          if (closeForm) closeForm.hidden = showOpen;
          if (lockNote) lockNote.hidden = !(showOpen && locked);
        }
''', '''        if (fitted) {
          // The system's status is the headline: that is what the board is
          // made to follow. Whether the board has confirmed it is the
          // sentence underneath (valve_detail, written on the server), and
          // an unconfirmed status marks the block as a warning.
          var target = segment.valve_target || (segment.valve_state === 'closed' ? 'closed' : 'open');
          var agreement = segment.valve_agreement || 'confirmed';
          var wantClosed = target === 'closed';
          var label;
          if (agreement === 'pending') label = wantClosed ? gettext('closing…') : gettext('opening…');
          else if (agreement === 'board_hold') label = gettext('closed');
          else if (agreement === 'waiting') label = gettext('checking…');
          else label = wantClosed ? gettext('closed') : gettext('open');
          var txt = $('[data-field="valve_state_text"]', valveControl);
          if (txt) txt.textContent = label;
          valveControl.classList.toggle('is-warn', !!segment.valve_warn);
          var role = valveControl.getAttribute('data-valve-role') || 'admin';
          var locked = !!segment.valve_locked && role !== 'admin';
          // Offer the button that changes the system's status: Open when it
          // says closed, Close when it says open.
          var openForm = $('[data-valve-open]', valveControl);
          var closeForm = $('[data-valve-close]', valveControl);
          var lockNote = $('[data-valve-locked]', valveControl);
          if (openForm) openForm.hidden = !wantClosed || locked;
          if (closeForm) closeForm.hidden = wantClosed;
          if (lockNote) lockNote.hidden = !(wantClosed && locked);
        }
''')
save(p, t)
print("js ok")

# ---------------------------------------------------------------- app.css
p = 'static/css/app.css'
t, nl = load(p)
t = rep(t, nl, '.valve-cell form { margin: 0; }\n', '''.valve-cell form { margin: 0; }
/* The system's status the board has not confirmed: a relay, a wire or an
   old build. The sentence under the state carries the warning. */
[data-valve-control].is-warn [data-field="valve_detail"] { color: var(--warn); font-weight: 600; }
.owner-valve.is-warn { outline: 2px solid #ffc466; outline-offset: 3px; }
.owner-valve.is-warn .owner-valve-detail { color: #ffd38a; }
''')
save(p, t)
print("css ok")

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
head = t[:6000]
m = re.search(r'^from datetime import ([^\r\n]+)', head, re.M)
if m and 'timedelta' not in m.group(1):
    t = t.replace(m.group(0), m.group(0) + ', timedelta', 1)
elif not m:
    t = 'from datetime import timedelta' + nl + t
if re.search(r'^from django\.utils import .*\btimezone\b', t, re.M) is None:
    t = 'from django.utils import timezone' + nl + t
if re.search(r'^\s+Notification,$', t, re.M) is None and 'import Notification' not in t:
    t = rep(t, nl, 'from .models import (\n', 'from .models import Notification\nfrom .models import (\n')
save(p, t)
print("tests ok")
