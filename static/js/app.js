/* =========================================================================
   Smart Hose Leak Detection System - shared frontend behaviour.
   Plain ES5-compatible JavaScript, no build step and no framework.
   ========================================================================= */
(function () {
  'use strict';

  var SHL = window.SHL || {};
  SHL.pollMs = SHL.pollMs || 3000;

  /* ---------------------------------------------------------------- utils */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function fmt(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    return Number(value).toFixed(digits === undefined ? 2 : digits);
  }

  function timeAgo(iso) {
    if (!iso) return 'no data yet';
    var seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 10) return 'just now';
    if (seconds < 60) return Math.round(seconds) + 's ago';
    if (seconds < 3600) return Math.round(seconds / 60) + ' min ago';
    if (seconds < 86400) return Math.round(seconds / 3600) + ' hr ago';
    return Math.round(seconds / 86400) + ' d ago';
  }

  var STATUS_COLORS = {
    NORMAL: '#17794c',
    LOW_SUPPLY: '#a8620b',
    POSSIBLE_LEAK: '#b3261e',
    MAINTENANCE: '#a8620b',
    OFFLINE: '#6b7785'
  };
  SHL.statusColor = function (status) { return STATUS_COLORS[status] || STATUS_COLORS.OFFLINE; };

  /* ---------------------------------------------------------------- theme */

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('shl-theme', theme); } catch (e) { /* private mode */ }
    var icon = $('[data-theme-icon]');
    if (icon) icon.innerHTML = theme === 'dark' ? '&#9788;' : '&#9789;';
    document.dispatchEvent(new CustomEvent('shl:theme', { detail: { theme: theme } }));
  }

  var themeToggle = $('#theme-toggle');
  if (themeToggle) {
    applyTheme(document.documentElement.getAttribute('data-theme') || 'light');
    themeToggle.addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      applyTheme(next);
    });
  }

  /* ------------------------------------------------------- side navigation */

  /* Below the drawer breakpoint the sidebar is off-canvas and needs a toggle.
     Above it the sidebar is always visible and none of this runs, so the
     drawer state is cleared whenever the viewport grows past the breakpoint. */

  var sidenav = $('#sidenav');
  var navToggle = $('#nav-toggle');
  var navScrim = $('#sidenav-scrim');
  var navClose = $('#sidenav-close');
  var drawerQuery = window.matchMedia('(max-width: 1024px)');

  function setNav(open) {
    if (!sidenav) return;
    sidenav.classList.toggle('is-open', open);
    if (navScrim) navScrim.hidden = !open;
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('nav-open', open);
  }

  if (sidenav && navToggle) {
    navToggle.addEventListener('click', function () {
      var opening = !sidenav.classList.contains('is-open');
      setNav(opening);
      if (opening) {
        var first = $('a', sidenav);
        if (first) first.focus();
      }
    });

    if (navClose) {
      navClose.addEventListener('click', function () {
        setNav(false);
        navToggle.focus();
      });
    }
    if (navScrim) navScrim.addEventListener('click', function () { setNav(false); });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && sidenav.classList.contains('is-open')) {
        setNav(false);
        navToggle.focus();
      }
    });

    // Following a link in the drawer should not leave it open behind the
    // next page in browsers that restore scroll/DOM state on back.
    $$('a', sidenav).forEach(function (link) {
      link.addEventListener('click', function () { setNav(false); });
    });

    // Rotating a tablet to landscape can cross the breakpoint while the
    // drawer is open; drop the drawer state so the sticky sidebar returns.
    var onBreakpoint = function (event) { if (!event.matches) setNav(false); };
    if (drawerQuery.addEventListener) drawerQuery.addEventListener('change', onBreakpoint);
    else if (drawerQuery.addListener) drawerQuery.addListener(onBreakpoint);
  }

  /* ------------------------------------------------------------- user menu */

  var menuBtn = $('#usermenu-btn');
  var menuPanel = $('#usermenu-panel');
  if (menuBtn && menuPanel) {
    menuBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      var open = menuPanel.hidden;
      menuPanel.hidden = !open;
      menuBtn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function (event) {
      if (!menuPanel.hidden && !menuPanel.contains(event.target)) {
        menuPanel.hidden = true;
        menuBtn.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menuPanel.hidden) {
        menuPanel.hidden = true;
        menuBtn.setAttribute('aria-expanded', 'false');
        menuBtn.focus();
      }
    });
  }

  $$('.message-close').forEach(function (button) {
    button.addEventListener('click', function () {
      var message = button.closest('.message');
      if (message) message.remove();
    });
  });


  /* -------------------------------------------------- connection quality
     Decides whether the dashboard may load Leaflet tiles. Three signals,
     cheapest first: offline flag, the Network Information API, then an
     actual timed tile fetch, because a phone can report 4g and still sit
     behind a dead link. The result is cached briefly so every page load
     does not re-probe.                                                     */

  var CONN_CACHE_KEY = 'shl-conn';
  var CONN_CACHE_MS = 120000;

  function cachedConnection() {
    try {
      var raw = sessionStorage.getItem(CONN_CACHE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (Date.now() - parsed.at > CONN_CACHE_MS) return null;
      return parsed.quality;
    } catch (e) { return null; }
  }

  function cacheConnection(quality) {
    try {
      sessionStorage.setItem(CONN_CACHE_KEY, JSON.stringify({ quality: quality, at: Date.now() }));
    } catch (e) { /* ignore */ }
  }

  function probeTile(timeoutMs) {
    // One real map tile of Cantilan/Surigao at low zoom. If it does not
    // arrive quickly the full map would be painful, so we fall back.
    return new Promise(function (resolve) {
      var settled = false;
      var image = new Image();
      var started = Date.now();
      var timer = setTimeout(function () {
        if (!settled) { settled = true; image.src = ''; resolve({ ok: false, ms: timeoutMs }); }
      }, timeoutMs);

      image.onload = function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ ok: true, ms: Date.now() - started });
      };
      image.onerror = function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ ok: false, ms: Date.now() - started });
      };
      image.src = 'https://tile.openstreetmap.org/7/103/60.png?cb=' + Date.now();
    });
  }

  SHL.checkConnection = function () {
    var cached = cachedConnection();
    if (cached) return Promise.resolve(cached);

    if (navigator.onLine === false) {
      cacheConnection('offline');
      return Promise.resolve('offline');
    }

    var info = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (info) {
      if (info.saveData) { cacheConnection('weak'); return Promise.resolve('weak'); }
      var type = info.effectiveType || '';
      if (type === 'slow-2g' || type === '2g') { cacheConnection('weak'); return Promise.resolve('weak'); }
    }

    return probeTile(2500).then(function (result) {
      var quality = !result.ok ? 'offline' : (result.ms > 1400 ? 'weak' : 'strong');
      cacheConnection(quality);
      return quality;
    });
  };

  function paintConnectionPill(quality) {
    var pill = $('#conn-pill');
    if (!pill) return;
    var labels = {
      strong: ['pill pill-ok', '&#8226; online'],
      weak: ['pill pill-warn', '&#8226; weak signal'],
      offline: ['pill pill-danger', '&#8226; offline']
    };
    var chosen = labels[quality] || labels.offline;
    pill.className = chosen[0];
    pill.innerHTML = chosen[1];
    pill.title = {
      strong: 'Internet connection is good. The interactive map is available.',
      weak: 'Weak connection detected. The offline schematic view is being used instead of the map.',
      offline: 'No internet connection. Showing the offline schematic view.'
    }[quality] || '';
  }

  SHL.checkConnection().then(paintConnectionPill);
  window.addEventListener('online', function () {
    try { sessionStorage.removeItem(CONN_CACHE_KEY); } catch (e) {}
    SHL.checkConnection().then(paintConnectionPill);
  });
  window.addEventListener('offline', function () { paintConnectionPill('offline'); });

  /* ------------------------------------------------------------ live poll */

  /* ---------------------------------------------------------------- modal */

  // Progressive enhancement: [data-modal-open="id"] is an ordinary link to the
  // standalone form page. With JS it opens the inline modal instead, so the
  // page still works if this script fails to load.

  var openModalEl = null;
  var lastFocused = null;

  function focusables(root) {
    return $$('a[href], button:not([disabled]), input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex="-1"])', root)
      .filter(function (el) { return el.offsetParent !== null; });
  }

  function closeModal() {
    if (!openModalEl) return;
    openModalEl.hidden = true;
    document.body.classList.remove('modal-open');
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    openModalEl = null;
    lastFocused = null;
  }

  function openModal(el) {
    if (!el) return;
    lastFocused = document.activeElement;
    el.hidden = false;
    openModalEl = el;
    document.body.classList.add('modal-open');
    // Prefer a field marked autofocus (the sign-in dialog wants the username,
    // not its close button); otherwise the first focusable thing.
    var first = el.querySelector('[autofocus]') || focusables(el)[0];
    if (first) first.focus();
  }

  SHL.openModal = openModal;
  SHL.closeModal = closeModal;

  $$('[data-modal-open]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var target = document.getElementById(trigger.getAttribute('data-modal-open'));
      if (!target) return;           // no modal on this page: follow the link
      event.preventDefault();
      openModal(target);
    });
  });

  $$('[data-modal-close]').forEach(function (btn) {
    btn.addEventListener('click', function (event) {
      event.preventDefault();
      closeModal();
    });
  });

  $$('.modal-backdrop').forEach(function (backdrop) {
    backdrop.addEventListener('mousedown', function (event) {
      // Only a click on the backdrop itself, not one that started inside the
      // dialog and drifted out while selecting text.
      if (event.target === backdrop) closeModal();
    });
  });

  document.addEventListener('keydown', function (event) {
    if (!openModalEl) return;
    if (event.key === 'Escape') {
      closeModal();
      return;
    }
    if (event.key !== 'Tab') return;

    // Keep focus inside the dialog while it is open.
    var items = focusables(openModalEl);
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  /* ------------------------------------------- row-driven household dialogs */

  // One edit dialog and one delete dialog are shared by every row and filled
  // from the clicked row's data attributes. Rendering a pair per row would
  // otherwise put fifty forms in a twenty-five row page.

  function setValue(id, value) {
    var field = document.getElementById(id);
    if (field) field.value = value === undefined || value === null ? '' : value;
  }

  $$('[data-edit-household]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var modal = $('#household-edit-modal');
      var form = $('#household-edit-form');
      if (!modal || !form) return;          // no dialog here: follow the link
      event.preventDefault();

      form.setAttribute('action', trigger.getAttribute('data-action'));
      setValue('edit_owner_name', trigger.getAttribute('data-owner'));
      setValue('edit_address', trigger.getAttribute('data-address'));
      setValue('edit_contact_number', trigger.getAttribute('data-contact'));
      setValue('edit_email', trigger.getAttribute('data-email'));
      setValue('edit_segment', trigger.getAttribute('data-segment'));
      setValue('edit_account_status', trigger.getAttribute('data-status'));
      setValue('edit_latitude', trigger.getAttribute('data-lat'));
      setValue('edit_longitude', trigger.getAttribute('data-lng'));
      setValue('edit_notes', trigger.getAttribute('data-notes'));

      var subtitle = $('[data-edit-subtitle]', modal);
      if (subtitle) subtitle.textContent = trigger.getAttribute('data-hid');

      openModal(modal);
    });
  });

  $$('[data-delete-household]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#household-delete-modal');
      var form = $('#household-delete-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var hid = trigger.getAttribute('data-hid');
      var owner = trigger.getAttribute('data-owner');
      var subtitle = $('[data-delete-subtitle]', modal);
      if (subtitle) subtitle.textContent = hid + ' · ' + owner;
      var name = $('[data-delete-name]', modal);
      if (name) name.textContent = hid + ' (' + owner + ')';

      // Spell out what goes with it. Counts come from the row so the dialog
      // never claims records that are not there.
      var list = $('[data-delete-list]', modal);
      if (list) {
        var items = [];
        var complaints = Number(trigger.getAttribute('data-complaints') || 0);
        var notices = Number(trigger.getAttribute('data-notices') || 0);
        var login = trigger.getAttribute('data-login');
        items.push(complaints + (complaints === 1 ? ' complaint' : ' complaints'));
        items.push(notices + (notices === 1 ? ' notice' : ' notices'));
        if (login) items.push('the Owner Portal login “' + login + '”');
        list.innerHTML = '';
        items.forEach(function (text) {
          var li = document.createElement('li');
          li.textContent = text;
          list.appendChild(li);
        });
      }

      openModal(modal);
    });
  });

  /* ------------------------------------------- row-driven activation dialog */

  $$('[data-activate-household]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#household-activate-modal');
      var form = $('#household-activate-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      // What the administrator entered, shown read-only.
      ['hid', 'owner', 'address', 'contact', 'email', 'line'].forEach(function (name) {
        var cell = $('[data-activate-field="' + name + '"]', modal);
        if (!cell) return;
        var value = trigger.getAttribute('data-' + name) || '';
        cell.textContent = value || cell.getAttribute('data-empty') || '';
        cell.classList.toggle('muted', !value);
      });
      setValue('activate_latitude', trigger.getAttribute('data-lat'));
      setValue('activate_longitude', trigger.getAttribute('data-lng'));

      openModal(modal);
    });
  });

  /* ------------------------------------------ row-driven facilitator dialogs */

  $$('[data-edit-facilitator]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var modal = $('#facilitator-edit-modal');
      var form = $('#facilitator-edit-form');
      if (!modal || !form) return;          // no dialog here: follow the link
      event.preventDefault();

      form.setAttribute('action', trigger.getAttribute('data-action'));
      setValue('edit_first_name', trigger.getAttribute('data-first'));
      setValue('edit_last_name', trigger.getAttribute('data-last'));
      setValue('edit_contact_number', trigger.getAttribute('data-contact'));
      setValue('edit_email', trigger.getAttribute('data-email'));

      var subtitle = $('[data-edit-subtitle]', modal);
      if (subtitle) subtitle.textContent = trigger.getAttribute('data-fid') + ' \u00b7 login ' + trigger.getAttribute('data-login');

      openModal(modal);
    });
  });

  $$('[data-delete-facilitator]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#facilitator-delete-modal');
      var form = $('#facilitator-delete-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var fid = trigger.getAttribute('data-fid');
      var name = trigger.getAttribute('data-name');
      var subtitle = $('[data-delete-subtitle]', modal);
      if (subtitle) subtitle.textContent = fid + ' \u00b7 ' + name;
      var who = $('[data-delete-name]', modal);
      if (who) who.textContent = fid + ' (' + name + ')';

      var list = $('[data-delete-list]', modal);
      if (list) {
        var visits = Number(trigger.getAttribute('data-visits') || 0);
        var items = ['the login \u201c' + trigger.getAttribute('data-login') + '\u201d'];
        items.push(visits
          ? visits + (visits === 1 ? ' open visit becomes' : ' open visits become') + ' unassigned (they stay on the schedule)'
          : 'no open visits are assigned to them');
        list.innerHTML = '';
        items.forEach(function (text) {
          var li = document.createElement('li');
          li.textContent = text;
          list.appendChild(li);
        });
      }

      openModal(modal);
    });
  });

  /* --------------------------------------------- row-driven account dialog */

  $$('[data-edit-account]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var modal = $('#account-edit-modal');
      var form = $('#account-edit-form');
      if (!modal || !form) return;          // no dialog here: follow the link
      event.preventDefault();

      form.setAttribute('action', trigger.getAttribute('data-action'));
      setValue('edit_first_name', trigger.getAttribute('data-first'));
      setValue('edit_last_name', trigger.getAttribute('data-last'));
      setValue('edit_contact_number', trigger.getAttribute('data-contact'));
      setValue('edit_email', trigger.getAttribute('data-email'));

      var subtitle = $('[data-edit-subtitle]', modal);
      if (subtitle) subtitle.textContent = trigger.getAttribute('data-login') + ' \u00b7 ' + trigger.getAttribute('data-role');

      openModal(modal);
    });
  });

  /* ----------------------------------------------- row-driven line dialogs */

  function setChecked(root, name, value) {
    var radio = root.querySelector('input[name="' + name + '"][value="' + value + '"]');
    if (radio) radio.checked = true;
  }

  $$('[data-edit-segment]').forEach(function (trigger) {
    trigger.addEventListener('click', function (event) {
      var modal = $('#line-edit-modal');
      var form = $('#line-edit-form');
      if (!modal || !form) return;           // no dialog here: follow the link
      event.preventDefault();

      form.setAttribute('action', trigger.getAttribute('data-action'));
      setValue('edit_name', trigger.getAttribute('data-name'));
      setValue('edit_low_pressure_bar', trigger.getAttribute('data-lowbar'));
      setValue('edit_start_lat', trigger.getAttribute('data-startlat'));
      setValue('edit_start_lng', trigger.getAttribute('data-startlng'));
      setValue('edit_end_lat', trigger.getAttribute('data-endlat'));
      setValue('edit_end_lng', trigger.getAttribute('data-endlng'));
      setValue('edit_maintenance_note', trigger.getAttribute('data-note'));

      // Colour is a radio group and is_active a checkbox, so neither takes a
      // plain .value assignment.
      setChecked(modal, 'color', trigger.getAttribute('data-color'));
      var active = document.getElementById('edit_is_active');
      if (active) active.checked = trigger.getAttribute('data-active') === '1';

      var subtitle = $('[data-edit-subtitle]', modal);
      if (subtitle) subtitle.textContent = trigger.getAttribute('data-module');

      openModal(modal);
    });
  });

  /*
   * Copy a device id or key to the clipboard.
   *
   * The key is 43 mixed-case characters with dashes and underscores, and the
   * realistic way it reaches a board is on a phone: copy here, join the
   * board's hotspot, paste. Typing it is how a working board gets rejected
   * with a 401 that looks exactly like a network fault.
   */
  /* Show only the fields that belong to the device option chosen. Both
     stay in the DOM so a failed submission still renders its own error. */
  (function () {
    var radios = $$('input[name="device_choice"]');
    if (!radios.length) return;

    function sync() {
      var chosen = radios.filter(function (r) { return r.checked; })[0];
      var value = chosen ? chosen.value : 'none';
      $$('[data-device-when]').forEach(function (block) {
        block.hidden = block.getAttribute('data-device-when') !== value;
      });
    }
    radios.forEach(function (radio) { radio.addEventListener('change', sync); });
    sync();
  })();

  $$('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var value = button.getAttribute('data-copy') || '';
      var done = function (ok) {
        var original = button.textContent;
        button.textContent = ok ? 'Copied' : 'Press Ctrl+C';
        button.classList.toggle('is-copied', ok);
        setTimeout(function () {
          button.textContent = original;
          button.classList.remove('is-copied');
        }, 1600);
      };

      // navigator.clipboard needs a secure context, and this system is
      // served over plain HTTP on a LAN by design - so the old selection
      // route is the one that actually runs most of the time, not a
      // curiosity to fall back to.
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(value).then(function () { done(true); },
                                                  function () { done(false); });
        return;
      }
      var scratch = document.createElement('textarea');
      scratch.value = value;
      scratch.setAttribute('readonly', '');
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(scratch);
      done(ok);
    });
  });

  /* Issuing a new key silences a board until somebody walks to it, so it is
     confirmed like a deletion and the dialog names what goes quiet. */
  $$('[data-newkey-device]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#device-key-modal');
      var form = $('#device-key-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var name = trigger.getAttribute('data-name');
      var line = trigger.getAttribute('data-line');
      var subtitle = $('[data-newkey-device-name]', modal);
      if (subtitle) subtitle.textContent = name;

      var consequence = $('[data-newkey-consequence]', modal);
      if (consequence) {
        consequence.textContent = line
          ? line + ' will stop reporting until you enter the new key on the board.'
          : 'This board is not fitted to a line yet, so nothing stops reporting.';
      }
      openModal(modal);
    });
  });

  $$('[data-delete-segment]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var modal = $('#line-delete-modal');
      var form = $('#line-delete-form');
      if (!modal || !form) return;
      form.setAttribute('action', trigger.getAttribute('data-action'));

      var module = trigger.getAttribute('data-module');
      var name = trigger.getAttribute('data-name');
      var subtitle = $('[data-delete-subtitle]', modal);
      if (subtitle) subtitle.textContent = module + ' · ' + name;
      var target = $('[data-delete-name]', modal);
      if (target) target.textContent = module + ' — ' + name;

      var rollups = Number(trigger.getAttribute('data-rollups') || 0);
      var events = Number(trigger.getAttribute('data-events') || 0);
      var list = $('[data-delete-list]', modal);
      if (list) {
        list.innerHTML = '';
        [rollups + (rollups === 1 ? ' minute of pressure history' : ' minutes of pressure history'),
         events + (events === 1 ? ' leak event' : ' leak events')]
          .forEach(function (text) {
            var li = document.createElement('li');
            li.textContent = text;
            list.appendChild(li);
          });
      }

      // Households survive the delete, so say what actually happens to them
      // rather than leaving the facilitator to guess.
      var households = Number(trigger.getAttribute('data-households') || 0);
      var note = $('[data-delete-households]', modal);
      if (note) {
        note.textContent = households
          ? households + (households === 1 ? ' household is' : ' households are') +
            ' served by this line. They are kept, but become Unassigned and stop ' +
            'receiving alerts until you move them to another line.'
          : 'No households are assigned to this line.';
      }

      openModal(modal);
    });
  });

  // Reopened by the server after a validation error, so the typed values and
  // the messages stay in front of the user instead of on a separate page.
  var autoOpen = $('[data-modal-autoopen]');
  if (autoOpen) {
    openModal(autoOpen);
    // A dialog reopened with an error (the sign-in after a wrong password)
    // puts focus on that error, so it is read out and the visitor tabs
    // straight on to the field rather than hunting for what went wrong.
    var alertBox = autoOpen.querySelector('[role="alert"][tabindex="-1"]');
    if (alertBox) alertBox.focus();
  }

  /* ------------------------------------------------- conversation search */

  // Filters the rows already rendered rather than asking the server, so it
  // narrows within whatever status filter is active and responds on every
  // keystroke. Fine at this scale; if the list ever outgrows one page this
  // has to move server-side, because it can only match what was sent.

  var threadSearch = $('[data-thread-search]');
  if (threadSearch) {
    var searchable = $$('.thread-item[data-search]');
    var noMatch = $('[data-search-empty]');

    var runSearch = function () {
      var term = threadSearch.value.trim().toLowerCase();
      var shown = 0;

      searchable.forEach(function (item) {
        var hit = !term || item.getAttribute('data-search').indexOf(term) !== -1;
        item.hidden = !hit;
        if (hit) shown += 1;
      });

      if (noMatch) noMatch.hidden = !(term && shown === 0);
    };

    threadSearch.addEventListener('input', runSearch);
    threadSearch.addEventListener('search', runSearch);   // the native clear (x)
    threadSearch.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        threadSearch.value = '';
        runSearch();
      }
    });
  }

  /* --------------------------------------------- live table quick filter
     The households table grows with the barangay. Narrowing it works on
     the rows already on the page - the live poll keeps updating hidden
     rows too, and clearing the box always brings everything back - and
     the address carries the filter so a reload or a shared link keeps it. */

  var liveFilter = $('[data-live-filter]');
  var problemsToggle = $('[data-problems-toggle]');
  if (liveFilter || problemsToggle) {
    var liveRows = $$('tr[data-segment-id][data-search]');
    var filterCount = $('[data-filter-count]');
    var filterEmpty = $('[data-filter-empty]');

    var applyFilter = function () {
      var term = liveFilter ? liveFilter.value.trim().toLowerCase() : '';
      var onlyProblems = !!(problemsToggle && problemsToggle.checked);
      var shown = 0;

      liveRows.forEach(function (row) {
        var hit = (!term || row.getAttribute('data-search').indexOf(term) !== -1) &&
                  (!onlyProblems || row.getAttribute('data-problem') === '1');
        row.hidden = !hit;
        if (hit) shown += 1;
      });

      if (filterCount) {
        filterCount.textContent = (term || onlyProblems)
          ? 'Showing ' + shown + ' of ' + liveRows.length
          : '';
      }
      if (filterEmpty) filterEmpty.hidden = !(liveRows.length && shown === 0);

      if (window.history && window.history.replaceState && window.URLSearchParams) {
        var params = new URLSearchParams(window.location.search);
        if (term) params.set('q', term); else params.delete('q');
        if (onlyProblems) params.set('problems', '1'); else params.delete('problems');
        var qs = params.toString();
        window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
      }
    };

    if (liveFilter) {
      liveFilter.addEventListener('input', applyFilter);
      liveFilter.addEventListener('search', applyFilter);   // the native clear (x)
      liveFilter.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { liveFilter.value = ''; applyFilter(); }
      });
    }
    if (problemsToggle) problemsToggle.addEventListener('change', applyFilter);
    applyFilter();
  }

  /* --------------------------------------------------------------- toasts */

  var toastBox = null;

  function toastContainer() {
    if (toastBox) return toastBox;
    toastBox = document.createElement('div');
    toastBox.className = 'toasts';
    // polite: announced at the next pause rather than interrupting a screen
    // reader mid-sentence. Device state is important, not urgent.
    toastBox.setAttribute('aria-live', 'polite');
    toastBox.setAttribute('role', 'status');
    document.body.appendChild(toastBox);
    return toastBox;
  }

  // SHL.toast({title, body, type: ok|warn|danger|info, timeout: ms})
  // timeout 0 keeps it until dismissed.
  SHL.toast = function (options) {
    var opts = options || {};
    var el = document.createElement('div');
    el.className = 'toast toast-' + (opts.type || 'info');

    var dot = document.createElement('span');
    dot.className = 'toast-dot';
    dot.setAttribute('aria-hidden', 'true');

    var text = document.createElement('div');
    text.className = 'toast-text';

    var title = document.createElement('div');
    title.className = 'toast-title';
    title.textContent = opts.title || '';
    text.appendChild(title);

    if (opts.body) {
      var body = document.createElement('div');
      body.className = 'toast-body';
      body.textContent = opts.body;
      text.appendChild(body);
    }

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.innerHTML = '&times;';

    var ms = opts.timeout === undefined ? 3000 : opts.timeout;
    var timer = null;

    function dismiss() {
      if (timer) clearTimeout(timer);
      if (el.parentNode) el.parentNode.removeChild(el);
    }
    close.addEventListener('click', dismiss);

    // Pause the countdown while the pointer is over the toast, so a message
    // being read - or copied from - does not vanish mid-sentence.
    el.addEventListener('mouseenter', function () {
      if (timer) { clearTimeout(timer); timer = null; }
    });
    el.addEventListener('mouseleave', function () {
      if (ms > 0 && !timer) timer = setTimeout(dismiss, ms);
    });

    el.appendChild(dot);
    el.appendChild(text);
    el.appendChild(close);
    toastContainer().appendChild(el);

    if (ms > 0) timer = setTimeout(dismiss, ms);
    return dismiss;
  };

  /* ------------------------------------------- server messages as toasts */

  // Django renders its messages framework into .messages blocks. They are
  // real markup rather than data so the page still works without this
  // script; here they are lifted into toasts and the inline block removed.
  //
  // [data-keep-inline] opts a block out - the temporary-password banner is
  // a standing warning, not a one-off event, and should not float away.

  var TAG_TO_TOAST = {
    success: 'ok',
    error: 'danger',
    warning: 'warn',
    info: 'info',
    debug: 'info'
  };

  $$('.messages').forEach(function (block) {
    if (block.hasAttribute('data-keep-inline')) return;

    var converted = 0;
    $$('.message', block).forEach(function (node) {
      var body = $('span', node);
      var content = (body || node).textContent.trim();
      if (!content) return;

      // Any message carrying a credential has to be read and written down,
      // so it waits to be dismissed rather than timing out. Same for
      // anything that went wrong - those are not glanceable.
      var type = 'info';
      Object.keys(TAG_TO_TOAST).forEach(function (tag) {
        if (node.classList.contains('message-' + tag)) type = TAG_TO_TOAST[tag];
      });
      SHL.toast({
        type: type,
        title: content,
        timeout: 3000
      });
      converted += 1;
    });

    if (converted) block.remove();
  });

  var liveHandlers = [];
  SHL.onLive = function (handler) { liveHandlers.push(handler); };

  var pollTimer = null;
  var failures = 0;

  function poll() {
    fetch('/api/live/', { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        failures = 0;
        liveHandlers.forEach(function (handler) {
          try { handler(data); } catch (e) { console.error('live handler failed', e); }
        });
        SHL.renderTiles(data.segments || []);
        SHL.updateAlertBanner(data.open_events || []);
      })
      .catch(function () {
        failures += 1;
        if (failures === 3) paintConnectionPill('offline');
      })
      .then(function () {
        // Back off when the server is unreachable so a lost link does not
        // hammer the connection; return to normal cadence once it recovers.
        var delay = failures > 2 ? Math.min(SHL.pollMs * failures, 30000) : SHL.pollMs;
        pollTimer = setTimeout(poll, delay);
      });
  }

  /* ------------------------------------------------- device online/offline */

  // null until the first payload: the very first reading establishes what
  // "current" is, so opening a page on an already-offline device does not
  // announce a transition that did not happen.
  var deviceOnline = null;

  SHL.onLive(function (data) {
    var device = data.device;
    if (!device) return;

    var online = !!device.online;

    // The single "Device status" tile this used to keep current is gone -
    // one board per household made one global answer meaningless, and each
    // row now carries its own. The toast below stays: a board dropping off
    // is worth interrupting for wherever the facilitator happens to be.
    if (deviceOnline === null) {
      deviceOnline = online;
      return;
    }
    if (online === deviceOnline) return;
    deviceOnline = online;

    // Not on the owner's screen. "The ESP32 stopped reporting" is a sentence
    // about equipment they do not own, cannot reach and cannot fix, and the
    // state block already tells them what it means for their water. A red
    // banner on top of it would only suggest they had done something wrong.
    if (document.body.classList.contains('is-owner')) return;

    if (online) {
      SHL.toast({
        type: 'ok',
        title: 'Device is online',
        body: 'Readings are arriving from the ESP32 again.'
      });
    } else {
      SHL.toast({
        type: 'danger',
        title: 'Device went offline',
        body: ‘No readings received recently. Check the board’s power and Wi-Fi.’,
        timeout: 3000
      });
    }
  });

  SHL.startPolling = function () {
    if (pollTimer) return;
    poll();
    document.addEventListener('visibilitychange', function () {
      // Stop polling in a hidden tab; refresh immediately on return.
      if (document.hidden) {
        clearTimeout(pollTimer);
        pollTimer = null;
      } else if (!pollTimer) {
        poll();
      }
    });
  };

  /* ------------------------------------------------------------ sms length */

  /*
   * Count a notice the way the network will bill it.
   *
   * A text is delivered in 160-character units, and the facilitator cannot
   * see that boundary while typing. Showing it here turns "why did that
   * arrive as three messages" into a decision made before sending, on a
   * system where every message costs load credit.
   */
  (function () {
    var body = $('[data-sms-counter]');
    var out = $('[data-sms-count]');
    var smsBox = $('#id_by_sms');
    if (!body || !out) return;

    var limit = parseInt(out.getAttribute('data-limit'), 10) || 160;
    var PART = 153;   // parts spend 7 characters on the "(1/2)" marker

    function update() {
      // Only relevant when the message is actually going out as a text.
      if (smsBox && !smsBox.checked) { out.hidden = true; return; }
      out.hidden = false;

      var length = body.value.length;
      var parts = length <= limit ? 1 : Math.ceil(length / PART);
      out.textContent = length + ' characters' +
        (parts > 1 ? ' - sends as ' + parts + ' text messages' : ' - one text message');
      out.classList.toggle('warn-text', parts > 1);
    }

    body.addEventListener('input', update);
    if (smsBox) smsBox.addEventListener('change', update);
    update();
  })();

  /* --------------------------------------------------- recipient picker */

  /*
   * A token field over the real checkboxes.
   *
   * The checkboxes are what the form posts and they never go away - this
   * hides them and drives them. With no JavaScript the plain grid is simply
   * visible and works as it always did, so nothing depends on this having
   * loaded.
   *
   * A grid was fine at seven households and stops working at fifty: finding
   * one becomes hunting, and the compose form grows with the barangay. A
   * modal would have fixed the space but hidden the choice behind a dialog,
   * so you could not see who you had picked while writing the message.
   */
  (function () {
    var root = $('[data-recipients]');
    if (!root) return;

    var all = $('#id_to_all');
    var grid = $('[data-household-picker]', root);
    var field = $('[data-token-field]', root);
    var chips = $('[data-token-chips]', root);
    var input = $('[data-token-input]', root);
    var menu = $('[data-token-menu]', root);
    var summary = $('[data-token-summary]', root);
    if (!grid || !field || !input || !menu) return;

    var boxes = $$('input[type="checkbox"]', grid);
    if (!boxes.length) return;

    // Take over only now that everything needed is present. Hiding the grid
    // any earlier would leave no way to choose if the rest failed.
    grid.hidden = true;
    field.hidden = false;
    if (summary) summary.hidden = false;

    var options = boxes.map(function (box) {
      var label = box.closest('label');
      return { box: box, text: (label ? label.textContent : '').trim() };
    });

    var activeIndex = -1;

    function chosen() {
      return options.filter(function (o) { return o.box.checked; });
    }

    function renderChips() {
      chips.innerHTML = '';
      chosen().forEach(function (option) {
        var chip = document.createElement('span');
        chip.className = 'token-chip';
        chip.textContent = option.text;

        var remove = document.createElement('button');
        remove.type = 'button';           // never submit the form
        remove.className = 'token-x';
        remove.setAttribute('aria-label', 'Remove ' + option.text);
        remove.textContent = '×';
        remove.addEventListener('click', function () {
          option.box.checked = false;
          renderChips();
          input.focus();
        });

        chip.appendChild(remove);
        chips.appendChild(chip);
      });

      if (summary) {
        var count = chosen().length;
        summary.textContent = all && all.checked
          ? 'Sending to every active household.'
          : count
            ? count + ' household' + (count === 1 ? '' : 's') + ' selected.'
            : 'Nobody selected yet.';
      }
    }

    function closeMenu() {
      menu.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      activeIndex = -1;
    }

    function matches() {
      var query = input.value.trim().toLowerCase();
      return options.filter(function (o) {
        return !o.box.checked && (!query || o.text.toLowerCase().indexOf(query) >= 0);
      });
    }

    function openMenu() {
      var found = matches();
      menu.innerHTML = '';

      if (!found.length) {
        var none = document.createElement('li');
        none.className = 'token-none';
        none.textContent = input.value.trim()
          ? 'No household matches that.'
          : 'Everyone is already selected.';
        menu.appendChild(none);
      } else {
        found.forEach(function (option, index) {
          var item = document.createElement('li');
          item.className = 'token-opt';
          item.setAttribute('role', 'option');
          item.textContent = option.text;
          // mousedown, not click: blur would close the menu first and the
          // click would land on nothing.
          item.addEventListener('mousedown', function (event) {
            event.preventDefault();
            pick(option);
          });
          item.addEventListener('mouseenter', function () { highlight(index); });
          menu.appendChild(item);
        });
      }

      menu.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      highlight(found.length ? 0 : -1);
    }

    function highlight(index) {
      activeIndex = index;
      $$('.token-opt', menu).forEach(function (item, i) {
        item.classList.toggle('is-active', i === index);
        item.setAttribute('aria-selected', i === index ? 'true' : 'false');
      });
    }

    function pick(option) {
      option.box.checked = true;
      input.value = '';
      renderChips();
      openMenu();
    }

    input.addEventListener('focus', openMenu);
    input.addEventListener('input', openMenu);

    input.addEventListener('keydown', function (event) {
      var found = matches();
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (menu.hidden) { openMenu(); return; }
        var step = event.key === 'ArrowDown' ? 1 : -1;
        highlight((activeIndex + step + found.length) % found.length);
      } else if (event.key === 'Enter') {
        // Only swallow Enter when it is choosing something. Otherwise it
        // should still submit the form, the way Enter does everywhere else.
        if (!menu.hidden && activeIndex >= 0 && found[activeIndex]) {
          event.preventDefault();
          pick(found[activeIndex]);
        }
      } else if (event.key === 'Escape') {
        closeMenu();
      } else if (event.key === 'Backspace' && input.value === '') {
        // Backspace on an empty box removes the last chip, which is what
        // every other token field does.
        var picked = chosen();
        if (picked.length) {
          picked[picked.length - 1].box.checked = false;
          renderChips();
        }
      }
    });

    input.addEventListener('blur', function () {
      // A tick, so a click on an option is not cancelled by the blur.
      setTimeout(closeMenu, 120);
    });

    /*
     * "All active households" and picking individuals are alternatives, not
     * things to combine. Disabling one while the other is on says so without
     * an error message after the fact.
     */
    function syncAll() {
      if (!all) return;
      field.classList.toggle('is-disabled', all.checked);
      input.disabled = all.checked;
      boxes.forEach(function (box) { box.disabled = all.checked; });
      if (all.checked) closeMenu();
      renderChips();
    }

    if (all) all.addEventListener('change', syncAll);
    renderChips();
    syncAll();
  })();

  /* ----------------------------------------------------------- tile render */

  /*
   * What each status means to the person whose water it is.
   *
   * Not the status label. "LOW_SUPPLY" and "Low Water Supply" both describe
   * the system's verdict rather than the household's situation, and neither
   * says whether to do anything. These say what is happening and what, if
   * anything, is worth doing - and the leak wording asks them to check their
   * own taps first, because a tap left slightly open and a small leak are
   * identical to the sensors and the tap is far more common.
   */
  var OWNER_STATE = {
    NORMAL: {
      head: 'Your water is normal',
      sub: 'Nothing wrong with your line right now.'
    },
    LOW_SUPPLY: {
      head: 'Your water is weak',
      sub: 'Pressure is lower than usual on your line. It may be slow at the tap.'
    },
    POSSIBLE_LEAK: {
      head: 'Possible leak on your line',
      sub: 'Please check that all your faucets are fully closed. If they are and water is still running, tell the water system facilitator.'
    },
    MAINTENANCE: {
      head: 'Under repair',
      sub: 'The facilitator is working on your line. Supply may stop for a while.'
    },
    OFFLINE: {
      head: 'Not being checked',
      sub: 'No readings are coming from your line. Your water may still be fine - the device has stopped reporting.'
    }
  };

  SHL.renderTiles = function (segments) {
    segments.forEach(function (segment) {
      var tile = $('[data-segment-id="' + segment.id + '"]');
      if (!tile) return;

      tile.setAttribute('data-status', segment.status);

      var pill = $('[data-field="status"]', tile);
      if (pill) {
        pill.className = 'pill pill-' + segment.status;
        pill.textContent = segment.status_label;
      }

      var setText = function (field, value) {
        var node = $('[data-field="' + field + '"]', tile);
        if (node) node.textContent = value;
      };

      setText('pressure_bar', fmt(segment.pressure_bar, 2));
      setText('pressure_psi', segment.pressure_psi === null ? '--' : fmt(segment.pressure_psi, 1) + ' psi');
      setText('flow_lpm', fmt(segment.flow_lpm, 2));
      setText('updated', timeAgo(segment.updated_at));
      setText('valve', segment.has_device ? (segment.valve_label || '--') : '--');
      setText('low_bar', fmt(segment.low_pressure_bar, 2));

      // A tile is collapsed until it has something to show. Cleared here so a
      // line that starts reporting opens up on its own, without a reload.
      if (segment.updated_at) tile.removeAttribute('data-empty');

      // A line waiting for hardware is not the same as one whose board has
      // stopped answering, so only lines that HAVE a device get a live/down
      // indicator. The no-device case is rendered server-side and left alone.
      var deviceDot = $('[data-field="device_dot"]', tile);
      if (deviceDot) {
        var live = segment.device_online && !segment.stale;
        deviceDot.className = 'dot ' + (live ? 'dot-live' : 'dot-down');
        setText('device', live ? 'live' : 'not reporting');
      }

      // A link to the board's own page. Hidden until it has reported at
      // least once, so it never offers an address that goes nowhere - and
      // kept current by the poll, because the address moves whenever the
      // network does and that is precisely when the link is needed.
      var ipLink = $('[data-field="device_ip"]', tile);
      if (ipLink) {
        if (segment.device_ip) {
          ipLink.hidden = false;
          ipLink.textContent = segment.device_ip;
          ipLink.setAttribute('href', 'http://' + segment.device_ip + '/');
        } else {
          ipLink.hidden = true;
        }
      }

      // The valve controls stay hidden until a board actually reports a
      // valve. The hardware is on order, and a button that silently does
      // nothing teaches people to distrust the ones that work.
      var valveForm = $('[data-valve-form]', tile);
      if (valveForm) {
        valveForm.hidden = !segment.valve_state || segment.valve_state === 'not_fitted';
      }

      // The owner's screen says it in words. The facilitator's wording below
      // is written for somebody who knows what a threshold is and which test
      // fired; an owner needs to know whether their water is all right and
      // what, if anything, they should do about it.
      var state = $('[data-field="state"]', tile);
      if (state) {
        var plain = OWNER_STATE[segment.status] || OWNER_STATE.OFFLINE;
        state.textContent = plain.head;

        var sub = $('[data-field="statenote"]', tile);
        if (sub) {
          sub.textContent = segment.status === 'MAINTENANCE' && segment.maintenance_note
            ? segment.maintenance_note
            : plain.sub;
        }
      }

      var note = $('[data-field="note"]', tile);
      if (note) {
        if (segment.status === 'POSSIBLE_LEAK') {
          // Which test fired matters more than the verdict: sustained low
          // flow is as likely to be a tap left open as a leak, while night
          // flow on a line the family says was idle is hard to explain away.
          var detail = [];
          if (segment.severity_label && segment.severity !== 'none') detail.push(segment.severity_label);
          if (segment.trigger_label && segment.trigger !== 'none') detail.push(segment.trigger_label);
          note.textContent = detail.length ? detail.join(' - ') : 'Suspected leak.';
        } else if (segment.status === 'MAINTENANCE') {
          note.textContent = segment.maintenance_note || 'Under maintenance - alerts paused.';
        } else if (segment.status === 'OFFLINE') {
          // A line with no board was being told to check the power and Wi-Fi
          // of a device that does not exist. Six rows said it at once, which
          // made the rollout look like six faults - and sent the facilitator
          // hunting for a problem that was never there.
          note.textContent = segment.has_device
            ? 'No readings received. Check the board’s power and Wi-Fi.'
            : 'No board fitted to this line yet.';
        } else if (segment.status === 'LOW_SUPPLY') {
          // Naming the figure turns a verdict into something checkable:
          // the facilitator can see at a glance whether the line is genuinely
          // weak or the threshold is simply set too high for this line.
          note.textContent = segment.low_pressure_bar
            ? 'Pressure is under this line’s ' + fmt(segment.low_pressure_bar, 2) + ' bar threshold.'
            : 'Pressure below the normal operating threshold.';
        } else {
          note.textContent = segment.simulated ? 'Simulated input.' : '';
        }
      }
    });

    if (window.SHLMap && typeof window.SHLMap.update === 'function') {
      window.SHLMap.update(segments);
    }
  };

  SHL.updateAlertBanner = function (events) {
    var banner = $('#alert-banner');
    if (!banner) return;
    var critical = events.filter(function (event) { return event.severity === 'critical'; });

    if (!critical.length) {
      banner.hidden = true;
      document.title = document.title.replace(/^\(\d+\)\s*/, '');
      return;
    }

    banner.hidden = false;
    banner.innerHTML = '';
    critical.forEach(function (event) {
      var row = document.createElement('div');
      row.className = 'message message-error';
      // The line is named after its household now, so naming the segment and
      // then its location said "HH-001 - Maria Santos" twice in one sentence.
      // The banner has one job - which household, and how bad - so it says
      // that once and gives the readings as evidence.
      row.innerHTML =
        '<span><strong>' + event.status_label + '</strong> on ' + event.segment + '. ' +
        fmt(event.pressure_bar, 2) + ' bar, ' +
        fmt(event.flow_lpm, 2) + ' L/min, ' + timeAgo(event.detected_at) + '.</span>';
      banner.appendChild(row);
    });

    if (!/^\(\d+\)/.test(document.title)) {
      document.title = '(' + critical.length + ') ' + document.title;
    }
  };

  /* -------------------------------------------------------- relative times */

  function refreshTimestamps() {
    $$('[data-ago]').forEach(function (node) {
      node.textContent = timeAgo(node.getAttribute('data-ago'));
    });
  }
  refreshTimestamps();
  setInterval(refreshTimestamps, 30000);

  /* ------------------------------------------------- show/hide password
     The eye on a password field. Flips the input between password and text
     and says which way it is now; the icon swaps so the state is visible
     without reading the label. Focus returns to the field so typing can
     carry on where it left off. */

  $$('[data-password-toggle]').forEach(function (button) {
    var wrap = button.closest('.pw-field');
    var input = wrap ? wrap.querySelector('input') : null;
    if (!input) return;
    var use = button.querySelector('use');
    button.addEventListener('click', function () {
      var showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      var label = showing ? 'Show password' : 'Hide password';
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
      button.setAttribute('aria-pressed', showing ? 'false' : 'true');
      if (use) use.setAttribute('href', showing ? '#i-eye' : '#i-eye-off');
      input.focus();
    });
  });

  /* ------------------------------------------------- sign-in dialog details
     Caps Lock is the usual reason a right password reads as wrong, so it is
     said while the key is held rather than after a failed attempt. On
     submit the button shows it is working, so a slow server is not mistaken
     for a frozen one, and a second tap cannot send the form twice. */

  $$('[data-login-form]').forEach(function (form) {
    var password = form.querySelector('input[name="password"]');
    var caps = form.querySelector('[data-caps-hint]');
    if (password && caps) {
      var sync = function (event) {
        var on = !!(event.getModifierState && event.getModifierState('CapsLock'));
        caps.hidden = !on;
      };
      password.addEventListener('keydown', sync);
      password.addEventListener('keyup', sync);
      password.addEventListener('blur', function () { caps.hidden = true; });
    }

    var submit = form.querySelector('[data-login-submit]');
    form.addEventListener('submit', function () {
      if (!submit) return;
      submit.setAttribute('aria-busy', 'true');
      submit.disabled = true;
      submit.textContent = 'Signing in…';
    });
  });

  /* ----------------------------------------------------------- PWA worker
     Owner-only: the service worker (offline shell + push) is registered on the
     household portal, and actively removed on the facilitator side so that
     half of the app stays an ordinary, non-installable web app. Public pages
     are left untouched so an owner signing in does not churn their worker. */

  // Every role: the shell is cached so the app opens on a weak signal, and
  // every navigation and API call still goes to the network first, so no
  // dashboard ever shows a stale reading as if it were live.
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () { /* not fatal */ });
    });
  }

  /* --------------------------------------------- install from the user menu
     The prompt is caught in <head> before this script runs; the menu button
     appears only once the browser has actually offered it. */
  $$('[data-install-trigger]').forEach(function (button) {
    function sync() { button.hidden = !window.__shlInstallPrompt; }
    document.addEventListener('shl-install-available', sync);
    document.addEventListener('shl-installed', sync);
    sync();
    button.addEventListener('click', function () {
      var prompt = window.__shlInstallPrompt;
      if (!prompt) return;
      prompt.prompt();
      prompt.userChoice
        .then(function () { window.__shlInstallPrompt = null; sync(); })
        .catch(function () { /* dismissed */ });
    });
  });

  /* ------------------------------------------------- install & alerts
     The owner's Settings page. Everything here answers a question only the
     browser can answer - is this already installed, will this phone offer to
     install it, has permission been granted - so none of it can be decided
     server-side and rendered.
     -------------------------------------------------------------------------- */

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify(body || {})
    });
  }

  /* The Push API wants the server's public key as bytes, and it is served as
     URL-safe base64 because that is what survives a .env file. */
  function urlBase64ToUint8Array(base64) {
    var padding = '='.repeat((4 - (base64.length % 4)) % 4);
    var raw = atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function isInstalled() {
    return window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true;
  }

  function isIOS() {
    return /iPad|iPhone|iPod/.test(navigator.userAgent)
      /* iPadOS 13+ reports itself as a Mac. The touch points are the giveaway
         a desktop Safari does not have. */
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  SHL.initSettings = function () {
    var show = function (group, which) {
      $$('[data-' + group + ']').forEach(function (node) {
        if (node.getAttribute('data-' + group) === which) node.hidden = false;
      });
    };

    /* ---- install ----
       Show exactly ONE state, and prefer the one-tap button whenever the
       browser has offered a prompt. The manual steps are only a fallback for
       iPhone (which never fires the event) and for browsers that decline it. */

    var setInstall = function (which) {
      $$('[data-install]').forEach(function (node) {
        var on = node.getAttribute('data-install') === which;
        node.hidden = !on;
        if (on && node.tagName === 'DETAILS') node.open = true;
      });
    };

    /* The prompt may already be waiting, caught by the early listener in the
       page head before this script ran. */
    var deferred = window.__shlInstallPrompt || null;

    var renderInstall = function () {
      if (isInstalled())           return setInstall('installed');
      if (deferred)                return setInstall('prompt');    // one-tap button
      if (!window.isSecureContext) return setInstall('insecure');
      if (isIOS())                 return setInstall('ios');       // Safari: manual
      return setInstall('android');                               // fallback steps
    };
    renderInstall();

    /* If the prompt arrives (or was already captured), swap to the button. */
    var onPrompt = function (event) {
      if (event && event.preventDefault) event.preventDefault();
      deferred = window.__shlInstallPrompt || event || deferred;
      window.__shlInstallPrompt = deferred;
      setInstall('prompt');
    };
    document.addEventListener('shl-install-available', onPrompt);
    window.addEventListener('beforeinstallprompt', onPrompt);

    var installButton = $('#install-now');
    if (installButton) {
      installButton.addEventListener('click', function () {
        if (!deferred) { renderInstall(); return; }
        deferred.prompt();
        deferred.userChoice.then(function (choice) {
          if (choice.outcome === 'accepted') setInstall('installed');
          deferred = null;
          window.__shlInstallPrompt = null;
        });
      });
    }

    var onInstalled = function () {
      deferred = null;
      window.__shlInstallPrompt = null;
      setInstall('installed');
    };
    window.addEventListener('appinstalled', onInstalled);
    document.addEventListener('shl-installed', onInstalled);

    /* ---- alerts ---- */

    var keyNode = document.getElementById('vapid-key');
    if (!keyNode) return;               /* server has no keys; nothing to wire */
    var publicKey = JSON.parse(keyNode.textContent);

    var status = $('[data-push="status"]');
    var say = function (text) { if (status) status.textContent = text || ''; };

    var supported = 'serviceWorker' in navigator
      && 'PushManager' in window
      && 'Notification' in window;

    if (!supported) { show('push', 'unsupported'); return; }
    if (Notification.permission === 'denied') { show('push', 'denied'); return; }

    var paint = function (subscription) {
      var on = $('[data-push="on"]');
      var off = $('[data-push="off"]');
      if (on) on.hidden = !subscription;
      if (off) off.hidden = !!subscription;
    };

    navigator.serviceWorker.ready.then(function (registration) {
      registration.pushManager.getSubscription().then(paint);

      var onButton = $('#push-on');
      if (onButton) {
        onButton.addEventListener('click', function () {
          say('Asking for permission…');
          Notification.requestPermission().then(function (permission) {
            if (permission !== 'granted') {
              say('');
              show('push', 'denied');
              return;
            }
            registration.pushManager.subscribe({
              /* Required by every browser: a push may not be silent. Ours
                 never is - each one draws a notification. */
              userVisibleOnly: true,
              applicationServerKey: urlBase64ToUint8Array(publicKey)
            }).catch(function () {
              /* The browser itself refused to create a subscription. Private
                 browsing is the usual reason - Chrome has no Push API there
                 and deliberately gives sites no way to detect it, so the
                 attempt failing is the only signal there is. Blaming the
                 connection here would send somebody to check their WiFi for
                 a problem that is not on their network. */
              throw new Error('browser');
            }).then(function (subscription) {
              return postJSON('/api/push/subscribe/', subscription.toJSON())
                .then(function (response) {
                  if (!response.ok) throw new Error('server');
                  paint(subscription);
                  say('Alerts are on for this phone.');
                });
            }).catch(function (error) {
              say(error && error.message === 'browser'
                ? 'This browser will not set up notifications. If you are using a private or incognito window, open the portal normally and try again.'
                : 'Could not turn alerts on. Check your connection and try again.');
            });
          });
        });
      }

      var offButton = $('#push-off');
      if (offButton) {
        offButton.addEventListener('click', function () {
          registration.pushManager.getSubscription().then(function (subscription) {
            if (!subscription) { paint(null); return; }
            var endpoint = subscription.endpoint;
            /* Tell the server first. Unsubscribing locally first and then
               failing to reach the server would leave a row that pushes to a
               handset which can no longer receive it. */
            postJSON('/api/push/unsubscribe/', { endpoint: endpoint })
              .then(function () { return subscription.unsubscribe(); })
              .then(function () { paint(null); say('Alerts are off for this phone.'); })
              .catch(function () { say('Could not turn alerts off. Try again.'); });
          });
        });
      }

      var testButton = $('#push-test');
      if (testButton) {
        testButton.addEventListener('click', function () {
          say('Sending…');
          postJSON('/api/push/test/').then(function (response) {
            return response.json().then(function (data) {
              say(response.ok ? 'Sent. It should appear in a moment.'
                              : (data.error || 'Could not send a test.'));
            });
          }).catch(function () { say('Could not send a test.'); });
        });
      }
    });
  };

  SHL.$ = $;
  SHL.$$ = $$;
  SHL.fmt = fmt;
  SHL.timeAgo = timeAgo;
  window.SHL = SHL;
})();

