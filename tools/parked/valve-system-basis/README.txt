PARKED: "THE SYSTEM IS THE BASIS" FOR THE VALVE  (built and tested 15 Nov 2026,
not deployed - the firmware in the field works and the owner asked to keep it)

WHAT IT DOES
  The server holds the valve's status (Segment.valve_target) and restates
  it on every reply; the board drives the valve to it whenever its own
  position differs. Every screen shows the system's status with a sentence
  on whether the board has confirmed it, amber after 30 s of disagreement.
  Decisions taken with the owner:
    - the board's own temporary closes (night test, flow hold) are kept;
      a leak found by the test, or a CLOSE/OPEN typed at the board, is
      adopted by the system (numbered, adopted once, notice to the
      household); an admin close stands even against the board's page;
    - on boot the board waits up to 60 s for the system before moving the
      valve and reports "unknown" meanwhile; no word -> its own memory;
    - no flow-based "valve did not close" check (declined for now).

WHAT IS HERE
  files/    the complete feature versions of every file touched, as they
            were on 15 Nov 2026 (server, tests, script, styles, Filipino
            strings, firmware, GUIDE, migration 0046)
  *.diff    each of those against the file as left in the project
  scripts/  the edit scripts that produced them, in order:
            server_edits.py, server_edits2.py (plus a one-line gettext
            import and the "adopt on transition" fix, both visible in
            api.py.diff), fw_edits.py, guide_edits.py, po_fill.py

HOW TO BRING IT BACK
  If the base files have not moved: copy files/ over the project, run
  makemigrations --check (0046 is included), migrate, compilemessages -l fil,
  compile and flash the sketch. If they have moved: apply the .diff files
  by hand or re-run the scripts against the new base and fix the anchors
  they complain about. Then: python manage.py test core
  Verified when parked: all 267 tests passed; sketch 83% of flash; owner,
  facilitator and admin screens rendered the new valve block.
