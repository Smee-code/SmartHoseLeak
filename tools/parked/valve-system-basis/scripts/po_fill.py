import io, re, sys, os

os.chdir(sys.argv[1])

DJANGO = {
    "the board itself": "ang board mismo",
    "The board has just restarted and is asking the system where the valve should be.":
        "Kaka-restart lang ng board at tinatanong nito sa sistema kung dapat bukas o sarado ang valve.",
    "Closed at the board itself (its own page or the serial monitor).":
        "Isinara mula mismo sa board (sa sarili nitong page o sa serial monitor).",
    "Closed.": "Sarado.",
    "Water should be flowing.": "Dapat ay umaagos ang tubig.",
    "The board has not confirmed this. Check the valve and its wiring.":
        "Hindi pa ito nakukumpirma ng board. Suriin ang valve at ang mga kable nito.",
    "The board is not reporting, so this cannot be confirmed.":
        "Hindi nagre-report ang board, kaya hindi ito makukumpirma.",
    "Your water valve was closed at the board itself. Your water is off until it is opened again.":
        "Isinara ang valve ng tubig mo mula mismo sa board. Walang tubig hangga't hindi ito binubuksan ulit.",
    "Your water valve was opened at the board itself. Water is back.":
        "Binuksan ang valve ng tubig mo mula mismo sa board. Bumalik na ang tubig.",
}
DJANGOJS = {
    "checking…": "sinusuri…",
}


def unquote(lines):
    out = ""
    for ln in lines:
        m = re.match(r'\s*"(.*)"\s*$', ln)
        if m:
            out += m.group(1).encode('utf-8').decode('unicode_escape').encode('latin-1').decode('utf-8')
    return out


def fill(path, table):
    raw = io.open(path, encoding='utf-8', newline='').read()
    nl = '\r\n' if '\r\n' in raw else '\n'
    blocks = raw.split(nl + nl)
    filled, missing = [], set(table)
    for i, block in enumerate(blocks):
        lines = block.split(nl)
        try:
            mi = next(k for k, ln in enumerate(lines) if ln.startswith('msgid '))
            ms = next(k for k, ln in enumerate(lines) if ln.startswith('msgstr '))
        except StopIteration:
            continue
        if any(ln.startswith('msgctxt') for ln in lines):
            continue
        msgid = unquote([lines[mi][6:]] + lines[mi + 1:ms])
        if msgid not in table:
            continue
        missing.discard(msgid)
        msgstr = unquote([lines[ms][7:]] + lines[ms + 1:])
        if msgstr and not any(ln.startswith('#,') and 'fuzzy' in ln for ln in lines):
            continue
        tr = table[msgid].replace('\\', '\\\\').replace('"', '\\"')
        new = [ln for ln in lines[:mi] if not (ln.startswith('#,') and 'fuzzy' in ln)]
        new += lines[mi:ms] + ['msgstr "%s"' % tr]
        blocks[i] = nl.join(new)
        filled.append(msgid)
    io.open(path, 'w', encoding='utf-8', newline='').write((nl + nl).join(blocks))
    print(os.path.basename(path), "filled:", len(filled), "| not found in .po:", sorted(missing))


fill('locale/fil/LC_MESSAGES/django.po', DJANGO)
fill('locale/fil/LC_MESSAGES/djangojs.po', DJANGOJS)
