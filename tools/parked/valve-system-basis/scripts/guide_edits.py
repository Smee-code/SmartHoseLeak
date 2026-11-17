import io, sys, os

os.chdir(sys.argv[1])
p = 'GUIDE.txt'
t = io.open(p, encoding='utf-8', newline='').read()
nl = '\r\n' if '\r\n' in t else '\n'


def rep(old, new):
    global t
    old = old.replace('\n', nl)
    new = new.replace('\n', nl)
    assert t.count(old) == 1, (old[:70], t.count(old))
    t = t.replace(old, new)


# ---- 9e THE VALVE: who decides
rep('''THE VALVE
  It is opened on boot, before anything else, so a power cut can never leave
  a house with the water shut off - EXCEPT a line somebody closed by hand,
  which is re-closed on boot so a shut-off survives a power cut.
''', '''THE VALVE
  THE SYSTEM IS THE BASIS (since 15 Nov 2026). The status the portal, the
  dashboard and the console show is what the server holds (valve_target);
  the board follows it. Every reply to an upload or a command poll carries
  it, and the board drives the valve to it whenever its own position
  differs, so a reboot, a reflash or a dropped reply ends with the valve
  where the system says. Under the status, one sentence says whether the
  board has confirmed it:
    "Closing." / "Opening."          - changed under 30 s ago, not reported yet
    "...The board has not confirmed
     this. Check the valve and its
     wiring."                        - the board still reports the other
                                       position after 30 s: a relay, a wire
                                       or an old build (amber on every screen)
    "The board has just restarted
     and is asking the system..."    - the boot wait below
    "...The board is not reporting"  - the board is offline
  Two decisions stay the board's, and the system adopts them:
    - the nightly test and the flow hold are temporary closes; the system's
      "open" does not undo them, and when they end the valve goes where the
      system says;
    - a leak found by the nightly test becomes "closed" in the system until
      somebody presses Open; a CLOSE or OPEN typed at the board (its own
      page, the serial monitor) is adopted too, numbered so it is adopted
      once, and the household gets a notice. An administrator's close
      stands even against the board's own page: the open is not adopted,
      the reply restates closed, and the board shuts the line again.
  ON BOOT the valve is left where it is until the system has said open or
  closed (the first reply - seconds, when online); meanwhile the board
  reports "unknown" and the screens say it is asking. With no word after
  60 s (no WiFi, no server) the board's own memory decides: a manual or
  leak-found close is restored shut, anything else is driven open.
''')

# ---- section 12: the amber sentence
rep('''The dashboard says the valve is "closed" but the valve did not move
''', '''The valve row says "The board has not confirmed this" (amber)
    The system's status and the board's last report have disagreed for more
    than 30 s. The system keeps restating its status on every reply, and a
    board on the 15 Nov 2026 firmware drives the valve to it as soon as it
    hears - so a warning that lasts means the board cannot: the relay for
    that direction, its wiring or its supply (next entry), or a board on
    older firmware, which acts only on the one-shot command and ignores the
    restated status - reflash it. Pressing the button again re-sends the
    one-shot command that old firmware does understand.

The dashboard says the valve is "closed" but the valve did not move
''')

# ---- section 13 cadence table
rep('''                      network if nobody is on the hotspot (firmware)
''', '''                      network if nobody is on the hotspot (firmware)
  Valve on boot       left as found for up to 60 s while the board asks the
                      system; then driven to the system's status (or by the
                      board's own memory if no word came)
''')

# ---- changelog
rep('''15 Nov 2026  Firmware: RELAY_OPEN is GPIO 26 and RELAY_CLOSE GPIO 25 now
''', '''15 Nov 2026  The system is the basis for the valve. The server holds the
             status (Segment.valve_target, migration 0046), restates it on
             every reply, and the board drives the valve to it. The board's
             own temporary closes (night test, flow hold) are kept; a leak
             found, or a CLOSE/OPEN typed at the board, is adopted by the
             system (numbered, once; the household gets a notice); an
             admin close stands even against the board's own page. Every
             screen shows the system's status with one sentence on whether
             the board has confirmed it, amber after 30 s of disagreement.
             On boot the board waits up to 60 s for the system before
             moving the valve and reports "unknown" meanwhile. Firmware,
             server, tests, Filipino strings. Section 9e THE VALVE, section
             12 (amber sentence), section 13.

15 Nov 2026  Firmware: RELAY_OPEN is GPIO 26 and RELAY_CLOSE GPIO 25 now
''')

io.open(p, 'w', encoding='utf-8', newline='').write(t)
print("guide ok")