/* ---- Owner: My device -------------------------------------------------
   Polls the board's state and repaints the card, so the owner standing in
   the yard sees it turn to Connected without reloading. The server-rendered
   card is the no-JavaScript version of the same thing. */
SHL.initOwnerDevice = function () {
  var section = document.querySelector('[data-device-poll]');
  if (!section || !window.fetch) return;
  var url = section.getAttribute('data-device-poll');
  // The dialog lives outside the section, so look page-wide.
  var pick = function (name) { return document.querySelector('[data-device="' + name + '"]'); };
  var tones = ['ok', 'warn', 'danger', 'muted', 'info'];

  var buttonText = {
    lost: 'What to check', claimed_silent: 'Set up again', connecting: 'Setup steps'
  };
  var noButton = { connected: true, no_board: true, awaiting_activation: true };

  var paint = function (state) {
    var card = pick('card');
    if (card) card.setAttribute('data-device-state', state.key);
    ['label', 'mlabel'].forEach(function (name) {
      var label = pick(name);
      if (!label) return;
      label.textContent = state.label;
      tones.forEach(function (t) { label.classList.remove('pill-' + t); });
      label.classList.add('pill-' + state.tone);
    });
    var note = pick('note'); if (note) note.textContent = state.note;
    var mnote = pick('mnote'); if (mnote) mnote.textContent = state.note;

    var codeBox = pick('code-box');
    if (codeBox) {
      codeBox.hidden = !state.code;
      var code = pick('code'); if (code) code.textContent = state.code || '';
      var exp = pick('expires'); if (exp) exp.textContent = state.code_expires || '';
    }
    var steps = pick('steps');
    if (steps) steps.hidden = !(state.show_steps || state.can_reissue || state.key === 'connecting');
    var trouble = pick('trouble');
    if (trouble) trouble.hidden = !(state.key === 'lost' || state.key === 'claimed_silent');
    var resend = pick('resend'); if (resend) resend.hidden = !state.can_resend;
    var reissue = pick('reissue'); if (reissue) reissue.hidden = !state.can_reissue;

    var open = pick('open');
    if (open) {
      open.hidden = !!noButton[state.key];
      open.textContent = buttonText[state.key] || 'Set up my board';
    }
  };

  var tick = function () {
    fetch(url, { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) { if (state && state.key) paint(state); })
      .catch(function () { /* offline: keep what is shown */ });
  };
  // Every 5 s is quick enough to feel live and slow enough for a weak signal.
  tick();
  var timer = setInterval(tick, 5000);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { clearInterval(timer); timer = null; }
    else if (!timer) { tick(); timer = setInterval(tick, 5000); }
  });
};
