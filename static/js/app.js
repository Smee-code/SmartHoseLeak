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
    var first = focusables(el)[0];
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
      setValue('edit_start_m', trigger.getAttribute('data-start'));
      setValue('edit_end_m', trigger.getAttribute('data-end'));
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
  if (autoOpen) openModal(autoOpen);

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

    var timer = null;
    function dismiss() {
      if (timer) clearTimeout(timer);
      if (el.parentNode) el.parentNode.removeChild(el);
    }
    close.addEventListener('click', dismiss);

    el.appendChild(dot);
    el.appendChild(text);
    el.appendChild(close);
    toastContainer().appendChild(el);

    var ms = opts.timeout === undefined ? 8000 : opts.timeout;
    if (ms > 0) timer = setTimeout(dismiss, ms);
    return dismiss;
  };

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

    // Update the dashboard's device badge in place, so it agrees with the
    // toast instead of staying stale until the next reload.
    $$('[data-device-status]').forEach(function (el) {
      el.textContent = online ? 'Online' : 'Offline';
    });

    if (deviceOnline === null) {
      deviceOnline = online;
      return;
    }
    if (online === deviceOnline) return;
    deviceOnline = online;

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
        body: 'No readings received recently. Check the board’s power and Wi-Fi.',
        timeout: 12000
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

  /* ----------------------------------------------------------- tile render */

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
      setText('households', segment.households);

      var note = $('[data-field="note"]', tile);
      if (note) {
        if (segment.status === 'POSSIBLE_LEAK') {
          note.textContent = 'Suspected leak ' + segment.location_label + '.';
        } else if (segment.status === 'MAINTENANCE') {
          note.textContent = segment.maintenance_note || 'Under maintenance - alerts paused.';
        } else if (segment.status === 'OFFLINE') {
          note.textContent = 'No readings received. Check device power and Wi-Fi.';
        } else if (segment.status === 'LOW_SUPPLY') {
          note.textContent = 'Pressure below the normal operating threshold.';
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
      row.innerHTML =
        '<span><strong>' + event.status_label + '</strong> on ' + event.segment +
        ' &mdash; ' + event.location + '. ' +
        fmt(event.pressure_bar, 2) + ' bar (' + fmt(event.pressure_psi, 1) + ' psi), ' +
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

  /* ----------------------------------------------------------- PWA worker */

  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () { /* not fatal */ });
    });
  }

  SHL.$ = $;
  SHL.$$ = $$;
  SHL.fmt = fmt;
  SHL.timeAgo = timeAgo;
  window.SHL = SHL;
})();
